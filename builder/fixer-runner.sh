#!/bin/bash
# fixer-runner: backgrounded subprocess inside the openclaw container.
# Holds a per-repo lock, manages a shared git checkout under
# ~/.openclaw/projects/<repo>/, and runs `openclaw agent --local`
# in a poll loop: agent does one turn at a time, the wrapper checks
# the issue for new @-mention comments every POLL_INTERVAL, reacts
# :+1: to each, and re-invokes the agent with the comment as the
# next turn's user message (same --session-id so context persists).
#
# Strict one-PR-per-issue: on startup we look up any existing open PR
# linked to this issue (PR body contains "closes/fixes/resolves #<n>"
# OR head ref starts with "issue-<n>-"). If found we check out that
# branch and the prompt tells the agent to push commits to it, NOT
# open a new PR. Same check at every poll: if ANY linked PR is open,
# the fixer's job is done — exit.
#
# Args:
#   $1 repo full_name       (owner/name)
#   $2 issue number
#   $3 issue url            (https://github.com/owner/name/issues/N)
#   $4 issue title          (free text — used in the agent prompt)
#
# Required env:
#   GITHUB_TOKEN            — bot's PAT (already on the openclaw pod)
#
# Optional env:
#   FIXER_BOT_LOGIN         — bot's GH login. If unset, resolved from
#                             $GITHUB_TOKEN at startup via /user.
#   FIXER_POLL_INTERVAL     — seconds between comment polls (default 300)
#   FIXER_MAX_LIFETIME      — overall wall-clock cap, seconds (default 6h)
#   STORY_POINTS            — the issue's size, passed by the spawner from the
#                             plan. Picks solver vs solver.small. Absent means
#                             unestimated, which defaults to 8 — the strong
#                             model — because under-estimating is what makes a
#                             run die half-finished.
#   FIXER_SYNC_RETRY_CAP    — how many times an unresolved rebase conflict may
#                             wake the agent while neither end has moved
#                             (default 4)
set -uo pipefail

REPO="$1"
ISSUE_NUM="$2"
ISSUE_URL="$3"
ISSUE_TITLE="$4"

# -- permission gate ---------------------------------------------------
# FIRST, ahead of the identity lookup, the lock and the clone — see
# builder/project_allowlist.py. Being assigned an issue is how somebody ASKS
# for work; the owner's allowed-projects list is the answer. A refused repo
# must cost nothing and leave nothing behind, which it only can if the refusal
# happens before anything is created.
#
# Exit 2 is the CLI's "not permitted"; anything else means the list could not
# be read, which permits nothing either. Exit 0 from the runner, not a
# failure: not being permitted is a normal answer, and a CronJob that reports
# failures for it trains everyone to ignore its failures.
if ! PERM_REASON="$(project-allow check "$REPO" 2>&1)"; then
  echo "[permission] refusing to work on $REPO — ${PERM_REASON:-project-allow unavailable}" >&2
  echo "[permission] Grant it from chat with:  projects add $REPO" >&2
  exit 0
fi

# -- runtime knobs -----------------------------------------------------
# The builder units are installed FLAT next to this script (/usr/local/bin in
# the image), so the script's own directory is where the shell libraries and
# the Python modules live. Resolved rather than hardcoded so a copy running
# from a checkout finds its siblings instead of silently falling back to a
# different deployment's version of them.
LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd)"
[ -n "$LIB_DIR" ] && [ -r "$LIB_DIR/agent-models.sh" ] || LIB_DIR="/usr/local/bin"
CLAW_LIB_DIR="$LIB_DIR"
export PYTHONPATH="$LIB_DIR:${PYTHONPATH:-}"

# The shared shell libraries, installed WITHOUT their .sh suffix in the image;
# the suffix is tried too so this runner also works from a checkout. A library
# that is genuinely absent is not fatal: every call site below is guarded,
# because a missing tuning knob must not stop an issue from being worked.
_source_lib() {
  # ONLY EVER SOURCE THE .sh FILE.
  #
  # Each runtime knob ships as a PAIR in the same directory: `agent-limits` is
  # the CLI a human runs, `agent-limits.sh` is the library this sources. An
  # earlier version searched the bare name first and therefore found the CLI.
  #
  # Sourcing the CLI does not merely fail to define the helpers. The CLI parses
  # its (absent) subcommand, prints `unknown command`, and calls `exit` — and
  # in a SOURCED file that exit belongs to the caller, so the runner died on
  # its second line having done nothing. The symptom carried no error: the
  # spawner reported a successful spawn every tick while no run ever started,
  # no lock was taken and no log was written.
  #
  # The suffix is the whole rule. Testing the execute bit instead was tried and
  # rejected: some filesystems force 0755 on every file, and there the guard
  # refuses the real library and silently drops every knob back to its default.
  # A name is a fact about the file; a mode bit is a fact about the volume.
  for _d in "$LIB_DIR" /usr/local/bin; do
    if [ -r "$_d/$1.sh" ]; then . "$_d/$1.sh"; return 0; fi
  done
  return 1
}
_source_lib agent-limits || true
_source_lib agent-models || true
_source_lib agent-thinking || true
_source_lib agent-slot || true
_source_lib project-kind || true
_source_lib project-instructions || true

# Resolve bot identity from $GITHUB_TOKEN unless explicitly pinned via
# FIXER_BOT_LOGIN. Hardcoding the login would couple the code to one
# deployment's identity — sibling deployments use different tokens
# (e.g. sephiroth-claw vs whatever this cluster's bot is).
if [ -n "${FIXER_BOT_LOGIN:-}" ]; then
  BOT_LOGIN="$FIXER_BOT_LOGIN"
else
  BOT_LOGIN="$(curl -fsSL \
    -H "Authorization: Bearer $GITHUB_TOKEN" \
    -H "Accept: application/vnd.github+json" \
    -H "X-GitHub-Api-Version: 2022-11-28" \
    https://api.github.com/user 2>/dev/null \
    | python3 -c "import sys,json; print(json.load(sys.stdin).get('login',''))" \
    2>/dev/null)"
  if [ -z "$BOT_LOGIN" ]; then
    echo "FATAL: could not resolve bot identity from \$GITHUB_TOKEN /user — aborting" >&2
    exit 1
  fi
fi
POLL_INTERVAL="${FIXER_POLL_INTERVAL:-300}"

# How long a turn and a whole run may take. Runtime-adjustable through
# `agent-limits` (the store lives on the workspace PVC and is read at the
# START of every run), so a cap can be changed without a redeploy — which is
# the moment you usually want to change one. Both fall back to the values that
# were literals here before the store existed.
_SOLVER_LIFETIME_DEFAULT="${FIXER_MAX_LIFETIME:-$((6 * 3600))}"
_SOLVER_TURN_DEFAULT=3500
if command -v agent_limit >/dev/null 2>&1; then
  MAX_LIFETIME_SECONDS="$(agent_limit solver.lifetime "$_SOLVER_LIFETIME_DEFAULT")"
  AGENT_TURN_TIMEOUT="$(agent_limit solver.turn "$_SOLVER_TURN_DEFAULT")"
  agent_limit_note solver.lifetime "$_SOLVER_LIFETIME_DEFAULT" "$MAX_LIFETIME_SECONDS" 2>/dev/null || true
  agent_limit_note solver.turn "$_SOLVER_TURN_DEFAULT" "$AGENT_TURN_TIMEOUT" 2>/dev/null || true
else
  MAX_LIFETIME_SECONDS="$_SOLVER_LIFETIME_DEFAULT"
  AGENT_TURN_TIMEOUT="$_SOLVER_TURN_DEFAULT"
fi

# How hard this subsystem thinks. Empty means pass no --thinking and inherit
# the deployment default.
AGENT_THINKING=""
if command -v agent_thinking >/dev/null 2>&1; then
  AGENT_THINKING="$(agent_thinking solver 2>/dev/null || echo '')"
  agent_thinking_note solver "$AGENT_THINKING" 2>/dev/null || true
fi

# The name this run holds its concurrency slot under. agent-slot reads it, and
# an unnamed holder makes "who is holding the slots?" unanswerable in the log
# at the exact moment somebody is asking it.
SLOT_NAME="solver $REPO#$ISSUE_NUM"

STATE_ROOT="${HOME:-/home/node}/.openclaw"
PROJECTS_ROOT="$STATE_ROOT/projects"
PROJECT_DIR="$PROJECTS_ROOT/$REPO"
LOCK_ROOT="$STATE_ROOT/.fixer-locks"
LOCK_DIR="$LOCK_ROOT/${REPO//\//__}"
LOG_DIR="$STATE_ROOT/fixer-logs"
LOG_FILE="$LOG_DIR/${REPO//\//_}-${ISSUE_NUM}.log"
# Per-issue state files live OUTSIDE the git working tree because the
# `git clean -fdx` in the fresh-branch checkout path wipes anything
# under $PROJECT_DIR (.43/.45 bug observed on cursor + marker). Keep
# all per-issue scratch in $STATE_ROOT/issue-state/ — a sibling of
# projects/ and survives clean.
ISSUE_STATE_DIR="$STATE_ROOT/issue-state"
CURSOR_FILE="$ISSUE_STATE_DIR/${REPO//\//__}-${ISSUE_NUM}.cursor"
CI_FP_FILE="$ISSUE_STATE_DIR/${REPO//\//__}-${ISSUE_NUM}.ci-fingerprint"

# -- gate state that must OUTLIVE a single run -------------------------
#
# Every file here answers a question of the form "have I already done this?",
# and every one of them is on the PVC rather than in memory because the answer
# has to survive the run being killed. A run is killed by every deploy.
#
# The autonomous review requested for a head sha. One request per sha, so a
# re-push asks again and a tick that changes nothing says nothing.
AWAITING_REVIEW_MARKER="$STATE_ROOT/issue-markers/${REPO//\//__}-${ISSUE_NUM}.awaiting-review"
# The head sha a human sign-off was ASKED for, and the head sha it was GIVEN
# for. Two files, because "I asked" and "they answered" are different facts
# and collapsing them re-asks a question already answered.
APPROVAL_ASKED_FILE="$ISSUE_STATE_DIR/${REPO//\//__}-${ISSUE_NUM}.approval-asked"
APPROVAL_GRANTED_FILE="$ISSUE_STATE_DIR/${REPO//\//__}-${ISSUE_NUM}.approval-granted"
# "<default-branch-sha>:<head-sha>" of the last rebase conflict handed to the
# agent, plus how many times THAT pair has been handed over.
#
# The fingerprint alone is not enough, and the reason is the whole point of
# this pair. It is written the moment a conflict is OBSERVED, before the agent
# has done anything about it — so a woken run that then died (a 429, a deploy,
# an OOM) spent the trigger for good. Every later tick found the stored and
# current pair identical, declined to re-wake, and the pull request sat
# conflicted forever while the log said, reasonably, "already handed to the
# agent". A bounded retry budget is the answer, because detecting "the agent
# did not finish" reliably is harder than simply trying again a few times.
# Either end moving produces a new fingerprint, which RESETS the budget, so a
# conflict that was actually resolved costs nothing extra.
SYNC_FP_FILE="$ISSUE_STATE_DIR/${REPO//\//__}-${ISSUE_NUM}.sync-fp"
SYNC_RETRY_FILE="$ISSUE_STATE_DIR/${REPO//\//__}-${ISSUE_NUM}.sync-retries"
SYNC_RETRY_CAP="${FIXER_SYNC_RETRY_CAP:-4}"
MERGE_CONFLICT_NEW=0

mkdir -p "$LOG_DIR" "$LOCK_ROOT" "$ISSUE_STATE_DIR" \
         "$STATE_ROOT/issue-markers" "$(dirname "$PROJECT_DIR")"

# Per-repo lock. A fixer killed without its EXIT trap firing (SIGKILL,
# pod restart, lifetime cap) leaves an orphaned lock dir. The planner
# (heartbeat-issue-tick) treats locks older than its TTL as stale and keeps
# re-spawning us, so a plain mkdir-or-abort here would deadlock the repo
# forever. Reclaim the lock when it's stale: older than the TTL, or its owner
# PID is no longer alive in this pod.
LOCK_TTL="${FIXER_LOCK_TTL:-${HEARTBEAT_TTL_SECONDS:-3600}}"
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  lock_age=$(( $(date +%s) - $(stat -c %Y "$LOCK_DIR" 2>/dev/null || date +%s) ))
  lock_pid="$(awk 'NR==1{print $1}' "$LOCK_DIR/owner" 2>/dev/null || true)"
  if [ "$lock_age" -ge "$LOCK_TTL" ] || { [ -n "$lock_pid" ] && ! kill -0 "$lock_pid" 2>/dev/null; }; then
    echo "[$(date -Iseconds)] reclaiming stale lock for $REPO (age=${lock_age}s owner=${lock_pid:-?}); proceeding with #$ISSUE_NUM" >> "$LOG_FILE"
    rm -rf "$LOCK_DIR"
    if ! mkdir "$LOCK_DIR" 2>/dev/null; then
      echo "[$(date -Iseconds)] lock race after reclaim for $REPO; aborting fixer for #$ISSUE_NUM" >> "$LOG_FILE"
      exit 0
    fi
  else
    echo "[$(date -Iseconds)] lock held for $REPO (age=${lock_age}s, owner live); aborting fixer for #$ISSUE_NUM" >> "$LOG_FILE"
    exit 0
  fi
fi
echo "$BASHPID $(date -Iseconds) issue=$ISSUE_NUM" > "$LOCK_DIR/owner"

# WIPE_FULL_STATE toggles the per-issue cleanup in the EXIT trap. It is
# set to 1 ONLY when the issue itself is closed — at that point the
# fixer's memory of the issue (cursor + session jsonls) is finished
# business and can go. On every other exit (PR-exists, max-lifetime,
# crash, lock-collision) we keep the per-issue state on disk so the
# next cron tick can read the cursor and the pre-flight gate can decide
# cheaply whether to bother spawning an agent.
WIPE_FULL_STATE=0

wipe_issue_state() {
  rm -f "$CURSOR_FILE" 2>/dev/null
  rm -f "$CI_FP_FILE" 2>/dev/null
  rm -f "$AWAITING_REVIEW_MARKER" "$APPROVAL_ASKED_FILE" \
        "$APPROVAL_GRANTED_FILE" "$SYNC_FP_FILE" "$SYNC_RETRY_FILE" 2>/dev/null
  rm -f "$STATE_ROOT/issue-markers/${REPO//\//__}-${ISSUE_NUM}.lexical-asked" 2>/dev/null
  rm -f "$STATE_ROOT"/agents/main/sessions/issue-"${REPO//\//-}"-"$ISSUE_NUM"-*.jsonl 2>/dev/null
  rm -f "$STATE_ROOT"/agents/main/sessions/issue-"${REPO//\//-}"-"$ISSUE_NUM"-*.trajectory.jsonl 2>/dev/null
  rm -f "$STATE_ROOT"/agents/main/sessions/issue-"${REPO//\//-}"-"$ISSUE_NUM"-*.trajectory-path.json 2>/dev/null
  echo "[cleanup] wiped local state for $REPO#$ISSUE_NUM (cursor + ci-fingerprint + lexical-asked + session files)"
}

