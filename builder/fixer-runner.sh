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
#   FIXER_BOT_LOGIN         — bot's GH login (default: cameron-claw)
#   FIXER_POLL_INTERVAL     — seconds between comment polls (default 300)
#   FIXER_MAX_LIFETIME      — overall wall-clock cap, seconds (default 6h)
set -uo pipefail

REPO="$1"
ISSUE_NUM="$2"
ISSUE_URL="$3"
ISSUE_TITLE="$4"

BOT_LOGIN="${FIXER_BOT_LOGIN:-cameron-claw}"
POLL_INTERVAL="${FIXER_POLL_INTERVAL:-300}"
MAX_LIFETIME_SECONDS="${FIXER_MAX_LIFETIME:-$((6 * 3600))}"
AGENT_TURN_TIMEOUT=3500

STATE_ROOT="${HOME:-/home/node}/.openclaw"
PROJECTS_ROOT="$STATE_ROOT/projects"
PROJECT_DIR="$PROJECTS_ROOT/$REPO"
LOCK_ROOT="$STATE_ROOT/.fixer-locks"
LOCK_DIR="$LOCK_ROOT/${REPO//\//__}"
LOG_DIR="$STATE_ROOT/fixer-logs"
LOG_FILE="$LOG_DIR/${REPO//\//_}-${ISSUE_NUM}.log"
CURSOR_FILE="$PROJECT_DIR/.issue-${ISSUE_NUM}.cursor"

mkdir -p "$LOG_DIR" "$LOCK_ROOT" "$(dirname "$PROJECT_DIR")"

if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  echo "[$(date -Iseconds)] lock held for $REPO; aborting fixer for #$ISSUE_NUM" >> "$LOG_FILE"
  exit 0
fi
echo "$BASHPID $(date -Iseconds) issue=$ISSUE_NUM" > "$LOCK_DIR/owner"
trap 'rm -rf "$LOCK_DIR"' EXIT

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

if [ -n "$EXISTING_PR_NUMBER" ]; then
  BRANCH_INSTRUCTION="**An open PR for this issue already exists: PR #${EXISTING_PR_NUMBER} on branch \`${EXISTING_PR_BRANCH}\` (${EXISTING_PR_URL}).** You have ALREADY checked out that branch. Push any further commits to **this same branch** — do NOT create a new branch, do NOT open a new PR. If the PR needs updates, push commits to ${EXISTING_PR_BRANCH}; the PR will pick them up automatically."
else
  BRANCH_INSTRUCTION="No PR is open for this issue yet. When the work is ready, open ONE PR from branch \`${BRANCH}\` to \`${DEFAULT_BRANCH}\` with \"Closes #${ISSUE_NUM}\" in the body. Do NOT open multiple PRs for the same issue."
fi

INITIAL_PROMPT="You are working autonomously to fix GitHub issue $ISSUE_URL — \"$ISSUE_TITLE\".

You are in a checkout of $REPO at $(pwd) on branch $BRANCH (off $DEFAULT_BRANCH). The git author identity is cameron-claw (via \$GITHUB_TOKEN). You have the github MCP server available.

## What has already been said on the issue

$ISSUE_HISTORY_TEXT

## Currently-open PRs linked to this issue

$EXISTING_PRS_TEXT

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

5. **If you get blocked** — i.e., the issue is genuinely ambiguous
   and you'd be guessing — DO NOT guess. Post ONE comment on the
   issue tagging \`@kstrassheim\` with a SPECIFIC question, then
   stop your turn. A wrapper polls for the reply; when the user
   answers (by tagging you @$BOT_LOGIN), you'll be re-invoked in
   the same session with their reply as the next user message.

6. **When you finish**: ensure there is exactly ONE open PR for
   this issue. Post a final status comment on the issue with the
   PR link. Then stop. Do not open additional PRs even if you
   think the previous one is wrong — push commits to it instead.

7. **Do NOT merge or close the PR — UNLESS the issue body
   explicitly says you may.** Default: stop at \"PR opened and CI
   green\". The user reviews and merges. The bot account
   (@$BOT_LOGIN) is NOT in the branch-protection bypass list, so
   an unauthorized merge attempt will fail anyway. Specifically:
     - Do not call \`gh pr merge\`, \`merge_pull_request\`, or any
       MCP tool that merges.
     - Do not call \`gh pr close\` or \`close_pull_request\`.
     - Do not call \`gh issue close\` or \`close_issue\`.
   **Exception** — the issue body grants explicit self-merge
   permission. Look for phrases like \"merge yourself\", \"feel
   free to merge\", \"auto-merge\", \"you may merge when CI
   passes\", or equivalent. If you find one, you MAY call
   \`merge_pull_request\` once ALL required CI checks are green.
   Be conservative: if it's ambiguous, default to \"do not merge\"
   and ask in a comment.

8. **If CI on the PR fails, fix it on the same branch.** Read the
   failing job logs (\`gh run view --log\` / \`gh api ...checks\`),
   diagnose the root cause, push a fix commit to the SAME branch.
   Post a one-line status comment naming the failing job + root
   cause. Do NOT open a new PR, do NOT close the existing one,
   and do NOT declare the issue done while CI is red — wait for
   the next push to go green, then post the final status (or
   merge, if rule 7's exception applies).

9. **Reviewer assignment.** Only when rule 7's exception does NOT
   apply (i.e., you will NOT self-merge): add the issue author as
   reviewer on PR creation (\`gh pr create --reviewer ...\` or the
   github MCP reviewers field). If the issue body or a user
   comment names a specific reviewer, use that instead. If
   self-merge IS allowed, do not add a reviewer — no human needs
   to be paged.

10. **Reactions:** do NOT add reactions yourself. The wrapper
    handles marking comments as read with :+1: after each poll.

Begin."

echo "[turn 1] initial agent invocation"
openclaw agent --local \
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

  # Exit when there's any open PR linked to this issue.
  CUR_PRS="$(fetch_open_prs_for_issue 2>/dev/null || echo '[]')"
  CUR_PR_COUNT="$(echo "$CUR_PRS" | python3 -c 'import sys,json;print(len(json.load(sys.stdin)))')"
  if [ "$CUR_PR_COUNT" -ge 1 ]; then
    echo "[$(date -Iseconds)] open PR exists for issue #$ISSUE_NUM — exiting"
    echo "[pr] $(echo "$CUR_PRS" | python3 -c "import sys,json; [print(f\"#{p[\\\"number\\\"]} {p[\\\"html_url\\\"]}\") for p in json.load(sys.stdin)]")"
    break
  fi

  echo "[poll] sleeping $POLL_INTERVAL s (last_seen=$LAST_SEEN_ID)"
  sleep "$POLL_INTERVAL"

  # Re-check before doing more work.
  CUR_PRS="$(fetch_open_prs_for_issue 2>/dev/null || echo '[]')"
  CUR_PR_COUNT="$(echo "$CUR_PRS" | python3 -c 'import sys,json;print(len(json.load(sys.stdin)))')"
  if [ "$CUR_PR_COUNT" -ge 1 ]; then
    echo "[$(date -Iseconds)] open PR appeared during sleep — exiting"
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
    --timeout "$AGENT_TURN_TIMEOUT" \
    --session-id "$SESSION_ID" \
    --message "$FOLLOWUP_PROMPT" || echo "[agent] turn $turn exited non-zero ($?) — continuing"

  turn=$(( turn + 1 ))
done

echo "[$(date -Iseconds)] fixer exit  repo=$REPO  issue=#$ISSUE_NUM  turns=$(( turn - 1 ))"