# -- exit bookkeeping --------------------------------------------------
#
# Three things happen here, in this order, and the order matters.
#
# 1. A delivered story has to SAY it was delivered. `mergedAt` on the story
#    document is what every report is built on — completed points, velocity,
#    the burndown, the story timeline — so a sprint where nothing writes it
#    reads as a sprint that shipped nothing, and a burndown line that never
#    falls looks like a bot that never finishes anything.
#
#    Asked of GitHub rather than recorded at the merge call, because a pull
#    request also lands by other routes: a human merges it, or a later tick
#    does. Asking what is TRUE beats trusting the path that happened to run.
#    Which pull request actually delivered the issue is delivering_pr.pick's
#    decision, not a guess from "the newest related one" — see that module for
#    what that guess costs.
#
# 2. Record what this run COST, so the estimator can be corrected by the
#    difference between the size it predicted and the model calls it took.
#
# 3. Release the concurrency slot. Never leak one: a leaked slot throttles all
#    three subsystems, and it looks exactly like a slot nobody wanted.
record_delivery() {
  command -v python3 >/dev/null 2>&1 || return 0
  # The trap is installed before the API constants are, and an exit in that
  # window must not turn into an unbound-variable error inside the trap.
  [ -n "${GH_API:-}" ] || return 0
  local prs created
  prs="$(curl -fsSL -H "$AUTH_HEADER" -H "$ACCEPT_HEADER" -H "$APIV_HEADER" \
    "$GH_API/repos/$REPO/pulls?state=closed&sort=updated&direction=desc&per_page=50" \
    2>/dev/null || true)"
  [ -n "$prs" ] || return 0
  created="$(curl -fsSL -H "$AUTH_HEADER" -H "$ACCEPT_HEADER" -H "$APIV_HEADER" \
    "$GH_API/repos/$REPO/issues/$ISSUE_NUM" 2>/dev/null \
    | python3 -c "import sys,json; print(json.load(sys.stdin).get('created_at') or '')" \
    2>/dev/null || echo '')"
  printf '%s' "$prs" | REPO="$REPO" ISSUE_NUM="$ISSUE_NUM" BRANCH="${BRANCH:-}" \
    ISSUE_CREATED="$created" LIB="${CLAW_LIB_DIR:-/usr/local/bin}" python3 -c "
import json, os, sys
sys.path.insert(0, os.environ['LIB'])
try:
    import delivering_pr, planning_docs as docs, planning_store as store
except Exception:
    raise SystemExit(0)
try:
    prs = json.load(sys.stdin)
except Exception:
    raise SystemExit(0)
repo = os.environ['REPO']
number = os.environ['ISSUE_NUM']
pr = delivering_pr.pick(prs, number,
                        branch=os.environ.get('BRANCH') or '',
                        not_before=os.environ.get('ISSUE_CREATED') or '',
                        repo=repo)
if not pr:
    raise SystemExit(0)
doc_id = docs.story_pk('github', repo, number)
rows = store.query({'id': doc_id, 'type': 'story'}, limit=1)
if not rows:
    raise SystemExit(0)   # never planned; inventing a story now would be worse
doc = rows[0]
if doc.get('mergedAt'):
    raise SystemExit(0)   # already recorded — this only ever FILLS an empty field
doc['mergedAt'] = pr.get('merged_at')
doc['deliveredBy'] = '%s#%s' % (repo, pr.get('number'))
if store.write(doc):
    sys.stderr.write('[planning] story recorded as delivered (PR #%s merged)\n'
                     % pr.get('number'))
" 2>&1 | grep -E '^\[planning\]' || true
}

on_exit() {
  record_delivery || true

  # Never fatal — planning-record always exits 0, and it is called with `|| true`
  # as well. A run that did its job must not be reported as failed because the
  # planning store had a bad day.
  if command -v planning-record >/dev/null 2>&1; then
    planning-record --role solver --repo "$REPO" --issue "$ISSUE_NUM" \
      --run-id "${SESSION_ID:-unknown}" --log "$LOG_FILE" \
      --since-line "${PLANNING_LOG_MARK:-0}" \
      --model "${AGENT_MODEL:-}" --worker "${BOT_LOGIN:-}" \
      --started "${PLANNING_STARTED_AT:-}" \
      --outcome "${RUN_OUTCOME:-}" || true
  fi

  command -v release_agent_slot >/dev/null 2>&1 && release_agent_slot

  if [ "$WIPE_FULL_STATE" = "1" ]; then
    wipe_issue_state
  fi
  rm -rf "$LOCK_DIR"
}
trap on_exit EXIT

# What this run is recorded as having achieved. Set at the points that decide
# it; empty means "nothing conclusive happened", which is the truth for the
# overwhelming majority of ticks.
RUN_OUTCOME=""
PLANNING_STARTED_AT="$(date -Iseconds)"
# The log is per ISSUE and accumulates across runs, so counting the whole file
# would attribute every previous run's model calls to this one.
PLANNING_LOG_MARK="$(wc -l < "$LOG_FILE" 2>/dev/null || echo 0)"

rm -rf "$PROJECT_DIR/.fixer.lock" 2>/dev/null
exec >> "$LOG_FILE" 2>&1

echo "============================================================"
echo "[$(date -Iseconds)] fixer start  repo=$REPO  issue=#$ISSUE_NUM"
echo "============================================================"

# -- GH API helpers ---------------------------------------------------

GH_API="https://api.github.com"
AUTH_HEADER="Authorization: Bearer $GITHUB_TOKEN"
ACCEPT_HEADER="Accept: application/vnd.github+json"
APIV_HEADER="X-GitHub-Api-Version: 2022-11-28"

export FIXER_BOT_LOGIN_VAL="$BOT_LOGIN"
export FIXER_ISSUE_NUM="$ISSUE_NUM"

# Find all OPEN PRs in this repo whose body says they close issue #N,
# OR whose head ref starts with `issue-<n>-`. Output: JSON array of
# {number, head_ref, html_url, title}.
fetch_open_prs_for_issue() {
  curl -fsSL -H "$AUTH_HEADER" -H "$ACCEPT_HEADER" -H "$APIV_HEADER" \
    "$GH_API/repos/$REPO/pulls?state=open&per_page=100" \
  | python3 -c "
import sys, json, re, os
n = os.environ['FIXER_ISSUE_NUM']
pat = re.compile(r'\\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\\s+#' + n + r'\\b', re.IGNORECASE)
prefix = f'issue-{n}-'
out = []
for p in json.load(sys.stdin):
    body = p.get('body') or ''
    head_ref = (p.get('head') or {}).get('ref','')
    if pat.search(body) or head_ref.startswith(prefix):
        out.append({
            'number': p['number'],
            'head_ref': head_ref,
            'html_url': p['html_url'],
            'title': p['title'],
        })
print(json.dumps(out))
"
}

# All comments on the issue (used to seed the agent's context).
fetch_all_comments() {
  curl -fsSL -H "$AUTH_HEADER" -H "$ACCEPT_HEADER" -H "$APIV_HEADER" \
    "$GH_API/repos/$REPO/issues/$ISSUE_NUM/comments?per_page=100"
}

# Issue body itself.
fetch_issue_body() {
  curl -fsSL -H "$AUTH_HEADER" -H "$ACCEPT_HEADER" -H "$APIV_HEADER" \
    "$GH_API/repos/$REPO/issues/$ISSUE_NUM" \
  | python3 -c "import sys,json; i=json.load(sys.stdin); print(i.get('body') or '')"
}

# Issue state ("open" or "closed"). Used to trigger full wipe on close.
fetch_issue_state() {
  curl -fsSL -H "$AUTH_HEADER" -H "$ACCEPT_HEADER" -H "$APIV_HEADER" \
    "$GH_API/repos/$REPO/issues/$ISSUE_NUM" \
  | python3 -c "import sys,json; print(json.load(sys.stdin).get('state','open'))"
}

# Repository owner login — the @-mention target for any question the
# bot needs to ask. Pinned to the repo owner (NOT the issue author) on
# purpose: later, the bot itself may create issues (e.g. from a chat
# command), and pinging the issue.user.login would mean the bot pings
# itself. The repo owner is always the right human to escalate to.
# Derived from `$REPO` (owner/name) so no API call needed.
repo_owner_login() {
  echo "${REPO%%/*}"
}

# Filter to comments newer than cursor where the bot is @-mentioned
# (case-insensitive). Skip the bot's own comments so we don't react to
# our own posts.
fetch_new_mentions() {
  local since_id="$1"
  curl -fsSL -H "$AUTH_HEADER" -H "$ACCEPT_HEADER" -H "$APIV_HEADER" \
    "$GH_API/repos/$REPO/issues/$ISSUE_NUM/comments?per_page=100" \
  | python3 -c "
import sys, json, re, os
since = int('${since_id:-0}')
bot = os.environ['FIXER_BOT_LOGIN_VAL'].lower()
mention_re = re.compile(r'@' + re.escape(bot) + r'\b', re.IGNORECASE)
out = []
for c in json.load(sys.stdin):
    if c['id'] <= since:
        continue
    if (c.get('user') or {}).get('login', '').lower() == bot:
        continue
    body = c.get('body') or ''
    if not mention_re.search(body):
        continue
    out.append({
        'id': c['id'],
        'user': c['user']['login'],
        'body': body,
        'html_url': c.get('html_url'),
    })
print(json.dumps(out))
"
}

react_to_comment() {
  local cid="$1"
  curl -fsSL -X POST -H "$AUTH_HEADER" -H "$ACCEPT_HEADER" -H "$APIV_HEADER" \
    -H 'Content-Type: application/json' \
    -d '{"content":"+1"}' \
    "$GH_API/repos/$REPO/issues/comments/$cid/reactions" >/dev/null 2>&1 \
    && echo "[react] thumbs-up on comment $cid" \
    || echo "[react] FAILED on comment $cid"
}

most_recent_comment_id() {
  curl -fsSL -H "$AUTH_HEADER" -H "$ACCEPT_HEADER" -H "$APIV_HEADER" \
    "$GH_API/repos/$REPO/issues/$ISSUE_NUM/comments?per_page=100" \
  | python3 -c "import sys,json; cs=json.load(sys.stdin); print(max((c['id'] for c in cs), default=0))"
}

# CI fingerprint: a stable token for the CI state on the PR head. The
# head SHA is part of the fingerprint so a new push (even one whose CI
# settles with the exact same set of check conclusions as the previous
# commit) still wakes the agent — otherwise a "fix that didn't fix"
# looks identical to "no change" and the bot misses the chance to
# diagnose the next root cause.
#
# Format:
#   "no-checks:<sha7>"     — head exists, no checks reported yet
#   "in-progress:<sha7>"   — at least one check still running / queued
#   "settled:<sha7>:<hash>"— all checks settled; hash over (name,conclusion) pairs
#
# Pre-flight gate wakes the agent on ANY change. So:
#   - push of a fix → sha7 changes → wake
#   - last check settles → settled prefix → wake
#   - CI flaps red after a hotfix attempt → still wakes via sha
ci_fingerprint_for_pr() {
  local pr_num="$1"
  local head_sha
  head_sha=$(curl -fsSL -H "$AUTH_HEADER" -H "$ACCEPT_HEADER" -H "$APIV_HEADER" \
    "$GH_API/repos/$REPO/pulls/$pr_num" 2>/dev/null \
    | python3 -c "import sys,json; print(json.load(sys.stdin)['head']['sha'])" 2>/dev/null)
  if [ -z "$head_sha" ]; then echo "unknown"; return; fi
  local sha7="${head_sha:0:7}"
  curl -fsSL -H "$AUTH_HEADER" -H "$ACCEPT_HEADER" -H "$APIV_HEADER" \
    "$GH_API/repos/$REPO/commits/$head_sha/check-runs?per_page=100" 2>/dev/null \
  | SHA7="$sha7" python3 -c "
import sys, json, hashlib, os
sha7 = os.environ['SHA7']
try:
    d = json.load(sys.stdin)
except Exception:
    print('unknown'); sys.exit(0)
runs = d.get('check_runs', [])
if not runs:
    print(f'no-checks:{sha7}'); sys.exit(0)
if any(r.get('status') != 'completed' for r in runs):
    print(f'in-progress:{sha7}'); sys.exit(0)
completed = sorted([(r['name'], r.get('conclusion') or 'unknown') for r in runs])
h = hashlib.sha256(repr(completed).encode()).hexdigest()[:16]
print(f'settled:{sha7}:{h}')
"
}

# Human-readable summary of CI on the PR head, included in the
# initial agent prompt so the agent can act on rule 8 (CI red → fix)
# or rule 9 (CI green + no more work → request review) without
# having to fetch first.
ci_summary_text_for_pr() {
  local pr_num="$1"
  local head_sha
  head_sha=$(curl -fsSL -H "$AUTH_HEADER" -H "$ACCEPT_HEADER" -H "$APIV_HEADER" \
    "$GH_API/repos/$REPO/pulls/$pr_num" 2>/dev/null \
    | python3 -c "import sys,json; print(json.load(sys.stdin)['head']['sha'])" 2>/dev/null)
  if [ -z "$head_sha" ]; then echo "(could not fetch CI status)"; return; fi
  curl -fsSL -H "$AUTH_HEADER" -H "$ACCEPT_HEADER" -H "$APIV_HEADER" \
    "$GH_API/repos/$REPO/commits/$head_sha/check-runs?per_page=100" 2>/dev/null \
  | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
except Exception:
    print('(unparseable check-runs response)'); sys.exit(0)
runs = d.get('check_runs', [])
if not runs:
    print('(no checks reported yet on head sha)'); sys.exit(0)
for r in sorted(runs, key=lambda x: x['name']):
    status = r.get('status','?')
    conclusion = r.get('conclusion') or '-'
    url = r.get('html_url','')
    marker = '✅' if conclusion == 'success' else ('❌' if conclusion in ('failure','cancelled','timed_out') else '⏳')
    print(f'{marker} {r[\"name\"]:35s} status={status:12s} conclusion={conclusion:10s} {url}')
"
}

# For every failing check-run on the PR head, fetch the job log via
# the GitHub API and pull out the last ~80 lines plus any error-
# pattern matches. We inject this into the agent's initial prompt
# under a "## Failing CI excerpt" heading so the agent doesn't have
# to remember to call github__get_job_logs before reasoning — the
# evidence is already in front of it.
ci_failing_logs_for_pr() {
  local pr_num="$1"
  local head_sha
  head_sha=$(curl -fsSL -H "$AUTH_HEADER" -H "$ACCEPT_HEADER" -H "$APIV_HEADER" \
    "$GH_API/repos/$REPO/pulls/$pr_num" 2>/dev/null \
    | python3 -c "import sys,json; print(json.load(sys.stdin)['head']['sha'])" 2>/dev/null)
  if [ -z "$head_sha" ]; then return 0; fi
  # Pull check-runs and pick out the failing job_ids from details_url
  # (format: https://github.com/<owner>/<repo>/actions/runs/<run_id>/job/<job_id>).
  local FAILED_JOBS
  FAILED_JOBS="$(curl -fsSL -H "$AUTH_HEADER" -H "$ACCEPT_HEADER" -H "$APIV_HEADER" \
    "$GH_API/repos/$REPO/commits/$head_sha/check-runs?per_page=100" 2>/dev/null \
  | python3 -c "
import sys, json, re
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
for r in d.get('check_runs', []):
    if r.get('conclusion') not in ('failure', 'cancelled', 'timed_out'):
        continue
    m = re.search(r'/runs/(\d+)/job/(\d+)', r.get('details_url') or '')
    if not m:
        continue
    print(f\"{r['name']}\t{m.group(2)}\")
" 2>/dev/null)"
  if [ -z "$FAILED_JOBS" ]; then return 0; fi
  while IFS=$'\t' read -r jobname jobid; do
    [ -n "$jobid" ] || continue
    echo "### ❌ $jobname"
    echo
    echo '```'
    # The logs endpoint returns 302 to an Azure blob URL; -L follows it.
    curl -fsSL -H "$AUTH_HEADER" -H "$ACCEPT_HEADER" -H "$APIV_HEADER" \
      "$GH_API/repos/$REPO/actions/jobs/$jobid/logs" 2>/dev/null \
    | python3 -c "
import sys, re
raw = sys.stdin.read()
# Strip ANSI + the leading ISO8601 timestamp on each line for legibility.
ansi = re.compile(r'\x1b\\[[0-9;]*m')
ts = re.compile(r'^\\d{4}-\\d{2}-\\d{2}T[0-9:.]+Z\\s*')
lines = [ts.sub('', ansi.sub('', ln)).rstrip() for ln in raw.split('\\n')]
# 1. Show every line that looks like a failure signal
patterns = re.compile(
    r'\\b(error|ERROR|FAIL\\b|✗|✘|Failing:|AssertionError|Exception|Traceback|threshold|does not meet|below|not met|coverage for|expected.*to|ENOENT|exit code [1-9])\\b',
    re.IGNORECASE,
)
hits = [ln for ln in lines if patterns.search(ln)]
# 2. Plus the last 30 raw lines (often contain the summary)
tail = [ln for ln in lines if ln.strip()][-30:]
seen = set()
out = []
for ln in hits[:40] + ['---'] + tail:
    if ln in seen: continue
    seen.add(ln)
    out.append(ln[:240])
print('\\n'.join(out))
" 2>/dev/null
    echo '```'
    echo
  done <<< "$FAILED_JOBS"
}

# CI gate: returns "green" if every check-run on the PR's head SHA
# completed=success, "pending" if none have reported yet, "not_green"
# otherwise. The user's rule is "only request review when all pipelines
# are running [green]" — anything other than "green" disqualifies the
# PR from having a reviewer assigned.
ci_status_for_pr() {
  local pr_num="$1"
  local head_sha
  head_sha=$(curl -fsSL -H "$AUTH_HEADER" -H "$ACCEPT_HEADER" -H "$APIV_HEADER" \
    "$GH_API/repos/$REPO/pulls/$pr_num" 2>/dev/null \
    | python3 -c "import sys,json; print(json.load(sys.stdin)['head']['sha'])" 2>/dev/null)
  if [ -z "$head_sha" ]; then
    echo "unknown"
    return
  fi
  curl -fsSL -H "$AUTH_HEADER" -H "$ACCEPT_HEADER" -H "$APIV_HEADER" \
    "$GH_API/repos/$REPO/commits/$head_sha/check-runs?per_page=100" 2>/dev/null \
  | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
except Exception:
    print('unknown'); sys.exit(0)
runs = d.get('check_runs', [])
if not runs:
    print('pending')
elif all(r.get('status') == 'completed' and r.get('conclusion') == 'success' for r in runs):
    print('green')
else:
    print('not_green')
"
}

# List requested-reviewer logins on the PR (one per line).
fetch_pr_reviewers() {
  local pr_num="$1"
  curl -fsSL -H "$AUTH_HEADER" -H "$ACCEPT_HEADER" -H "$APIV_HEADER" \
    "$GH_API/repos/$REPO/pulls/$pr_num/requested_reviewers" 2>/dev/null \
  | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
for u in d.get('users', []):
    print(u['login'])
"
}

# Enforce the invariant: while CI is not all-green on a PR, that PR
# must have ZERO requested reviewers. If the agent added one
# prematurely (against rule 9), this wipes it. Idempotent + cheap to
# call on every tick.
enforce_no_reviewer_when_ci_red() {
  local pr_num="$1"
  local reviewers
  reviewers="$(fetch_pr_reviewers "$pr_num")"
  if [ -z "$reviewers" ]; then
    return 0
  fi
  local status
  status="$(ci_status_for_pr "$pr_num")"
  if [ "$status" = "green" ]; then
    echo "[ci-gate] PR #$pr_num CI green and reviewers=[$(echo "$reviewers" | tr '\n' ',' | sed 's/,$//')] — allowed"
    return 0
  fi
  local reviewers_json
  reviewers_json="$(echo "$reviewers" | python3 -c "
import sys, json
logins = [l.strip() for l in sys.stdin if l.strip()]
print(json.dumps({'reviewers': logins}))
")"
  curl -fsSL -X DELETE -H "$AUTH_HEADER" -H "$ACCEPT_HEADER" -H "$APIV_HEADER" \
    -H 'Content-Type: application/json' \
    -d "$reviewers_json" \
    "$GH_API/repos/$REPO/pulls/$pr_num/requested_reviewers" >/dev/null 2>&1 \
    && echo "[ci-gate] PR #$pr_num CI=$status — removed reviewer(s) [$(echo "$reviewers" | tr '\n' ',' | sed 's/,$//')] (rule 9: no review until all checks green)" \
    || echo "[ci-gate] PR #$pr_num CI=$status — FAILED to remove reviewers"
}

# -- work-item status --------------------------------------------------
# GitHub has two states, open and closed, and the solver needs five answers —
# see builder/issue_status.py. Non-terminal statuses live in a `status::`
# label, terminal ones in GitHub's own close reason, which is the one place
# GitHub is genuinely better than the model it replaces: it records the
# operator's intent at the moment of closing and never overwrites it.

post_issue_comment() { # $1 = body
  BODY="$1" python3 -c 'import os,json;print(json.dumps({"body":os.environ["BODY"]}))' \
  | curl -fsSL -X POST -H "$AUTH_HEADER" -H "$ACCEPT_HEADER" -H "$APIV_HEADER" \
      -H 'Content-Type: application/json' -d @- \
      "$GH_API/repos/$REPO/issues/$ISSUE_NUM/comments" >/dev/null 2>&1
}

# Move the issue to a NON-TERMINAL status. Refuses on a closed issue.
#
# That refusal is the important half. GitHub reopens a closed issue as a side
# effect of some writes, and a status the wrapper sets out of habit on a tick
# after the work landed would resurrect an issue a human deliberately ended —
# silently, five minutes after they closed it, over and over. Nothing here may
# be the reason a closed issue comes back.
set_issue_status() { # $1 = status name (issue_status vocabulary)
  local want="$1"
  if [ "${ISSUE_STATE:-open}" = "closed" ]; then
    echo "[status] #$ISSUE_NUM is closed — NOT setting '$want' (that would reopen it)"
    return 0
  fi
  local updates
  updates="$(LABELS="$ISSUE_LABELS_JSON" WANT="$want" python3 -c "
import json, os, sys
sys.path.insert(0, os.environ.get('PYTHONPATH','').split(os.pathsep)[0])
import issue_status
labels = json.loads(os.environ['LABELS'] or '[]')
add, remove = issue_status.label_updates(labels, os.environ['WANT'])
print(json.dumps({'add': add, 'remove': remove}))
" 2>/dev/null)"
  [ -n "$updates" ] || { echo "[status] could not compute the label diff for '$want'"; return 0; }
  # An empty diff means the issue already says this. Writing anyway would
  # append a timeline event on every five-minute tick, and an issue whose
  # history is a wall of identical label events is an issue nobody can read.
  local add remove
  add="$(printf '%s' "$updates" | python3 -c "import sys,json;print(json.dumps(json.load(sys.stdin)['add']))")"
  remove="$(printf '%s' "$updates" | python3 -c "import sys,json;print('\n'.join(json.load(sys.stdin)['remove']))")"
  if [ "$add" != "[]" ]; then
    printf '%s' "{\"labels\": $add}" \
    | curl -fsSL -X POST -H "$AUTH_HEADER" -H "$ACCEPT_HEADER" -H "$APIV_HEADER" \
        -H 'Content-Type: application/json' -d @- \
        "$GH_API/repos/$REPO/issues/$ISSUE_NUM/labels" >/dev/null 2>&1 \
      && echo "[status] #$ISSUE_NUM → $want" \
      || echo "[status] WARNING: could not set '$want' on #$ISSUE_NUM"
  fi
  # GitHub's issue PATCH has no add/remove semantics, so each removal is its
  # own DELETE. Applying BOTH halves is what keeps two `status::` labels off
  # one issue — GitHub does not enforce one-value-per-scope.
  while IFS= read -r stale; do
    [ -n "$stale" ] || continue
    curl -fsSL -X DELETE -H "$AUTH_HEADER" -H "$ACCEPT_HEADER" -H "$APIV_HEADER" \
      "$GH_API/repos/$REPO/issues/$ISSUE_NUM/labels/$(printf '%s' "$stale" | python3 -c 'import sys,urllib.parse;print(urllib.parse.quote(sys.stdin.read().strip()))')" \
      >/dev/null 2>&1 || true
  done <<< "$remove"
}

# Close the issue with the reason a TERMINAL status implies: `completed` for a
# delivery, `not_planned` for a revoke. The distinction is the whole reason
# terminal status lives in the close reason — "was this delivered?" has to be
# answerable afterwards without re-deriving it from the merge history.
close_issue_as() { # $1 = terminal status name
  local reason
  reason="$(WANT="$1" python3 -c "
import os, sys
sys.path.insert(0, os.environ.get('PYTHONPATH','').split(os.pathsep)[0])
import issue_status
print(issue_status.close_reason(os.environ['WANT']) or '')
" 2>/dev/null)"
  [ -n "$reason" ] || { echo "[status] '$1' is not a terminal status — not closing"; return 1; }
  if [ "${ISSUE_STATE:-open}" = "closed" ]; then
    echo "[status] #$ISSUE_NUM already closed — leaving its close reason alone"
    return 0
  fi
  printf '%s' "{\"state\":\"closed\",\"state_reason\":\"$reason\"}" \
  | curl -fsSL -X PATCH -H "$AUTH_HEADER" -H "$ACCEPT_HEADER" -H "$APIV_HEADER" \
      -H 'Content-Type: application/json' -d @- \
      "$GH_API/repos/$REPO/issues/$ISSUE_NUM" >/dev/null 2>&1 \
    && { echo "[status] closed #$ISSUE_NUM as $1 (state_reason=$reason)"; ISSUE_STATE=closed; return 0; } \
    || { echo "[status] WARNING: could not close #$ISSUE_NUM"; return 1; }
}

# -- pull-request facts ------------------------------------------------

pr_head_sha() { # $1 = pr number
  curl -fsSL -H "$AUTH_HEADER" -H "$ACCEPT_HEADER" -H "$APIV_HEADER" \
    "$GH_API/repos/$REPO/pulls/$1" 2>/dev/null \
  | python3 -c "import sys,json; print((json.load(sys.stdin).get('head') or {}).get('sha') or '')" 2>/dev/null
}

# "<mergeable_state> <draft>" — e.g. "clean false", "dirty false", "blocked true".
#
# `mergeable_state` rather than `mergeable`: GitHub computes mergeability
# asynchronously and answers null while it is thinking, and reading null as
# "not mergeable" would make every freshly-pushed head look conflicted.
pr_merge_facts() { # $1 = pr number
  curl -fsSL -H "$AUTH_HEADER" -H "$ACCEPT_HEADER" -H "$APIV_HEADER" \
    "$GH_API/repos/$REPO/pulls/$1" 2>/dev/null \
  | python3 -c "
import sys, json
try:
    p = json.load(sys.stdin)
except Exception:
    print('unknown false'); raise SystemExit(0)
print('%s %s' % (p.get('mergeable_state') or 'unknown',
                 'true' if p.get('draft') else 'false'))
" 2>/dev/null
}

# -- autonomous review gate --------------------------------------------
# The pr-reviewer subsystem (k8s/052-reviewer.yaml) reviews green pull
# requests the bot is asked to review and posts a machine-readable comment
# whose FIRST LINE is
#     🔎 REVIEW RESULT: APPROVED|CHANGES REQUIRED (sha <head_sha>)
# The solver merges only after an APPROVED verdict for the CURRENT head. A
# verdict names its sha so a verdict about an older commit can never
# green-light a newer one — which is the whole failure mode a review gate
# keyed on the pull request alone would have.

REVIEWER_ENABLED_CACHE=""
reviewer_enabled() {  # 0 = reviewer active, 1 = suspended / absent / unreachable
  if [ -z "$REVIEWER_ENABLED_CACHE" ]; then
    local ns suspend
    ns="$(cat /var/run/secrets/kubernetes.io/serviceaccount/namespace 2>/dev/null || echo claw-code-local)"
    if suspend="$(kubectl -n "$ns" get cronjob pr-reviewer -o jsonpath='{.spec.suspend}' 2>/dev/null)"; then
      [ "$suspend" = "true" ] && REVIEWER_ENABLED_CACHE=0 || REVIEWER_ENABLED_CACHE=1
    else
      # Cannot tell. Fail OPEN to the documented pre-reviewer behaviour: green
      # pull requests merge. Failing closed would mean that switching the
      # reviewer off, or losing the RBAC to ask about it, silently stops every
      # merge in every repository with nothing on the issue to say why.
      REVIEWER_ENABLED_CACHE=0
    fi
  fi
  [ "$REVIEWER_ENABLED_CACHE" = "1" ]
}

# The newest reviewer verdict on the pull request → "approved <sha>" /
# "changes <sha>" / "" (no verdict at all). The sha comes from the verdict's
# own first line, not from the pull request.
pr_review_verdict() { # $1 = pr number
  curl -fsSL -H "$AUTH_HEADER" -H "$ACCEPT_HEADER" -H "$APIV_HEADER" \
    "$GH_API/repos/$REPO/issues/$1/comments?per_page=100" 2>/dev/null \
  | BOT="$BOT_LOGIN" python3 -c "
import sys, json, re, os
bot = os.environ['BOT'].lower()
try:
    cs = json.load(sys.stdin)
except Exception:
    cs = []
for c in reversed(cs if isinstance(cs, list) else []):
    if ((c.get('user') or {}).get('login','').lower()) != bot:
        continue
    body = (c.get('body') or '').strip()
    if not body.startswith('🔎 REVIEW RESULT:'):
        continue
    first = body.splitlines()[0]
    m = re.search(r'\(sha ([0-9a-fA-F]{7,40})\)', first)
    print(('approved' if 'APPROVED' in first else 'changes') + ' ' + (m.group(1) if m else ''))
    break
" 2>/dev/null
}

# Ask the pr-reviewer for a review of the CURRENT head: request the bot as
# reviewer (which is what the reviewer's planner keys on) and post ONE request
# comment per sha. The marker is what makes it one per sha rather than one per
# tick — a comment every five minutes is how a pull request becomes unreadable.
request_self_review() { # $1 = pr number, $2 = head sha
  local pr="$1" sha="$2" requested=""
  printf '%s' "{\"reviewers\":[\"$BOT_LOGIN\"]}" \
  | curl -fsSL -X POST -H "$AUTH_HEADER" -H "$ACCEPT_HEADER" -H "$APIV_HEADER" \
      -H 'Content-Type: application/json' -d @- \
      "$GH_API/repos/$REPO/pulls/$pr/requested_reviewers" >/dev/null 2>&1 \
    || echo "[review-gate] WARNING: could not request a review on PR #$pr"
  [ -f "$AWAITING_REVIEW_MARKER" ] && requested="$(cat "$AWAITING_REVIEW_MARKER" 2>/dev/null)"
  if [ "$requested" = "$sha" ]; then
    echo "[review-gate] review of ${sha:0:8} already requested — waiting for the verdict"
    return 0
  fi
  post_issue_comment "🔎 Requested an autonomous review of \`${sha:0:8}\` on PR #$pr — the reviewer runs the app locally, checks the acceptance criteria and scans the change. I merge only after its ✅." \
    && echo "[review-gate] posted the review request for ${sha:0:8}" \
    || echo "[review-gate] WARNING: failed to post the review request"
  printf '%s' "$sha" > "$AWAITING_REVIEW_MARKER"
}

# The gate. 0 → satisfied (or the reviewer is off): the caller may merge.
# 1 → do NOT merge yet.
review_gate() { # $1 = pr number
  if ! reviewer_enabled; then
    # Reviewer suspended or unreachable → the documented pre-reviewer
    # behaviour: a green pull request merges. Drop any stale wait state so the
    # gate does not resume mid-wait when the reviewer is switched back on.
    rm -f "$AWAITING_REVIEW_MARKER" 2>/dev/null
    echo "[review-gate] the pr-reviewer CronJob is suspended — merging green pull requests directly"
    return 0
  fi
  local head_sha verdict vsha
  head_sha="$(pr_head_sha "$1")"
  [ -z "$head_sha" ] && { echo "[review-gate] could not resolve the head sha of PR #$1 — not merging"; return 1; }
  read -r verdict vsha <<< "$(pr_review_verdict "$1")"
  if [ "${verdict:-}" = "approved" ] && [ "${vsha:-}" = "$head_sha" ]; then
    rm -f "$AWAITING_REVIEW_MARKER" 2>/dev/null
    echo "[review-gate] the reviewer APPROVED ${head_sha:0:8} — merge may proceed"
    return 0
  fi
  if [ "${verdict:-}" = "changes" ] && [ "${vsha:-}" = "$head_sha" ]; then
    echo "[review-gate] the reviewer requires CHANGES on ${head_sha:0:8} — not merging"
    return 1
  fi
  # No verdict for THIS head: none at all, or one about a commit that has been
  # superseded. A push invalidates a verdict, so this is a fresh request.
  request_self_review "$1" "$head_sha"
  return 1
}

# -- human sign-off gate (the `approval` label) ------------------------
# Everything above answers "may this be merged?" with machinery. This answers
# it with a person, and it is the LAST gate: it only ever runs on a pull
# request that is already green, already conflict-free, not a draft and
# already approved by the autonomous reviewer. The label means "I want to look
# at this before it lands", and the question is only worth asking once
# everything else has already said yes.
#
# It FAILS OPEN when the labels cannot be read — see story_estimate.
# requires_approval for the reasoning: a gate that can turn itself on by
# accident would stop every merge in every repository for a reason invisible
# from the issue.
APPROVAL_REQUIRED_CACHE=""
approval_required() {  # 0 = a human must sign off before the merge
  if [ -z "$APPROVAL_REQUIRED_CACHE" ]; then
    local answer
    answer="$(LABELS="$ISSUE_LABELS_JSON" python3 -c "
import json, os, sys
sys.path.insert(0, os.environ.get('PYTHONPATH','').split(os.pathsep)[0])
import story_estimate
print('1' if story_estimate.requires_approval(json.loads(os.environ['LABELS'] or '[]')) else '0')
" 2>/dev/null)"
    case "$answer" in
      1) APPROVAL_REQUIRED_CACHE=1 ;;
      0) APPROVAL_REQUIRED_CACHE=0 ;;
      *) APPROVAL_REQUIRED_CACHE=0
         echo "[approval-gate] could not read the labels of #$ISSUE_NUM — proceeding without a sign-off gate" ;;
    esac
  fi
  [ "$APPROVAL_REQUIRED_CACHE" = "1" ]
}

# Has a HUMAN approved this exact head? Prints the login, or nothing.
#
# GitHub's own review state, keyed on `commit_id`: an approval is given to the
# code somebody actually read, so a later push must not inherit it. The bot's
# own review never counts — it is the same account that opened the pull
# request, and an account approving itself is not a sign-off.
pr_human_approval() { # $1 = pr number, $2 = head sha
  curl -fsSL -H "$AUTH_HEADER" -H "$ACCEPT_HEADER" -H "$APIV_HEADER" \
    "$GH_API/repos/$REPO/pulls/$1/reviews?per_page=100" 2>/dev/null \
  | BOT="$BOT_LOGIN" SHA="$2" python3 -c "
import sys, json, os
bot = os.environ['BOT'].lower()
sha = os.environ['SHA']
try:
    rs = json.load(sys.stdin)
except Exception:
    rs = []
for r in reversed(rs if isinstance(rs, list) else []):
    who = ((r.get('user') or {}).get('login') or '')
    if who.lower() == bot:
        continue
    if (r.get('state') or '').upper() != 'APPROVED':
        continue
    if sha and (r.get('commit_id') or '') != sha:
        continue     # approved an older commit; a push invalidates a sign-off
    print(who)
    break
" 2>/dev/null
}

request_merge_approval() { # $1 = pr number, $2 = head sha
  local pr="$1" sha="$2" asked="" owner
  owner="$(repo_owner_login)"
  [ -f "$APPROVAL_ASKED_FILE" ] && asked="$(cat "$APPROVAL_ASKED_FILE" 2>/dev/null)"
  if [ "$asked" = "$sha" ]; then
    echo "[approval-gate] sign-off for ${sha:0:8} already requested — waiting"
    return 0
  fi
  post_issue_comment "🛂 MERGE APPROVAL REQUESTED (sha \`${sha:0:8}\`)

@$owner — this issue is labelled \`approval\`, so I will not merge it without you. Everything else is done: the checks are green, there are no conflicts, PR #$pr is not a draft, and the autonomous review approved \`${sha:0:8}\`.

To let it land, **approve PR #$pr** on GitHub. If you want changes first, say so here and @-mention \`@$BOT_LOGIN\`. If I push another commit I will ask again — an approval covers the code it was given for." \
    && echo "[approval-gate] asked @$owner to sign off on ${sha:0:8}" \
    || echo "[approval-gate] WARNING: could not post the approval request"
  printf '%s' "$sha" > "$APPROVAL_ASKED_FILE"
}

approval_gate() { # $1 = pr number, $2 = head sha
  if ! approval_required; then
    rm -f "$APPROVAL_ASKED_FILE" 2>/dev/null   # label removed → the wait is stale
    return 0
  fi
  local pr="$1" sha="$2" granted="" who
  [ -z "$sha" ] && { echo "[approval-gate] could not resolve the head sha — not merging"; return 1; }
  [ -f "$APPROVAL_GRANTED_FILE" ] && granted="$(cat "$APPROVAL_GRANTED_FILE" 2>/dev/null)"
  if [ "$granted" = "$sha" ]; then
    echo "[approval-gate] sign-off already on record for ${sha:0:8} — merge may proceed"
    return 0
  fi
  who="$(pr_human_approval "$pr" "$sha")"
  if [ -n "$who" ]; then
    printf '%s' "$sha" > "$APPROVAL_GRANTED_FILE"
    rm -f "$APPROVAL_ASKED_FILE" 2>/dev/null
    echo "[approval-gate] @$who approved ${sha:0:8} — merge may proceed"
    return 0
  fi
  request_merge_approval "$pr" "$sha"
  echo "[approval-gate] waiting for a human sign-off on ${sha:0:8}"
  return 1
}

# -- the merge ---------------------------------------------------------
# The wrapper merges, not the agent. Rule 7 used to hand the merge to the
# model, and a model cannot be relied on to observe a gate it can also
# rationalise its way past — which is the entire reason the review and
# sign-off gates exist. Deciding it here also means the decision is testable
# without a model call.
merge_pr() { # $1 = pr number
  printf '%s' "{\"merge_method\":\"squash\"}" \
  | curl -fsSL -X PUT -H "$AUTH_HEADER" -H "$ACCEPT_HEADER" -H "$APIV_HEADER" \
      -H 'Content-Type: application/json' -d @- \
      "$GH_API/repos/$REPO/pulls/$1/merge" >/dev/null 2>&1
}

# Does the issue body forbid the bot from merging? A pre-existing opt-out,
# kept: some issues are opened precisely so a person presses the button.
merge_forbidden_by_issue() {
  BODY="$ISSUE_BODY" python3 -c "
import os, re, sys
body = (os.environ.get('BODY') or '').lower()
patterns = (r'do\s*not\s*merge', r\"don'?t\s*merge\", r'no\s*auto-?\s*merge',
            r'leave\s*(it\s*)?for\s*review', r'manual\s*review\s*only',
            r'hold\s*for\s*approval')
sys.exit(0 if any(re.search(p, body) for p in patterns) else 1)
" 2>/dev/null
}

# 0 = merged. 1 = not merged (and the log says which gate stopped it).
maybe_merge_green_pr() { # $1 = pr number
  local pr="$1" status facts state draft head_sha
  status="$(ci_status_for_pr "$pr")"
  if [ "$status" != "green" ]; then
    echo "[merge] PR #$pr checks are '$status' — not merging"
    return 1
  fi
  read -r state draft <<< "$(pr_merge_facts "$pr")"
  if [ "${draft:-false}" = "true" ]; then
    echo "[merge] PR #$pr is a draft — not merging"
    return 1
  fi
  case "${state:-unknown}" in
    dirty)
      echo "[merge] PR #$pr conflicts with the base branch — not merging"
      return 1 ;;
    unknown)
      # GitHub computes mergeability asynchronously and answers null while it
      # is thinking. That is "ask again", not "no".
      echo "[merge] PR #$pr mergeability not computed yet — waiting for the next tick"
      return 1 ;;
  esac
  if merge_forbidden_by_issue; then
    echo "[merge] the issue body opts out of auto-merge — leaving PR #$pr for a human"
    return 1
  fi
  head_sha="$(pr_head_sha "$pr")"
  review_gate "$pr" || return 1
  # LAST, deliberately: a person is asked to sign off only once everything
  # else has already said yes.
  approval_gate "$pr" "$head_sha" || return 1
  if ! merge_pr "$pr"; then
    echo "[merge] WARNING: the merge of PR #$pr was refused — leaving it open"
    return 1
  fi
  echo "[merge] merged PR #$pr (${head_sha:0:8})"
  RUN_OUTCOME="merged"
  # The delivery is what closes the issue, and `completed` is what says it was
  # a delivery rather than a revoke.
  close_issue_as done || true
  command -v telegram-notify >/dev/null 2>&1 \
    && telegram-notify "✅ Merged $REPO#$ISSUE_NUM via PR #$pr — $ISSUE_TITLE"
  return 0
}

# -- rebase-conflict retry --------------------------------------------
# A branch that has fallen behind the default branch far enough to CONFLICT
# leaves a pull request that is green, reviewed, signed off — and unmergeable.
# A conflict is the one blocker the wake gates cannot see: it is not a
# mention, not a CI change and not a verdict, so without this the run exits in
# seconds having spent nothing, every five minutes, forever.
#
# 0 = the agent should be woken to resolve it. 1 = nothing to do.
conflict_needs_agent() { # $1 = pr number
  local pr="$1" state draft head fp last tries
  read -r state draft <<< "$(pr_merge_facts "$pr")"
  [ "${state:-unknown}" = "dirty" ] || { rm -f "$SYNC_FP_FILE" "$SYNC_RETRY_FILE" 2>/dev/null; return 1; }
  head="$(pr_head_sha "$pr")"
  fp="$(git rev-parse "origin/${DEFAULT_BRANCH:-HEAD}" 2>/dev/null || echo unknown):${head:-unknown}"
  last=""
  [ -f "$SYNC_FP_FILE" ] && last="$(cat "$SYNC_FP_FILE" 2>/dev/null)"
  tries=0
  [ -f "$SYNC_RETRY_FILE" ] && tries="$(cat "$SYNC_RETRY_FILE" 2>/dev/null || echo 0)"
  case "$tries" in ''|*[!0-9]*) tries=0 ;; esac
  if [ "$last" != "$fp" ]; then
    printf '%s' "$fp" > "$SYNC_FP_FILE"
    echo 1 > "$SYNC_RETRY_FILE"
    echo "[conflict] new rebase conflict ($fp) — waking the agent to resolve it"
    return 0
  fi
  if [ "$tries" -lt "$SYNC_RETRY_CAP" ]; then
    # Still conflicted on the same pair. That is NOT "already handled": the
    # fingerprint was written when the conflict was OBSERVED, so a run that
    # died before resolving it spent the trigger for good. See SYNC_RETRY_FILE.
    tries=$(( tries + 1 ))
    echo "$tries" > "$SYNC_RETRY_FILE"
    echo "[conflict] rebase conflict still unresolved — waking the agent (attempt $tries/$SYNC_RETRY_CAP)"
    return 0
  fi
  echo "[conflict] rebase conflict unresolved after $SYNC_RETRY_CAP attempts — not retrying (a human should look)"
  return 1
}

# -- issue snapshot ----------------------------------------------------
# One fetch, three answers that the rest of the run keeps asking for: is it
# open, what did whoever closed it mean, and what is on it. Fetched once
# because every gate below reads the same facts, and three fetches of the same
# issue can disagree with each other mid-tick.

ISSUE_JSON="$(curl -fsSL -H "$AUTH_HEADER" -H "$ACCEPT_HEADER" -H "$APIV_HEADER" \
  "$GH_API/repos/$REPO/issues/$ISSUE_NUM" 2>/dev/null || echo '{}')"
ISSUE_STATE="$(printf '%s' "$ISSUE_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin).get('state') or 'open')" 2>/dev/null || echo open)"
ISSUE_STATE_REASON="$(printf '%s' "$ISSUE_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin).get('state_reason') or '')" 2>/dev/null || echo '')"
ISSUE_LABELS_JSON="$(printf '%s' "$ISSUE_JSON" | python3 -c "
import sys, json
try:
    i = json.load(sys.stdin)
except Exception:
    i = {}
print(json.dumps([str((l or {}).get('name') or '') if isinstance(l, dict) else str(l)
                  for l in (i.get('labels') or [])]))
" 2>/dev/null || echo '[]')"
ISSUE_STATUS="$(LABELS="$ISSUE_LABELS_JSON" STATE="$ISSUE_STATE" REASON="$ISSUE_STATE_REASON" python3 -c "
import json, os, sys
sys.path.insert(0, os.environ.get('PYTHONPATH','').split(os.pathsep)[0])
import issue_status
print(issue_status.status_of(json.loads(os.environ['LABELS'] or '[]'),
                             state=os.environ['STATE'],
                             state_reason=os.environ['REASON'] or None))
" 2>/dev/null || echo 'to do')"
echo "[status] #$ISSUE_NUM is '$ISSUE_STATUS' (state=$ISSUE_STATE reason=${ISSUE_STATE_REASON:-none})"

# A REVOKED issue: a human set `status::wont-do` or `status::duplicate` while
# leaving it open. That is a terminal call the bot must honour rather than
# work — and honouring it means closing the issue with `not_planned`, which is
# what keeps a revoked issue from being counted as a delivery six weeks later.
case "$ISSUE_STATUS" in
  "won't do"|duplicate)
    if [ "$ISSUE_STATE" != "closed" ]; then
      echo "[status] #$ISSUE_NUM was revoked ('$ISSUE_STATUS') — closing it as not_planned instead of working it"
      close_issue_as "$ISSUE_STATUS" || true
      RUN_OUTCOME="revoked"
    fi
    WIPE_FULL_STATE=1
    exit 0 ;;
esac

# -- model routing -----------------------------------------------------
# Which model implements THIS story, decided from its size and then from any
# pin the issue carries.
#
# The size arrives in $STORY_POINTS from the spawner, which reads it off the
# issue's `SP::<n>` label. An UNESTIMATED story defaults to 8 and never
# qualifies for the cheap lane: under-estimating is the expensive direction —
# it is what makes a run die half-finished — so an unknown story gets the
# strong model. A defaulted 8 and a judged 8 route identically and are still
# not the same fact, which is why the default is logged as one.
case "${STORY_POINTS:-}" in
  ''|*[!0-9]*) STORY_POINTS=8; POINTS_DEFAULTED=1 ;;
  *)           POINTS_DEFAULTED=0 ;;
esac
[ "$POINTS_DEFAULTED" = "1" ] && echo "[model] $REPO#$ISSUE_NUM is unestimated — planning with the default $STORY_POINTS point(s)"

SOLVER_MODEL_KEY="solver"
_SMALL_MAX=3
command -v agent_count >/dev/null 2>&1 && _SMALL_MAX="$(agent_count solver.small.max_points 3)"
# agent_model_raw, NOT agent_model: the raw reader answers "" when the key is
# unset, while agent_model falls back to the deployment baseline and is
# therefore almost never empty — which would route every small story into a
# cheap lane nobody configured.
if [ "$POINTS_DEFAULTED" = "0" ] && [ "${_SMALL_MAX:-0}" -gt 0 ] \
   && [ "$STORY_POINTS" -le "$_SMALL_MAX" ] 2>/dev/null \
   && command -v agent_model_raw >/dev/null 2>&1 \
   && [ -n "$(agent_model_raw solver.small 2>/dev/null)" ]; then
  SOLVER_MODEL_KEY="solver.small"
  echo "[model] $REPO#$ISSUE_NUM is $STORY_POINTS point(s) (<= $_SMALL_MAX) — using the small-story model"
fi

AGENT_MODEL=""
command -v agent_model >/dev/null 2>&1 && AGENT_MODEL="$(agent_model "$SOLVER_MODEL_KEY" 2>/dev/null || echo '')"

# A `model::` label on the issue OVERRIDES everything above. It is a human's
# routing instruction, and the one input here that is not something the bot
# worked out for itself.
ISSUE_MODEL_LABEL="$(LABELS="$ISSUE_LABELS_JSON" python3 -c "
import json, os, sys
sys.path.insert(0, os.environ.get('PYTHONPATH','').split(os.pathsep)[0])
import story_estimate
print(story_estimate.model_label(json.loads(os.environ['LABELS'] or '[]')))
" 2>/dev/null || echo '')"
if [ -n "$ISSUE_MODEL_LABEL" ] && command -v agent_model_from_label >/dev/null 2>&1; then
  _PINNED="$(agent_model_from_label "$ISSUE_MODEL_LABEL" 2>/dev/null || echo '')"
  if [ -n "$_PINNED" ]; then
    AGENT_MODEL="$_PINNED"
    SOLVER_MODEL_KEY="model::$ISSUE_MODEL_LABEL"
    echo "[model] #$ISSUE_NUM is pinned to '$ISSUE_MODEL_LABEL' by a label — using $AGENT_MODEL (overrides the size-based choice)"
  else
    # An unresolvable pin keeps whatever we would have used. A story is still
    # worked, just not on the model somebody hoped for — better than a run
    # that dies at its first request.
    echo "[model] #$ISSUE_NUM is pinned to '$ISSUE_MODEL_LABEL', which resolves to nothing configured here — keeping ${AGENT_MODEL:-the deployment default}"
  fi
fi
command -v agent_model_note >/dev/null 2>&1 && agent_model_note "$SOLVER_MODEL_KEY" "$AGENT_MODEL" 2>/dev/null || true

# `openclaw agent` takes no empty --model, so the flag is built as an array
# that is simply absent when nothing was chosen.
AGENT_MODEL_ARGS=()
[ -n "$AGENT_MODEL" ] && AGENT_MODEL_ARGS=(--model "$AGENT_MODEL")
[ -n "$AGENT_THINKING" ] && AGENT_MODEL_ARGS+=(--thinking "$AGENT_THINKING")

# -- detect existing PR + pick branch ---------------------------------

EXISTING_PRS_JSON="$(fetch_open_prs_for_issue 2>/dev/null || echo '[]')"
EXISTING_PR_COUNT="$(echo "$EXISTING_PRS_JSON" | python3 -c 'import sys,json;print(len(json.load(sys.stdin)))')"

if [ "$EXISTING_PR_COUNT" -ge 1 ]; then
  # Resume on the existing PR's branch. If multiple are open (legacy
  # mess from before this fix), pick the lowest-numbered one — that's
  # the first one the bot opened, and we'll work to merge IT rather
  # than continue the cascade.
  EXISTING_PR_NUMBER="$(echo "$EXISTING_PRS_JSON" | python3 -c "import sys,json; ps=sorted(json.load(sys.stdin), key=lambda p: p['number']); print(ps[0]['number'])")"
  EXISTING_PR_BRANCH="$(echo "$EXISTING_PRS_JSON" | python3 -c "import sys,json; ps=sorted(json.load(sys.stdin), key=lambda p: p['number']); print(ps[0]['head_ref'])")"
  EXISTING_PR_URL="$(echo "$EXISTING_PRS_JSON" | python3 -c "import sys,json; ps=sorted(json.load(sys.stdin), key=lambda p: p['number']); print(ps[0]['html_url'])")"
  BRANCH="$EXISTING_PR_BRANCH"
  echo "[pr] resuming existing PR #$EXISTING_PR_NUMBER on branch '$BRANCH' ($EXISTING_PR_URL)"
  echo "[pr] also-open (will note in prompt): $EXISTING_PRS_JSON"
else
  EXISTING_PR_NUMBER=""
  EXISTING_PR_BRANCH=""
  EXISTING_PR_URL=""
  BRANCH="issue-$ISSUE_NUM-fix"
  echo "[pr] no open PR linked to issue #$ISSUE_NUM yet; will work on fresh branch '$BRANCH'"
fi

# -- workspace setup --------------------------------------------------

if [ ! -d "$PROJECT_DIR/.git" ]; then
  echo "[clone] $REPO → $PROJECT_DIR"
  git clone --quiet "https://github.com/$REPO.git" "$PROJECT_DIR"
fi
cd "$PROJECT_DIR"
git fetch --quiet origin
DEFAULT_BRANCH="$(git remote show origin | awk '/HEAD branch/ {print $NF}')"
echo "[checkout] default-branch=$DEFAULT_BRANCH"

if [ -n "$EXISTING_PR_BRANCH" ] && git ls-remote --heads origin "$EXISTING_PR_BRANCH" | grep -q .; then
  # Resume on the existing PR's remote branch
  git checkout --quiet "$DEFAULT_BRANCH"
  git branch -D "$EXISTING_PR_BRANCH" 2>/dev/null || true
  git checkout --quiet -b "$EXISTING_PR_BRANCH" "origin/$EXISTING_PR_BRANCH"
  echo "[checkout] resumed existing branch $EXISTING_PR_BRANCH from origin"
else
  # Fresh branch off default
  git checkout --quiet "$DEFAULT_BRANCH"
  git reset --hard --quiet "origin/$DEFAULT_BRANCH"
  git clean -fdx --quiet
  git branch -D "$BRANCH" 2>/dev/null || true
  git checkout --quiet -b "$BRANCH"
  echo "[checkout] created fresh branch $BRANCH off $DEFAULT_BRANCH"
fi

# -- gather issue context for the agent -------------------------------

ISSUE_BODY="$(fetch_issue_body 2>/dev/null || echo '')"
ALL_COMMENTS_JSON="$(fetch_all_comments 2>/dev/null || echo '[]')"
# Default @-mention target = repo owner (NOT issue author). Stable
# even when the bot itself creates issues later via chat commands.
ISSUE_AUTHOR="$(repo_owner_login)"
echo "[mention-target] @-mention target = repo owner @$ISSUE_AUTHOR (NOT issue author; stable across bot-created issues)"

ISSUE_HISTORY_TEXT="$(python3 - <<'PY'
import os, sys, json
body = os.environ.get('FIXER_ISSUE_BODY', '')
print('## Issue body')
print(body.strip() if body else '(empty)')
print()
print('## Conversation history (most recent first)')
try:
    cs = json.loads(os.environ.get('FIXER_ALL_COMMENTS_JSON','[]'))
except Exception:
    cs = []
if not cs:
    print('(no comments yet)')
else:
    for c in reversed(cs):
        user = (c.get('user') or {}).get('login','?')
        ts = c.get('created_at','')
        body = (c.get('body') or '').strip()
        if len(body) > 1200:
            body = body[:1200] + '\n…[truncated]'
        print(f'--- @{user} at {ts} ---')
        print(body)
        print()
PY
)"
FIXER_ISSUE_BODY="$ISSUE_BODY" FIXER_ALL_COMMENTS_JSON="$ALL_COMMENTS_JSON" python3 -c "import os; pass" >/dev/null 2>&1
# Note: bash here-doc captures don't pass env through `python3 - <<`,
# so we re-run with the env set explicitly:
ISSUE_HISTORY_TEXT="$(FIXER_ISSUE_BODY="$ISSUE_BODY" FIXER_ALL_COMMENTS_JSON="$ALL_COMMENTS_JSON" python3 - <<'PY'
import os, json
body = os.environ.get('FIXER_ISSUE_BODY', '')
print('## Issue body')
print(body.strip() if body else '(empty)')
print()
print('## Conversation history (oldest first)')
try:
    cs = json.loads(os.environ.get('FIXER_ALL_COMMENTS_JSON','[]'))
except Exception:
    cs = []
if not cs:
    print('(no comments yet)')
else:
    for c in cs:
        user = (c.get('user') or {}).get('login','?')
        ts = c.get('created_at','')
        text = (c.get('body') or '').strip()
        if len(text) > 1200:
            text = text[:1200] + '\n…[truncated]'
        print(f'--- @{user} at {ts} ---')
        print(text)
        print()
PY
)"

# Existing-PRs section for the prompt
EXISTING_PRS_TEXT="$(FIXER_EXISTING_PRS="$EXISTING_PRS_JSON" python3 - <<'PY'
import os, json
try:
    prs = json.loads(os.environ.get('FIXER_EXISTING_PRS','[]'))
except Exception:
    prs = []
if not prs:
    print('(none — you may open a new PR when ready)')
else:
    for p in sorted(prs, key=lambda x: x['number']):
        print(f"- PR #{p['number']} ({p['html_url']}) head_ref=`{p['head_ref']}` — {p['title']}")
PY
)"

# -- session + initial turn -------------------------------------------

SESSION_ID="issue-${REPO//\//-}-${ISSUE_NUM}-$(date +%s)"

# Anchor the comment cursor at the latest existing comment so first
# poll doesn't pick up old ones.
if [ -f "$CURSOR_FILE" ]; then
  LAST_SEEN_ID="$(cat "$CURSOR_FILE")"
  echo "[cursor] resumed from $CURSOR_FILE = $LAST_SEEN_ID"
else
  LAST_SEEN_ID="$(most_recent_comment_id)"
  echo "$LAST_SEEN_ID" > "$CURSOR_FILE"
  echo "[cursor] initialised at $LAST_SEEN_ID"
fi

# -- early-exit gates -------------------------------------------------
# These run BEFORE the initial agent invocation so we don't burn an LLM
# turn just to discover "nothing to do". Every cron tick respawns this
# script for any open assigned issue; we need to be cheap when there's
# no actual new work.

# Gate 1: issue closed → wipe everything and exit. This is the only
# path that triggers WIPE_FULL_STATE (the user's "once an issue is
# finished he can wipe his local memory" — the definitive signal of
# finished is the issue being closed, typically via PR merge).
# $ISSUE_STATE comes from the snapshot taken above — one read of the issue,
# so the gates below cannot disagree with each other about what it says.
if [ "$ISSUE_STATE" = "closed" ]; then
  echo "[$(date -Iseconds)] issue #$ISSUE_NUM is CLOSED — wiping state and exiting"
  WIPE_FULL_STATE=1
  exit 0
fi

# Gate 1.5: Lexical destructive-pattern guard.
# Hard-enforced ASK for issues whose title+body contains destructive
# patterns that the model historically rationalises into shipping
# (rule 14 trigger A/B in the prompt was bypassed in real tests
# even when explicitly listed). This is the wrapper enforcing what
# the LLM couldn't be trusted to enforce.
#
# Fires ONLY when:
#   - No PR exists yet for this issue (we're on the first chance to ask)
#   - The marker file is absent (we haven't already asked for this issue)
# When matched: post a @<repo-owner> ask via the GitHub API, write
# the marker, exit. On the next tick the bot will gate-exit silently
# until the user replies (no PR + no new @-mention to bot). When the
# user @-mentions the bot, the pre-flight gate routes to agent turn
# normally and the agent has the user's clarification in context.
# Marker location: outside the git working tree on the PVC. Anything
# inside PROJECT_DIR gets wiped by the `git clean -fdx` in the
# fresh-branch checkout path above (.43 bug). Use a sibling dir.
LEXICAL_ASKED_MARKER="$STATE_ROOT/issue-markers/${REPO//\//__}-${ISSUE_NUM}.lexical-asked"
mkdir -p "$(dirname "$LEXICAL_ASKED_MARKER")"
if [ -z "$EXISTING_PR_NUMBER" ] && [ ! -f "$LEXICAL_ASKED_MARKER" ]; then
  PATTERN_HIT="$(LEX_TITLE="$ISSUE_TITLE" LEX_BODY="$ISSUE_BODY" python3 <<'PYEOF'
import os, re, sys
title = os.environ.get('LEX_TITLE', '')
body = os.environ.get('LEX_BODY', '')
text = title + '\n' + body

# Trigger A: destructive verb within ~120 chars of a load-bearing noun
DESTRUCTIVE = r'\b(?:remove|delete|disable|drop|strip|kill|turn\s*off|get\s*rid\s*of)\b'
PROTECTED = r'\b(?:tests?|test\s+suite|test\s+files?|snapshots?|jest|lint|eslint|prettier|type[-\s]?check|tsconfig\b[^.]*?strict|mypy[^.]*?strict|ci\s+jobs?|workflows?|coverage|monitor(?:ing)?|logging|tracking|security|auth(?:entication)?|authorization|backups?|rollbacks?)\b'

for m_d in re.finditer(DESTRUCTIVE, text, re.IGNORECASE):
    for m_p in re.finditer(PROTECTED, text, re.IGNORECASE):
        if abs(m_d.start() - m_p.start()) < 120:
            print(f'A:{m_d.group(0).lower()} ... {m_p.group(0).lower()}')
            sys.exit(0)

# Trigger B: "feature flag" / "toggle" near "remove/delete/disable"
# (in either order — flag-then-remove OR remove-then-flag)
FLAG = r'(?:feature[\s-]+flag|toggle)'
FR = re.compile(f'{FLAG}[\\s\\S]{{0,200}}?{DESTRUCTIVE}', re.IGNORECASE)
RF = re.compile(f'{DESTRUCTIVE}[\\s\\S]{{0,200}}?{FLAG}', re.IGNORECASE)
for r in (FR, RF):
    m = r.search(text)
    if m:
        print(f'B:{m.group(0).lower()[:80].replace(chr(10)," ")}')
        sys.exit(0)
PYEOF
)"
  if [ -n "$PATTERN_HIT" ]; then
    echo "[lexical-guard] destructive pattern matched: $PATTERN_HIT — posting ASK and deferring"
    TRIGGER_LABEL="${PATTERN_HIT%%:*}"  # A or B
    MATCH_TEXT="${PATTERN_HIT#*:}"
    if [ "$TRIGGER_LABEL" = "A" ]; then
      ASK_INTRO="The wording matches **rule 14 HARD TRIGGER A** — a destructive verb (remove/delete/disable/...) against a load-bearing system (tests/lint/type-check/CI/coverage/monitoring/security/auth/backups). The matched fragment: \`$MATCH_TEXT\`"
      ASK_QUESTION="Before I proceed, please confirm:
1. **Why** specifically should this be removed? (one concrete consequence — what breaks today that the removal fixes, or what improves measurably?)
2. **Scope** — full removal, or just the parts causing pain? If the latter, which?
3. Are there any **replacement / equivalents** I should add alongside the removal?"
    else
      ASK_INTRO="The wording matches **rule 14 HARD TRIGGER B** — a feature-flag + remove-old combination on a single change. The matched fragment: \`$MATCH_TEXT\`"
      ASK_QUESTION="Adding a feature flag for the NEW thing AND removing the OLD thing in the same PR creates a flag with no fallback. Please confirm:
1. **Both phases at once?** Flag exists but old code is gone → off-state has nothing to render. Is that the intent (a kill-switch with a blank fallback)?
2. **Phase 1 only?** Add the flag with both paths preserved; remove old in a follow-up PR after the flag has shipped safely.
3. **Phase 2 only?** Remove the old code; the new path is unconditional (no flag needed).
4. Something else?"
    fi
    ASK_BODY="@$ISSUE_AUTHOR — I need clarification before writing any code.

$ASK_INTRO

$ASK_QUESTION

Reply with \`@$BOT_LOGIN\` and your choice and I'll proceed."

    # Post via authenticated API; payload built as JSON via python to
    # avoid quoting hell in the curl -d argument.
    REQ_PAYLOAD="$(ASK_BODY="$ASK_BODY" python3 -c 'import os,json,sys;print(json.dumps({"body":os.environ["ASK_BODY"]}))')"
    if curl -fsSL -X POST \
      -H "$AUTH_HEADER" -H "$ACCEPT_HEADER" -H "$APIV_HEADER" \
      -H 'Content-Type: application/json' \
      -d "$REQ_PAYLOAD" \
      "$GH_API/repos/$REPO/issues/$ISSUE_NUM/comments" >/dev/null 2>&1; then
      touch "$LEXICAL_ASKED_MARKER"
      echo "[lexical-guard] ASK posted, marker written, exiting without agent invocation"
      exit 0
    else
      echo "[lexical-guard] WARNING: failed to post ASK comment — falling through to agent invocation"
    fi
  fi
fi

# Gate 1.6: Lexical-asked + no user reply → keep waiting silently.
# After the lexical guard posts the ASK on a fresh issue, every
# subsequent tick that finds the marker present + no new @-mention
# to the bot since cursor must exit silently. Otherwise the wrapper
# falls through to the initial agent invocation, the agent gets the
# untouched (destructive) issue body, and ships destructive work
# despite the ask being posted (.44 bug observed on #54/#33).
if [ -z "$EXISTING_PR_NUMBER" ] && [ -f "$LEXICAL_ASKED_MARKER" ]; then
  POST_ASK_NEW="$(fetch_new_mentions "$LAST_SEEN_ID" 2>/dev/null || echo '[]')"
  POST_ASK_NEW_COUNT="$(echo "$POST_ASK_NEW" | python3 -c 'import sys,json;print(len(json.load(sys.stdin)))')"
  if [ "$POST_ASK_NEW_COUNT" = "0" ]; then
    echo "[lexical-guard] marker present, no new @-mention since cursor=$LAST_SEEN_ID — exiting silently (waiting for user reply)"
    exit 0
  fi
  echo "[lexical-guard] marker present AND $POST_ASK_NEW_COUNT new @-mention(s) since cursor — user replied, proceeding with agent"
  # Pre-react + advance cursor so the initial prompt sees the user's
  # reply consistently and we never re-prompt for the same comment.
  while read -r cid; do
    [ -z "$cid" ] && continue
    react_to_comment "$cid"
    if [ "$cid" -gt "$LAST_SEEN_ID" ]; then
      LAST_SEEN_ID="$cid"
      echo "$cid" > "$CURSOR_FILE"
    fi
  done < <(echo "$POST_ASK_NEW" | python3 -c "
import sys, json
for c in json.load(sys.stdin):
    print(c['id'])
")
fi

# CI-gate enforcement on the existing PR (idempotent): if any check is
# red/pending/missing AND a reviewer is requested, remove the reviewer.
# Runs on every tick so a premature add-reviewer is unwound within ~5
# minutes (current cron schedule).
if [ -n "$EXISTING_PR_NUMBER" ]; then
  enforce_no_reviewer_when_ci_red "$EXISTING_PR_NUMBER"

  # The merge is the wrapper's, not the agent's. Attempted BEFORE the wake
  # gates, because a pull request that can land needs no model at all: checks
  # green, no conflict, not a draft, the autonomous review satisfied and any
  # human sign-off in. Every one of those is a cheap question, and answering
  # them here is what keeps a finished story from costing a turn to discover
  # it was finished.
  if maybe_merge_green_pr "$EXISTING_PR_NUMBER"; then
    echo "[$(date -Iseconds)] PR #$EXISTING_PR_NUMBER merged and #$ISSUE_NUM closed — wiping state and exiting"
    WIPE_FULL_STATE=1
    exit 0
  fi
fi

# Gate 2: PR open. Wake the agent only when there's something for it to
# do; otherwise exit cheaply.
#
# Wake triggers (any one of them):
#   - new @-mention to the bot since cursor (user input)
#   - CI fingerprint on the PR head changed since last seen (CI just
#     settled — agent must react per rule 8 if red, rule 9 if green)
#   - the branch conflicts with the base and the conflict is still
#     unresolved (nothing else can see this one — see conflict_needs_agent)
# In any of those cases, save the new state and fall through to the initial
# agent invocation. Otherwise exit silently.
WAKE_REASON=""
if [ -n "$EXISTING_PR_NUMBER" ]; then
  # A rebase conflict is the third wake trigger, and the one nothing else can
  # see: it is neither a mention nor a CI change, so without this the run
  # exits in seconds having spent nothing while the pull request sits green
  # and unmergeable. The retry budget is what makes it survive a run that died
  # before resolving it — see SYNC_RETRY_FILE.
  if conflict_needs_agent "$EXISTING_PR_NUMBER"; then
    MERGE_CONFLICT_NEW=1
  fi

  # CI_FP_FILE is now defined at the top of the script in
  # $ISSUE_STATE_DIR (.46 fix — was being silently wiped by git clean
  # on every tick when defined inline here pointing into $PROJECT_DIR).
  CURRENT_CI_FP="$(ci_fingerprint_for_pr "$EXISTING_PR_NUMBER" 2>/dev/null || echo unknown)"
  LAST_CI_FP=""
  [ -f "$CI_FP_FILE" ] && LAST_CI_FP="$(cat "$CI_FP_FILE")"

  # Fingerprint policy (token-saver): track every transition on disk
  # but only WAKE the agent on transitions into "settled:*". CI being
  # in_progress means there's nothing actionable yet — no point
  # spending a turn just to be told to wait.
  CI_CHANGED=0
  if [ -n "$CURRENT_CI_FP" ] && [ "$CURRENT_CI_FP" != "unknown" ] && [ "$CURRENT_CI_FP" != "$LAST_CI_FP" ]; then
    echo "$CURRENT_CI_FP" > "$CI_FP_FILE"
    case "$CURRENT_CI_FP" in
      settled:*)
        CI_CHANGED=1
        ;;
      *)
        echo "[preflight] PR #$EXISTING_PR_NUMBER CI fingerprint changed but state is '$CURRENT_CI_FP' (not settled) — tracking but NOT waking agent (saves LLM calls during in-progress phase)"
        ;;
    esac
  fi

  PREFLIGHT_NEW="$(fetch_new_mentions "$LAST_SEEN_ID" 2>/dev/null || echo '[]')"
  PREFLIGHT_NEW_COUNT="$(echo "$PREFLIGHT_NEW" | python3 -c 'import sys,json;print(len(json.load(sys.stdin)))')"

  if [ "$PREFLIGHT_NEW_COUNT" = "0" ] && [ "$CI_CHANGED" = "0" ] && [ "$MERGE_CONFLICT_NEW" = "0" ]; then
    echo "[preflight] PR #$EXISTING_PR_NUMBER open, no new @-mentions since cursor=$LAST_SEEN_ID, CI fingerprint='$CURRENT_CI_FP' unchanged or not settled, no unresolved conflict — exiting without agent invocation"
    exit 0
  fi

  if [ "$MERGE_CONFLICT_NEW" = "1" ]; then
    WAKE_REASON="merge-conflict"
  fi

  if [ "$CI_CHANGED" = "1" ]; then
    echo "[preflight] PR #$EXISTING_PR_NUMBER CI settled: '$LAST_CI_FP' → '$CURRENT_CI_FP' — waking agent (rule 8 if red, rule 9 if green)"
    if [ -n "$WAKE_REASON" ]; then
      WAKE_REASON="${WAKE_REASON}+ci-change"
    else
      WAKE_REASON="ci-change"
    fi
  fi

  if [ "$PREFLIGHT_NEW_COUNT" != "0" ]; then
    echo "[preflight] PR #$EXISTING_PR_NUMBER $PREFLIGHT_NEW_COUNT new @-mention(s) since cursor — waking agent"
    if [ -n "$WAKE_REASON" ]; then
      WAKE_REASON="${WAKE_REASON}+user-mention"
    else
      WAKE_REASON="user-mention"
    fi
  fi

  # Pre-react + advance cursor so the initial prompt below sees them
  # consistently and we never re-prompt for the same comment on a
  # later tick.
  while read -r cid; do
    [ -z "$cid" ] && continue
    react_to_comment "$cid"
    if [ "$cid" -gt "$LAST_SEEN_ID" ]; then
      LAST_SEEN_ID="$cid"
      echo "$cid" > "$CURSOR_FILE"
    fi
  done < <(echo "$PREFLIGHT_NEW" | python3 -c "
import sys, json
for c in json.load(sys.stdin):
    print(c['id'])
")
fi

if [ -n "$EXISTING_PR_NUMBER" ]; then
  BRANCH_INSTRUCTION="**An open PR for this issue already exists: PR #${EXISTING_PR_NUMBER} on branch \`${EXISTING_PR_BRANCH}\` (${EXISTING_PR_URL}).** You have ALREADY checked out that branch. Push any further commits to **this same branch** — do NOT create a new branch, do NOT open a new PR. If the PR needs updates, push commits to ${EXISTING_PR_BRANCH}; the PR will pick them up automatically."
else
  BRANCH_INSTRUCTION="No PR is open for this issue yet. When the work is ready, open ONE PR from branch \`${BRANCH}\` to \`${DEFAULT_BRANCH}\` with \"Closes #${ISSUE_NUM}\" in the body. Do NOT open multiple PRs for the same issue."
fi

# Current CI state on the PR, embedded in the prompt so the agent sees
# failures/successes without having to fetch first. Only meaningful
# when a PR exists.
if [ -n "$EXISTING_PR_NUMBER" ]; then
  CI_STATUS_TEXT="$(ci_summary_text_for_pr "$EXISTING_PR_NUMBER" 2>/dev/null || echo '(could not fetch)')"
  # Also pre-fetch failing-job log excerpts so the agent doesn't have
  # to remember to call github__get_job_logs. Past runs have seen the
  # agent skip the MCP step and post an ASK based on local code
  # inspection alone (rule 8 violation in spirit). Pre-injecting the
  # log makes the failure cause visible without an extra tool call.
  CI_FAILURE_LOGS="$(ci_failing_logs_for_pr "$EXISTING_PR_NUMBER" 2>/dev/null || true)"
  if [ -n "$CI_FAILURE_LOGS" ]; then
    CI_STATUS_TEXT="$CI_STATUS_TEXT

## Failing CI excerpt

The wrapper already fetched the failing job logs via the GitHub API
and condensed them below. **Read this before reasoning about the
failure** — it is the authoritative source. Each section is one
failing job; lines combine pattern-matched failure signals with the
last ~30 lines of the job tail. Use this to identify the root cause
BEFORE deciding whether to push a fix, ASK, or escalate.

$CI_FAILURE_LOGS"
  fi
else
  CI_STATUS_TEXT="(no PR yet — CI not applicable)"
fi

# Why-am-I-awake hint for the agent. The wrapper has already decided
# there's work to do; this just tells the agent why and what to do
# first.
#
# Composed from the parts that actually fired rather than matched against a
# fixed list of combinations: three triggers make seven combinations, and the
# list version silently fell through to "Initial run on this issue" for the
# ones nobody had written out — telling the agent the opposite of the truth.
if [ -z "$WAKE_REASON" ]; then
  WAKE_REASON_TEXT="Initial run on this issue."
else
  WAKE_REASON_TEXT="The wrapper woke you because:"
  case "$WAKE_REASON" in
    *merge-conflict*)
      WAKE_REASON_TEXT="$WAKE_REASON_TEXT
  - **the branch conflicts with \`$DEFAULT_BRANCH\`.** The pull request cannot merge until the conflict is resolved. Rebase or merge the default branch into your branch, resolve every conflicted file, and push. This is the FIRST thing to do — nothing else about this issue can land until it is done." ;;
  esac
  case "$WAKE_REASON" in
    *ci-change*)
      WAKE_REASON_TEXT="$WAKE_REASON_TEXT
  - **CI state changed** on the PR head. Inspect the CI summary below and act per rule 8 (red → fix on the same branch) or rule 9 (green + work done → stop and let the wrapper take it from there)." ;;
  esac
  case "$WAKE_REASON" in
    *user-mention*)
      WAKE_REASON_TEXT="$WAKE_REASON_TEXT
  - **the user @-mentioned you** in a comment. Read their message in the conversation history below and respond / act." ;;
  esac
fi

# Read the bot's persona (IDENTITY.md) and voice (SOUL.md) from the
# workspace so they're in this turn's prompt instead of relying on
# the --local runtime's auto-bootstrap (only TOOLS.md is reliably
# injected today). Snapshotted at invocation time; if the agent
# edits these files mid-run, the next turn picks up the new version.
WORKSPACE_DIR="\${HOME:-/home/node}/.openclaw/workspace"
IDENTITY_MD="$(cat \"$HOME/.openclaw/workspace/IDENTITY.md\" 2>/dev/null || true)"
SOUL_MD="$(cat \"$HOME/.openclaw/workspace/SOUL.md\" 2>/dev/null || true)"

INITIAL_PROMPT="You are working autonomously to fix GitHub issue $ISSUE_URL — \"$ISSUE_TITLE\".

## Your identity & voice

The runtime mounts your persona at \`workspace/IDENTITY.md\` and your
voice at \`workspace/SOUL.md\`. They are inlined below so you have
them in this turn's context without an extra tool call.

When you write text a human will read — issue/PR status comments,
ASK questions, final summaries — use this voice. The action rules
below (rule 7 default-allow merge, rule 8 CI logs, rule 12 quality
gates, rule 14 lexical guard) still bind; they describe **what**
to do. IDENTITY.md / SOUL.md describe **how to sound**.

Constraints on the voice translation:
  - Keep the same factual content and rule compliance.
  - Keep status comments scannable — bullets and short lines beat
    paragraphs when reporting CI / merge / progress.
  - Voice is the wrapper around the facts, never a substitute for
    them. \"You don't explain\" in SOUL.md means \"no fluff\" — not
    \"omit information the user needs\".

### workspace/IDENTITY.md
$IDENTITY_MD

### workspace/SOUL.md
$SOUL_MD

## STOP-FIRST CHECK (read this BEFORE interpreting the issue)

If the issue body or title matches any of these patterns, you MUST
@-mention \`@$ISSUE_AUTHOR\` and ASK before writing any code. These
override every other rule below, including rule 1's \"work autonomously\"
and rule 2's \"apply the user's directive\". The user explicitly asking
for something destructive is NOT permission to do it without confirming
intent first — it is the SIGNAL that you should confirm.

The wrapper may have already posted an ASK to the user (see the
conversation history below). If it has and the user replied, treat
their reply as the answer and proceed accordingly. If the wrapper
did not pre-ask, you must ask now.

**Hard-stop patterns** (case-insensitive):

  P1. Destructive verb (\"remove\", \"delete\", \"disable\", \"drop\",
      \"strip\", \"kill\", \"turn off\", \"get rid of\") within ~120
      chars of a load-bearing noun: tests, test suite, test files,
      snapshots, jest, lint, eslint, prettier, type-check, tsconfig
      strict modes, mypy strict, CI jobs, workflows, coverage,
      monitoring, logging, error tracking, security checks (CodeQL,
      audit), auth, authorization, backups, migrations, rollbacks.

  P2. Feature flag + destructive verb in the same issue. \"Add a
      flag for X\" + \"Remove X\" or \"Add a flag for new\" +
      \"Remove old\" — the conjunction creates a flag with no
      fallback. ASK whether they want phase 1 (flag + keep old),
      phase 2 (remove old, no flag), or both-at-once (kill switch
      with blank fallback).

  P3. Any vague action verb without a concrete target: \"clean up\",
      \"improve\", \"make X better/faster/safer\", \"refactor\" with
      no scope. ASK for the specific behaviour change and success
      criteria.

  P4. The issue body's justification reads like a pre-regret
      rationale: \"they slow down the build\", \"we don't need them
      anymore\", \"it's just legacy code\", \"clean it up\" —
      attached to a destructive directive. ASK for one concrete
      consequence.

If ANY of the above matches → STOP, post the ASK comment with
\`@$ISSUE_AUTHOR\`, and END YOUR TURN. Do not start a branch. Do
not write code. The cost of asking once is small; the cost of
shipping the wrong destruction is large.

If NONE of the above matches → continue to the rest of the prompt.

---

You are in a checkout of $REPO at $(pwd) on branch $BRANCH (off $DEFAULT_BRANCH). The git author identity is \`$BOT_LOGIN\` (resolved at runtime from \$GITHUB_TOKEN). You have the github MCP server available.

## Why you're awake right now

$WAKE_REASON_TEXT

## What has already been said on the issue

$ISSUE_HISTORY_TEXT

## Currently-open PRs linked to this issue

$EXISTING_PRS_TEXT

## Current CI state on the PR head

$CI_STATUS_TEXT

## Branch policy — STRICT

$BRANCH_INSTRUCTION

## Protocol — follow this exactly

1. **Read the conversation history above.** Continue from where the
   previous turns left off. Do NOT post \"🚧 Starting work\" or
   similar if a previous status already says you are working — the
   user is reading these in a notification feed and duplicates are
   annoying.

2. **If the most recent user comment includes a directive or
   correction**, apply it. If the answer was given to a question you
   previously asked, use that answer.

3. **Work as autonomously as possible.** Read the codebase,
   implement the change, run tests if any exist, commit, push the
   branch indicated above. Do not delegate to subagents.

4. **Status comments**: post AT MOST one short status comment per
   meaningful state transition (started / blocked / pushed / done).
   Never repeat a status that's already in the history. One line.

5. **Asking the user is the LAST resort.** Before posting any
   question, exhaust your toolbox:
     - Use every relevant MCP server (github, k8s, terraform,
       aws/gcp/aliyun, debug, etc.). Read tool descriptions if
       you're unsure what's available — \`/tools\`-style listings
       are free, and the github MCP alone has ~30 actions.
     - Try the action, even if you're not 100% sure of the
       arguments. A failed call gives you an error message you
       can debug. Hesitation is more expensive than experiment.
     - If a tool you'd normally reach for isn't wired, look for
       a CLI substitute (\`gh\`, \`kubectl\`, \`terraform\`, \`az\`,
       \`aws\`, \`gcloud\`, \`aliyun\`, \`git\`). They're all on
       PATH.
     - Read the codebase, the failing CI logs, the issue's
       linked PRs, neighbouring docs. The answer is usually
       already written down somewhere.
   ONLY when you definitively know a blocker is a setting or
   permission you cannot change (e.g., a missing GitHub
   environment secret in someone else's account, a missing
   federated identity in Azure Entra, a feature flag you don't
   own) — THEN post ONE comment on the issue tagging
   \`@$ISSUE_AUTHOR\` with a specific, actionable question:
   what's blocked, what you tried, what setting you need
   changed. Then stop your turn. A wrapper polls for the
   reply; when the user answers (by tagging you @$BOT_LOGIN),
   you'll be re-invoked in the same session with their reply
   as the next user message.

6. **When you finish**: ensure there is exactly ONE open PR for
   this issue. Post a final status comment on the issue with the
   PR link. Then stop. Do not open additional PRs even if you
   think the previous one is wrong — push commits to it instead.

7. **Do NOT merge, and do NOT close anything. The wrapper does
   that.** Your deliverable is a pull request that is ready to
   land: the work committed, pushed, and CI green. Stop there.

   The wrapper merges it on a later tick, once — in this order —
   every check on the head is green, the branch does not conflict
   with the base, the PR is not a draft, the autonomous reviewer
   has posted an APPROVED verdict for that exact head commit, and
   any human sign-off the issue asks for (the \`approval\` label)
   has been given. Then the wrapper closes this issue with the
   \`completed\` reason, which is what records it as delivered.

   Why not you: those gates are cheap facts, checked in seconds
   without a model call, and a gate that can be reasoned past is
   not a gate. Merging yourself skips the review and the sign-off
   the repo owner asked for.

   So: never call \`merge_pull_request\`, \`gh pr merge\`,
   \`gh pr close\` / \`close_pull_request\`, \`gh issue close\` /
   \`close_issue\`. If you believe the PR is ready and the wrapper
   has not merged it, say so in one status comment naming what you
   think is blocking — do not act on it yourself.

   If CI is red, follow rule 8 instead. If the branch conflicts
   with the base branch, resolving that conflict IS your task for
   this turn — the wrapper cannot merge until it is gone.

8. **If CI on the PR fails, fix it on the same branch.** Read the
   actual failing job logs FIRST — guessing from the workflow YAML
   or your local code inspection wastes turns. The wrapper has
   ALREADY fetched the failing-job log excerpt and injected it into
   this prompt under \"## Failing CI excerpt\" — read THAT before
   anything else. If for some reason that section is missing (only
   happens when the GH API was unreachable when the wrapper ran),
   fall back to the **github MCP** to fetch logs yourself:
   \`github__list_workflow_runs\`, then
   \`github__list_workflow_jobs\` →
   \`github__get_job_logs\` /
   \`github__download_workflow_run_logs\`.

   **Hard rule** — you may NOT post an ASK about a CI failure
   (e.g. \"can you share the job log?\", \"which test is failing?\")
   until you have actually consulted the failing-job log lines. The
   wrapper already gave them to you in this prompt; ignoring that
   evidence and asking the user to copy-paste it back is a turn
   waste. If the excerpt is present, base your diagnosis on its
   contents. If it is missing AND github MCP also fails to return
   logs, mention that explicitly in your status comment (so the
   user knows the issue is API access, not your reasoning).

   **The github MCP is the ONLY authenticated path to GitHub from
   inside the agent.** Every other channel — \`gh\` CLI, bare
   \`curl\`, and \`web_fetch\` — runs without \$GITHUB_TOKEN
   (the openclaw exec tool sanitizes the token from subprocesses)
   and will fail predictably:
     - \`gh ...\` → \"please run gh auth login\"
     - \`curl -H \"Authorization: Bearer \$GITHUB_TOKEN\" ...\` →
       401 / empty (token is gone in exec)
     - \`web_fetch https://api.github.com/...\` → 403
       \"Must have admin rights to Repository\" (unauthenticated)
     - \`web_fetch https://github.com/.../actions/runs/...\` →
       404 \"Page not found\" (logged-out HTML wall)
   When the agent finds itself reaching for any of these against
   a github.com URL: STOP, use the github MCP instead. Re-trying
   the same unauthenticated path is the most common LLM-time
   waste in this codebase.

   Once you have the actual error, diagnose the root cause, push a
   fix commit to the SAME branch. Post a one-line status comment
   naming the failing job + root cause. Do NOT open a new PR, do
   NOT close the existing one, and do NOT declare the issue done
   while CI is red — wait for the next push to go green, then post
   the final status (and merge per rule 7's default-allow path).

   The wrapper's pre-flight gate already waits for CI to settle
   before waking you again on the next tick — you don't need to
   poll CI yourself inside the same turn. Make your fix, push, and
   stop. The next tick will pick up the new CI result.

9. **Reviewer assignment — STRICT, ENFORCED BY THE WRAPPER.**
   NEVER request a reviewer while ANY CI check on the PR head is
   queued, in_progress, pending, or has conclusion != success.
   The wrapper checks CI on every tick and will IMMEDIATELY
   REMOVE any reviewer you add while CI is not all-green —
   adding one early is wasted effort and annoys the user.

   When you need user input because you're blocked on a setting
   they own (Azure cred, GitHub secret, etc.) → **COMMENT on
   the issue with @<user> mention**. Do NOT request them as
   reviewer; the two channels are different. Mention-in-comment
   = \"I'm blocked, please help\". Reviewer-request = \"this is
   done, please review\".

   In the ordinary case you do not request a reviewer at all: the
   wrapper asks the autonomous reviewer itself once the head is
   green, and it does that per head commit so the request always
   names the code that is actually up for review.

   Only call \`request_reviewers\` / \`gh pr edit --add-reviewer\`
   yourself when ALL of the following are true:
     (a) every check-run on the PR head has conclusion=success,
     (b) the PR is the final deliverable (no more commits planned),
     (c) the issue body names a specific HUMAN reviewer it wants.
   In that case use the person the issue names. Otherwise leave
   the reviewer field alone.

10. **Reactions:** do NOT add reactions yourself. The wrapper
    handles marking comments as read with :+1: after each poll.

11. **Empty-PR is a signal to ASK, not to declare done.** If you
    find yourself about to push a commit that, combined with the
    PR's prior commits, results in a **net-zero diff** vs the base
    branch (i.e. you've effectively undone your own earlier work
    in this PR), STOP. Do not push. Do not request review on an
    empty PR. That state is a strong signal that you misread the
    issue and need clarification.

    Instead, post ONE comment on the issue tagging
    \`@$ISSUE_AUTHOR\` summarising:
      - What you initially understood the task to be
      - What you discovered (e.g. \"found existing X in Y.yaml
        that already does this\")
      - The specific clarifying question (e.g. \"should I add
        a separate Z job to ci.yml, or is the existing Y.yaml
        flow what you intended?\")
    Then stop your turn. The wrapper will let you reply when
    the user answers.

    Quick self-check before pushing the final commit: run
    \`git diff --stat origin/<default-branch>...HEAD\` — if
    that is empty, you are in the empty-PR case.

12. **NEVER lower a test/quality threshold to make CI pass.** If a
    coverage check, lint rule, type-check, mutation score, flaky-
    retry budget, snapshot, or similar guardrail is failing, the
    fix is to **raise the code or the tests to meet the threshold**
    — NOT to lower the threshold so the failing measurement passes.

    Specifically forbidden when CI is red on a quality gate:
      - Reducing a coverage threshold (e.g. 80 → 70, or removing
        \`--fail-under\` / \`coverageThreshold\` entries)
      - Adding files/paths to a coverage \`omit\`/\`exclude\` list
        purely to dodge a failure
      - Downgrading lint rules from error to warning (or to off),
        adding \`// eslint-disable\`, \`# noqa\`, \`@ts-ignore\`,
        \`# type: ignore\` to silence the failing check
      - Deleting / skipping / \`xit\`-ing / \`@pytest.mark.skip\`-ing
        a failing test
      - Loosening type-check strictness (\`tsconfig.json strict\`,
        \`mypy --strict\`, etc.)
      - Updating a snapshot file just because it diverged, without
        verifying the new output is actually correct
      - **Editing any file under \`.github/workflows/\*\*\` to make
        a gate non-fatal.** This includes — but is not limited to —
        replacing a quality-gate command with the same command +
        \`|| true\`, swapping a strict invocation
        (e.g. \`npm run check-coverage\`) for a permissive one
        (\`nyc check-coverage --check-coverage=false\`), removing a
        gate step entirely, adding \`continue-on-error: true\` to a
        previously-strict job, or moving a check into a non-required
        job. If you find yourself opening a workflow file because a
        check is failing, STOP — that is rule 5 LAST-RESORT
        territory, not a code change.

    The correct response is one of:
      (a) Improve an existing test so it actually covers the new
          code path / catches the new bug
      (b) Add NEW tests covering the previously-uncovered lines
      (c) Refactor the production code to be more testable, then
          add tests
      (d) If the threshold itself is genuinely wrong (e.g. it was
          set arbitrarily and the team agreed to relax it), that
          is a SEPARATE conversation — @-mention the repo owner
          per rule 5 with the concrete numbers and your reasoning.
          Do NOT lower it as a side effect of fixing an unrelated
          PR.

    If your only path to green CI is lowering a threshold, that
    is a strong signal you are in the rule-5 LAST-RESORT case —
    ASK the repo owner instead of silently weakening the gate.

13. **ASK BEFORE writing code that depends on values you cannot
    derive from the repository.** If the task requires *any*
    identifier, name, secret, URL, or credential you would have
    to invent or leave as a placeholder, STOP writing code and
    @-mention \`@$ISSUE_AUTHOR\` first with the concrete list of
    unknowns.

    Common examples that trigger this rule:
      - Cloud resource identifiers: Azure subscription / tenant /
        resource group / container app / app registration; AWS
        account ID / region / ECR repo / cluster name; GCP project
        / location / service account
      - GitHub Actions secret/variable names you expect to exist
        (e.g. \`AZURE_CLIENT_ID\`, \`SLACK_WEBHOOK_URL\`,
        \`STRIPE_API_KEY\`)
      - Federated identity subjects, OAuth client IDs,
        DNS records, custom domains, webhook URLs
      - Third-party tokens / API keys (Sentry DSN, Datadog API
        key, etc.)
      - Internal references in the issue body (RFC numbers,
        Figma links, Confluence pages, ticket IDs) whose
        content the bot cannot fetch

    The self-check: if your draft code, workflow, or config
    would contain a literal \`<REPLACE-ME>\`, a non-derived
    environment-variable reference, or a documentation paragraph
    explaining what the user must set up before this works,
    that is a rule-13 ASK situation — NOT a deliverable.

    Specifically: do NOT use the PR description or a status
    comment as a substitute for asking. \`See the PR description
    for the list of required secrets\` is a deferred failure, not
    a question. A future CI run will fail when those secrets are
    missing and you will have wasted a turn. Ask first.

    The right shape of the ASK comment:
      @<author> Before I implement <X>, I need:
        - <unknown 1> (what is it / where do I find it?)
        - <unknown 2>
        - ...
      Or: confirm I should use defaults <D1>, <D2> and you will
      wire up <Y> after merge.

    Then stop your turn. The wrapper will let you reply when the
    user answers.

14. **If the intent is ambiguous, contradictory, or surprising,
    ASK before acting.** The bot is good at executing well-scoped
    tasks; it is bad at silently picking between equally-valid
    interpretations. When the issue body could reasonably be read
    in more than one way, or when fulfilling the literal request
    would conflict with conventions in this repo / produce a
    surprising result, do NOT pick an interpretation and proceed
    — @-mention \`@$ISSUE_AUTHOR\` with the specific ambiguity.

    Triggers that should make you stop and ask:
      - Vague action verbs with no concrete target: \"improve\",
        \"fix\", \"clean up\", \"make X better/faster/safer\",
        \"update\" — ask what specifically and what success
        looks like
      - Conflicting requirements: the request would break an
        existing test, lower an existing guarantee, contradict
        an existing pattern, or undo recent work
      - Unusual / anti-pattern-looking asks: a request that, at
        face value, looks like it would damage the codebase
        (e.g., disable a safety check, introduce a known
        anti-pattern, ship something obviously broken) — do
        NOT assume malice or stupidity; assume the user has a
        non-obvious reason and ask what it is
      - Multiple equally-defensible scopes: \"add tests\" could
        mean unit, integration, E2E, snapshot, mutation,
        property-based — ask which
      - Implicit choices you cannot derive: choice of library /
        framework / language / pattern when more than one is
        plausible
      - Anything you find yourself rationalising in your own
        chain-of-thought as \"I'll just assume they meant Y\" —
        that rationalisation is the signal you should ask instead

    The cost of asking once is one short comment + a wait for
    reply. The cost of guessing wrong is: a PR that misses the
    point, a CI cycle (or several) that does not measure what
    the user wanted, and a round of cleanup. Asking is cheaper
    when in doubt.

    **HARD ASK TRIGGERS — these override the counter-rule below.**
    When any of these patterns appears in the issue body, you must
    @-mention and ask before writing code. Do NOT rationalise. Do
    NOT resolve creatively. The pattern itself is the signal.

    Trigger A — destructive verb against a load-bearing system.
    The issue says \"remove\", \"delete\", \"disable\", \"drop\",
    \"strip\", \"turn off\", or \"get rid of\" + one of:
      - tests / test suite / test files / snapshots
      - lint config / eslint rules / prettier / formatter
      - type-checking / tsconfig strict modes / mypy strict
      - CI jobs / GitHub Actions workflows / build steps
      - coverage thresholds (also covered by rule 12)
      - monitoring / logging / error tracking / telemetry
      - security checks (CodeQL, deps audit, secret scanning)
      - authentication / authorization / access controls
      - backups / migrations / rollback paths / safety nets
    ASK what specifically goes wrong if the gate stays. The user
    may have a real reason — document it BEFORE executing. Do not
    accept \"they slow us down\" or \"we don't need them anymore\"
    at face value; ask for the concrete reason.

    Trigger B — literal conflicting verb pair on the same target.
    The issue body contains two operations that directly oppose
    each other on the same noun. Examples:
      - \"Add a feature flag for X\" + \"Remove X\"
        (the flag becomes useless after removal)
      - \"Migrate from X to Y\" + \"Keep X working\"
      - \"Disable X\" + \"Use X for Z\"
      - \"Add X\" and later in the same body \"Remove X\"
    Do NOT resolve creatively (e.g. by making one side a no-op
    fallback). ASK which phase the user wants now — both halves
    are valid in isolation; the user picked the conjunction for
    a reason.

    Trigger C — justification reads like a soft-rationale for
    a destructive change. Phrases like:
      - \"They slow down the build\"
      - \"We don't need them anymore\"
      - \"It's just legacy code\"
      - \"Make it simpler\"
      - \"Clean it up\"
    ...attached to a destructive directive (rule-14 trigger A or
    rule-12 territory). These are sometimes valid; but they are
    also what someone says before regretting a deletion. ASK for
    one concrete consequence: what breaks / what improves
    measurably / who asked for this?

    Counter-rule (to keep this from getting noisy, AND ONLY when
    no hard trigger above fires): do NOT ask about details a
    competent engineer in this repo would not bother clarifying
    — file naming, internal helper names, minor refactor style,
    where to place a new file when the convention is obvious
    from neighbouring files, etc. Make those calls yourself.

Begin."

# -- pickup ------------------------------------------------------------
# Everything above decided there IS work to do. Saying so on the issue is what
# makes the board readable: "nobody has started" and "a run is in flight" are
# different answers, and a planner that cannot tell them apart re-plans work
# that is already moving.
#
# Guarded against a closed issue by set_issue_status itself — a status write
# that reopens an issue a human just closed is the one mistake here that is
# both silent and repeated every five minutes.
set_issue_status "in progress"

# Concurrency gate — take a slot immediately before the model, never earlier.
# Holding one through the clone, the checkout and the API round trips would
# starve the reviewer and the tester for work that never touches a model.
#
# Yielding costs nothing: no cursor is advanced and no status is undone, so
# the next tick picks this issue up exactly where it is.
if command -v acquire_agent_slot >/dev/null 2>&1; then
  if ! acquire_agent_slot; then
    echo "[slot] no agent slot free — yielding this tick (nothing recorded, next tick retries)"
    exit 0
  fi
fi

echo "[turn 1] initial agent invocation"
openclaw agent --local \
  "${AGENT_MODEL_ARGS[@]}" \
  --timeout "$AGENT_TURN_TIMEOUT" \
  --session-id "$SESSION_ID" \
  --message "$INITIAL_PROMPT" || echo "[agent] turn 1 exited non-zero ($?) — continuing into poll loop"

# -- poll loop --------------------------------------------------------

START_TIME=$(date +%s)
turn=2

while :; do
  elapsed=$(( $(date +%s) - START_TIME ))
  if [ "$elapsed" -ge "$MAX_LIFETIME_SECONDS" ]; then
    echo "[$(date -Iseconds)] max lifetime reached — exiting"
    break
  fi

  # Exit when the issue is closed (full wipe) or when any open PR
  # linked to this issue exists (preserve cursor for next tick).
  CUR_ISSUE_STATE="$(fetch_issue_state 2>/dev/null || echo open)"
  if [ "$CUR_ISSUE_STATE" = "closed" ]; then
    echo "[$(date -Iseconds)] issue #$ISSUE_NUM closed — wiping state and exiting"
    WIPE_FULL_STATE=1
    break
  fi

  CUR_PRS="$(fetch_open_prs_for_issue 2>/dev/null || echo '[]')"
  CUR_PR_COUNT="$(echo "$CUR_PRS" | python3 -c 'import sys,json;print(len(json.load(sys.stdin)))')"
  if [ "$CUR_PR_COUNT" -ge 1 ]; then
    echo "[$(date -Iseconds)] open PR exists for issue #$ISSUE_NUM — exiting (cursor preserved)"
    echo "[pr] $(echo "$CUR_PRS" | python3 -c "import sys,json; nums=[p['number'] for p in json.load(sys.stdin)]; print(','.join('#%d' % n for n in nums))")"
    break
  fi

  echo "[poll] sleeping $POLL_INTERVAL s (last_seen=$LAST_SEEN_ID)"
  sleep "$POLL_INTERVAL"

  # Re-check before doing more work.
  CUR_ISSUE_STATE="$(fetch_issue_state 2>/dev/null || echo open)"
  if [ "$CUR_ISSUE_STATE" = "closed" ]; then
    echo "[$(date -Iseconds)] issue #$ISSUE_NUM closed during sleep — wiping state and exiting"
    WIPE_FULL_STATE=1
    break
  fi
  CUR_PRS="$(fetch_open_prs_for_issue 2>/dev/null || echo '[]')"
  CUR_PR_COUNT="$(echo "$CUR_PRS" | python3 -c 'import sys,json;print(len(json.load(sys.stdin)))')"
  if [ "$CUR_PR_COUNT" -ge 1 ]; then
    echo "[$(date -Iseconds)] open PR appeared during sleep — exiting (cursor preserved)"
    break
  fi

  NEW_JSON="$(fetch_new_mentions "$LAST_SEEN_ID" 2>/dev/null || echo '[]')"
  if [ -z "$NEW_JSON" ] || [ "$NEW_JSON" = "[]" ]; then
    echo "[poll] no new @-mention comments"
    continue
  fi

  while read -r cid; do
    [ -z "$cid" ] && continue
    react_to_comment "$cid"
    if [ "$cid" -gt "$LAST_SEEN_ID" ]; then
      LAST_SEEN_ID="$cid"
      echo "$cid" > "$CURSOR_FILE"
    fi
  done < <(echo "$NEW_JSON" | python3 -c "
import sys, json
for c in json.load(sys.stdin):
    print(c['id'])
")

  FOLLOWUP_PROMPT="New comments on issue #$ISSUE_NUM in which you are mentioned:

$(echo "$NEW_JSON" | python3 -c "
import sys, json
for c in json.load(sys.stdin):
    print(f'--- @{c[\"user\"]}: ---')
    print(c['body'])
    print()
")

Apply this guidance and continue from where you left off. Push commits to the branch you have checked out (\`$BRANCH\`); do not open a new PR — if a PR is already open, push to its branch."

  echo "[turn $turn] re-invoking agent"
  openclaw agent --local \
    "${AGENT_MODEL_ARGS[@]}" \
    --timeout "$AGENT_TURN_TIMEOUT" \
    --session-id "$SESSION_ID" \
    --message "$FOLLOWUP_PROMPT" || echo "[agent] turn $turn exited non-zero ($?) — continuing"

  # Re-run the CI-gate enforcement after the agent turn — catches the
  # case where this turn called request_reviewers despite CI still red.
  POST_TURN_PRS="$(fetch_open_prs_for_issue 2>/dev/null || echo '[]')"
  POST_TURN_PR_NUM="$(echo "$POST_TURN_PRS" | python3 -c "import sys,json; ps=sorted(json.load(sys.stdin), key=lambda p: p['number']); print(ps[0]['number'] if ps else '')")"
  if [ -n "$POST_TURN_PR_NUM" ]; then
    enforce_no_reviewer_when_ci_red "$POST_TURN_PR_NUM"
  fi

  turn=$(( turn + 1 ))
done

echo "[$(date -Iseconds)] fixer exit  repo=$REPO  issue=#$ISSUE_NUM  turns=$(( turn - 1 ))"
