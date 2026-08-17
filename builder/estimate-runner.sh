#!/bin/bash
# estimate-runner: size ONE GitHub issue in story points.
#
# WHY THIS IS ITS OWN RUNNER
# --------------------------
# The size has to exist BEFORE the solver starts, because the solver picks its
# model from it. Sizing inside the solver run would be circular — the model
# would already have been chosen by the time the number appeared.
#
# It also runs on its OWN model key (`planning`). Estimation is cheap, frequent
# and needs judgement rather than capability, and it must keep working when the
# implementation model's quota is spent. Folding it into `solver` would tie the
# two together in exactly the situation where being able to plan matters most.
#
# ONE TURN, NO CHECKOUT
# ---------------------
# Deliberately the lightest runner here: no clone, no branch, no poll loop. It
# reads the issue text and answers with a number. A checkout would roughly
# double the cost of a job whose entire output is one Fibonacci value, and the
# estimate it produced would still be a guess — that is what estimates are.
#
# The measured cost of the work lands separately, from the solver's own run
# log, and story_points.drift() compares the two. The estimator is corrected by
# that loop, not by reading more up front.
#
# WHAT IT WRITES
# --------------
#   - the `SP::<n>` label, and removes any other size label (GitHub does not
#     enforce one-value-per-scope, so story_estimate works out the removals and
#     this script applies every one of them)
#   - removes the `estimate` request label, so the ask does not repeat
#   - one short comment stating the number and its basis
#
# It writes nothing else. A model pin, an approval gate and a `next sprint`
# deferral are human instructions that outlive an estimate; see story_estimate.
#
# Usage: estimate-runner.sh <owner/repo> <issue-number>
#
# Required env:
#   GITHUB_TOKEN            — bot's PAT (already on the openclaw pod)
#
# Optional env:
#   ESTIMATE_AGENT_TURN_TIMEOUT   seconds for the one turn (default 420)
#   ESTIMATE_FORCE=1              size it even if it already has a number
set -uo pipefail

REPO="${1:?repo (owner/name) required}"
ISSUE_NUM="${2:?issue number required}"

# The builder units are installed FLAT next to this script (/usr/local/bin in
# the image), so the script's own directory is where the shell libraries and
# the Python modules live. Resolved rather than hardcoded so a copy running
# from a checkout finds its siblings instead of silently falling back to a
# different deployment's version of them.
LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd)"
[ -r "$LIB_DIR/agent-models.sh" ] || LIB_DIR="/usr/local/bin"
export PYTHONPATH="$LIB_DIR:${PYTHONPATH:-}"

GH_API="https://api.github.com"
AUTH_HEADER="Authorization: Bearer ${GITHUB_TOKEN:-}"
ACCEPT_HEADER="Accept: application/vnd.github+json"
APIV_HEADER="X-GitHub-Api-Version: 2022-11-28"

STATE_ROOT="${HOME:-/home/node}/.openclaw"
LOG_DIR="$STATE_ROOT/estimate-logs"
LOG_FILE="$LOG_DIR/${REPO//\//_}-${ISSUE_NUM}.log"
SUMMARIES_DIR="$STATE_ROOT/summaries"
SUMMARY_FILE="$SUMMARIES_DIR/${REPO//\//__}-${ISSUE_NUM}.estimate.md"
mkdir -p "$LOG_DIR" "$SUMMARIES_DIR"

# tee rather than a plain redirect: this runner is spawned by a cron tick whose
# own log is the first place anybody looks, and a one-shot job that leaves no
# trace there is a job nobody can tell ran at all.
exec > >(tee -a "$LOG_FILE") 2>&1
echo "[$(date -Iseconds)] estimate start  repo=$REPO  issue=#$ISSUE_NUM"

POINTS=""
on_exit() {
  echo "[$(date -Iseconds)] estimate exit  repo=$REPO  issue=#$ISSUE_NUM  points=${POINTS:-none}"
}
trap on_exit EXIT

# Model, thinking and caps: the same store-on-the-PVC pattern as every other
# runner, under the `planning` key so it is switched independently from chat.
. "$LIB_DIR/agent-models.sh"
. "$LIB_DIR/agent-thinking.sh"
. "$LIB_DIR/agent-limits.sh"
AGENT_MODEL="$(agent_model planning)"
agent_model_note planning "$AGENT_MODEL"
AGENT_ARGS=()
[ -z "$AGENT_MODEL" ] || AGENT_ARGS=(--model "$AGENT_MODEL")
AGENT_THINKING="$(agent_thinking planning)"
agent_thinking_note planning "$AGENT_THINKING"
[ -z "$AGENT_THINKING" ] || AGENT_ARGS+=(--thinking "$AGENT_THINKING")

# Short by design. An estimate that needs twenty minutes of model time has
# already cost more than the information is worth.
_EST_TURN_DEFAULT="${ESTIMATE_AGENT_TURN_TIMEOUT:-420}"
AGENT_TURN_TIMEOUT="$(agent_limit planning.turn "$_EST_TURN_DEFAULT")"
agent_limit_note planning.turn "$_EST_TURN_DEFAULT" "$AGENT_TURN_TIMEOUT"

SESSION_ID="estimate-${REPO//\//-}-${ISSUE_NUM}-$(date +%s)"

# --- the issue ---------------------------------------------------------------

ISSUE_JSON="$(curl -fsSL -H "$AUTH_HEADER" -H "$ACCEPT_HEADER" -H "$APIV_HEADER" \
  "$GH_API/repos/$REPO/issues/$ISSUE_NUM" 2>/dev/null)"
if [ -z "$ISSUE_JSON" ]; then
  echo "FATAL: could not read $REPO#$ISSUE_NUM — nothing estimated" >&2
  exit 1
fi

ISSUE_TITLE="$(printf '%s' "$ISSUE_JSON" | python3 -c \
  "import sys,json; print(json.load(sys.stdin).get('title') or '')" 2>/dev/null)"
ISSUE_BODY="$(printf '%s' "$ISSUE_JSON" | python3 -c \
  "import sys,json; print((json.load(sys.stdin).get('body') or '')[:6000])" 2>/dev/null)"

# What the labels already say. story_estimate is the single source of truth for
# that, so the gate below and the write further down cannot disagree about what
# counts as "already sized".
#
# One line per fact, empty meaning no/absent, in a fixed order — read straight
# into the variables that gate the rest of the script. The leading `ok` is a
# sentinel: without it a python that died would produce six empty lines, which
# read exactly like "an open, unlabelled, unestimated issue" and would send
# every malformed response to the model as if it were work.
READ_OK=""; IS_PR=""; ISSUE_STATE=""; HAVE_POINTS=""
ASKED=""; DEFERRED=""; MODEL_PIN=""
{
  read -r READ_OK
  read -r IS_PR
  read -r ISSUE_STATE
  read -r HAVE_POINTS
  read -r ASKED
  read -r DEFERRED
  read -r MODEL_PIN
} < <(ISSUE_JSON="$ISSUE_JSON" python3 -c '
import json, os
import story_estimate
issue = json.loads(os.environ["ISSUE_JSON"])
points = story_estimate.points_of(issue)
print("ok")
print("1" if "pull_request" in issue else "")
print(issue.get("state") or "open")
print("" if points is None else points)
print("1" if story_estimate.wants_estimate(issue) else "")
print("1" if story_estimate.deferred_to_next_sprint(issue) else "")
print(story_estimate.model_label(issue))' 2>/dev/null)
if [ "$READ_OK" != "ok" ]; then
  echo "FATAL: could not read the labels of $REPO#$ISSUE_NUM — nothing estimated" >&2
  exit 1
fi

# The issues endpoint serves pull requests too, and a PR is not a story.
if [ -n "$IS_PR" ]; then
  echo "[estimate] $REPO#$ISSUE_NUM is a pull request, not a story — nothing to size"
  exit 0
fi
if [ "$ISSUE_STATE" = "closed" ]; then
  echo "[estimate] $REPO#$ISSUE_NUM is closed — nothing to size"
  exit 0
fi

# Already sized and nobody asked again. Exiting here is what keeps a
# five-minute tick from spending a model call per issue per tick forever; the
# `estimate` label is how a human asks for a second opinion on a story that
# already has a number.
if [ -n "$HAVE_POINTS" ] && [ -z "$ASKED" ] && [ "${ESTIMATE_FORCE:-}" != "1" ]; then
  echo "[estimate] $REPO#$ISSUE_NUM is already $HAVE_POINTS point(s) and nothing asked for a re-estimate — exiting"
  exit 0
fi

# A deferral is NOT a reason to skip. `next sprint` parks the implementation;
# the size is exactly what the next sprint's planning needs in order to decide
# whether the story fits into it.
[ -z "$DEFERRED" ] || echo "[estimate] #$ISSUE_NUM is deferred to the next sprint — estimating anyway (the deferral parks the work, not the number)"
# The pin routes the SOLVER, not this turn: estimation runs on `planning` so it
# keeps working when the pinned model is out of quota. Logged because "why did
# this story run on that model" is asked of the log, not of the issue.
[ -z "$MODEL_PIN" ] || echo "[estimate] #$ISSUE_NUM pins the solver to '$MODEL_PIN' (this turn still runs on the planning model)"

# --- ask ---------------------------------------------------------------------

: > "$SUMMARY_FILE"

# The ceiling and the scale the model is shown come from ONE place.
#
# Both would otherwise be literals here: a `13` in the guard below and a
# hand-written band table in the prompt. Two copies of the same fact, and the
# fact is settable (`agent-limits set planning.split_points`). A table naming 13
# beside a ceiling of 5 would produce estimates the wrapper then rejects, and
# that reads as a misbehaving model rather than as two texts disagreeing.
SPLIT_POINTS="$(python3 -c "import story_points; print(story_points.SPLIT_POINTS)" 2>/dev/null)"
case "${SPLIT_POINTS:-}" in ''|*[!0-9]*) SPLIT_POINTS=13 ;; esac
# The ceiling can be switched OFF entirely, which means no story is ever handed
# back to be split. Carried as its own flag rather than as a magic value in
# SPLIT_POINTS, so the comparison below cannot accidentally succeed.
SPLIT_ENABLED="$(python3 -c "import story_points; print('1' if story_points.SPLIT_ENABLED else '0')" 2>/dev/null)"
case "${SPLIT_ENABLED:-}" in 0|1) : ;; *) SPLIT_ENABLED=1 ;; esac
SCALE_TABLE="$(python3 -c "import story_points; print(story_points.scale_table())" 2>/dev/null)"
[ -n "$SCALE_TABLE" ] || SCALE_TABLE="  1 point   up to 15 model calls
  2 points  up to 30 model calls
  3 points  up to 60 model calls
  5 points  up to 120 model calls
  8 points  up to 240 model calls
  13        more than that          TOO BIG — must be split, not started"
ALLOWED_POINTS="$(python3 -c "
import story_points
print(', '.join(str(p) for p in story_points.usable_scale() + (story_points.SPLIT_POINTS,)))" 2>/dev/null)"
[ -n "$ALLOWED_POINTS" ] || ALLOWED_POINTS="1, 2, 3, 5, 8, 13"
if [ "$SPLIT_ENABLED" = "1" ]; then
  echo "[estimate] scale ceiling: $SPLIT_POINTS points (allowed: $ALLOWED_POINTS)"
else
  echo "[estimate] scale ceiling: OFF — no story is handed back to be split (allowed: $ALLOWED_POINTS)"
fi

# This repository's own measured history: what past estimates turned out to
# cost. A model's prior about "a small refactor" is worth much less than what a
# small refactor actually cost in THIS repository.
#
# Empty until enough work has been recorded, and silent when empty: the output
# goes into the prompt, so a line explaining that there is no history would be
# read as a statement about the story being sized.
HISTORY="$(planning history "$REPO" --last 8 2>/dev/null || true)"
[ -z "$HISTORY" ] || echo "[estimate] including $(printf '%s' "$HISTORY" | grep -c '^  #') past story/stories as calibration"

PROMPT="Estimate this GitHub issue in story points. Do not implement anything,
do not open files, do not clone the repository — answer from the issue text.

Repository: $REPO
Issue #$ISSUE_NUM: $ISSUE_TITLE

$ISSUE_BODY

The scale is Fibonacci and it measures MODEL WORK, not human time. These bands
were measured from real runs of this bot, so estimate against them directly:

$SCALE_TABLE

$HISTORY

Estimate the work as SPECIFIED, not the work you would like it to be. If the
issue is vague, that is itself risk and belongs in the number; say so.

Estimate from the issue text first. Then, if history is shown above, check your
number against it: if this repository's stories of that size consistently cost
more than estimated, say so and correct upwards. Past estimates that were wrong
are evidence about THIS repository, not noise.

Write your answer to $SUMMARY_FILE in exactly this form, first line first:

POINTS: <one of $ALLOWED_POINTS>
<one or two sentences saying what drove the number, and naming the largest
unknown if there is one>

Write the file. Do not reply with the estimate only in chat."

echo "[agent] one turn on the planning model, timeout=${AGENT_TURN_TIMEOUT}s"
timeout "$((AGENT_TURN_TIMEOUT + 120))" openclaw agent --local \
  "${AGENT_ARGS[@]}" \
  --timeout "$AGENT_TURN_TIMEOUT" \
  --session-id "$SESSION_ID" \
  --message "$PROMPT" </dev/null
AGENT_RC=$?
echo "[agent] estimate turn finished rc=$AGENT_RC"

# --- read the answer ---------------------------------------------------------

ANSWER="$(SUMMARY_FILE="$SUMMARY_FILE" python3 -c '
import json, os, re
try:
    with open(os.environ["SUMMARY_FILE"], encoding="utf-8", errors="replace") as fh:
        text = fh.read()
except OSError:
    text = ""
# Tolerant of the bold the model reaches for unprompted (**POINTS:** 5) and of
# a leading blank line; strict about the number, which must be a bare integer.
m = re.search(r"^[ \t]*\**\s*POINTS\**\s*:\s*\**\s*(\d+)", text, re.MULTILINE)
rest = text[m.end():] if m else ""
lines = [l.strip() for l in rest.splitlines() if l.strip()]
print(json.dumps({"points": m.group(1) if m else "",
                  "rationale": " ".join(lines[:3])[:600]}))' 2>/dev/null)"

POINTS="$(printf '%s' "$ANSWER" | python3 -c \
  "import sys,json; print(json.load(sys.stdin)['points'])" 2>/dev/null)"
RATIONALE="$(printf '%s' "$ANSWER" | python3 -c \
  "import sys,json; print(json.load(sys.stdin)['rationale'])" 2>/dev/null)"

if [ -z "$POINTS" ]; then
  # No number is NOT an error to escalate: the issue keeps its `estimate`
  # label, stays unestimated, and the solver plans it as a defaulted 8 — which
  # is the strong model and the larger budget, the safe direction. The next
  # tick tries again.
  echo "[estimate] the agent produced no POINTS line — leaving #$ISSUE_NUM unestimated"
  exit 0
fi

# Coerce onto the scale. The agent is told the scale but is not trusted to
# honour it; 4 and 7 round UP, which is the direction that does not starve a
# run.
RAW_POINTS="$POINTS"
POINTS="$(RAW_POINTS="$RAW_POINTS" python3 -c '
import os
import story_points
n = story_points.normalise(os.environ["RAW_POINTS"])
print(n if n else "")' 2>/dev/null)"
if [ -z "$POINTS" ]; then
  echo "[estimate] '$RAW_POINTS' is not a size on this scale — leaving #$ISSUE_NUM unestimated"
  exit 0
fi
[ "$POINTS" = "$RAW_POINTS" ] || echo "[estimate] coerced the agent's $RAW_POINTS onto the scale as $POINTS"
echo "[estimate] $REPO#$ISSUE_NUM -> $POINTS point(s)"

# --- record it on the issue --------------------------------------------------
#
# GitHub has no add_labels/remove_labels on the issue PATCH: a label is ADDED
# with a POST to the issue's labels collection and REMOVED with a DELETE naming
# it in the URL. So the plan comes from story_estimate — which is where the
# one-value-per-scope rule the platform does not enforce actually lives — and
# this loop applies it, one call per removal.

LABEL_PLAN="$(ISSUE_JSON="$ISSUE_JSON" POINTS="$POINTS" python3 -c '
import json, os
import story_estimate
issue = json.loads(os.environ["ISSUE_JSON"])
add, remove = story_estimate.label_updates(issue, int(os.environ["POINTS"]))
print(json.dumps({"add": add, "remove": remove}))' 2>/dev/null)"
if [ -z "$LABEL_PLAN" ]; then
  echo "[estimate] WARNING: could not work out the label changes for #$ISSUE_NUM — the size is not recorded"
  LABEL_PLAN='{"add":[],"remove":[]}'
fi

ADD_PAYLOAD="$(printf '%s' "$LABEL_PLAN" | python3 -c '
import json, sys
plan = json.load(sys.stdin)
print(json.dumps({"labels": plan["add"]}) if plan["add"] else "")' 2>/dev/null)"

# The label has to EXIST in the repository before it can go on an issue, and a
# repo the bot has never estimated in has no `SP::` labels at all. Created from
# story_estimate.label_definitions() so the colour and the description are the
# same everywhere; a 422 because it already exists is the normal case and is
# deliberately ignored.
ensure_label() {
  local name="$1" payload
  payload="$(LABEL_NAME="$name" python3 -c '
import json, os
import story_estimate
name = os.environ["LABEL_NAME"]
for d in story_estimate.label_definitions():
    if d["name"] == name:
        print(json.dumps(d))
        break
else:
    print(json.dumps({"name": name, "color": "c5def5", "description": ""}))' 2>/dev/null)"
  [ -n "$payload" ] || return 0
  curl -fsSL -X POST -H "$AUTH_HEADER" -H "$ACCEPT_HEADER" -H "$APIV_HEADER" \
    -H 'Content-Type: application/json' -d "$payload" \
    "$GH_API/repos/$REPO/labels" >/dev/null 2>&1 || true
}

# Add BEFORE removing, so the issue is never momentarily sizeless. If the add
# fails the old size is still there, which is a stale number; if the removal
# ran first and the add then failed, the story would look unestimated and be
# re-planned as a defaulted 8.
if [ -n "$ADD_PAYLOAD" ]; then
  ensure_label "SP::$POINTS"
  if curl -fsSL -X POST -H "$AUTH_HEADER" -H "$ACCEPT_HEADER" -H "$APIV_HEADER" \
       -H 'Content-Type: application/json' -d "$ADD_PAYLOAD" \
       "$GH_API/repos/$REPO/issues/$ISSUE_NUM/labels" >/dev/null 2>&1; then
    echo "[estimate] labelled #$ISSUE_NUM SP::$POINTS"
  else
    echo "[estimate] WARNING: could not add SP::$POINTS to #$ISSUE_NUM"
  fi
else
  echo "[estimate] #$ISSUE_NUM already says SP::$POINTS — no label write"
fi

while IFS=$'\t' read -r enc name; do
  [ -n "$enc" ] || continue
  if curl -fsSL -X DELETE -H "$AUTH_HEADER" -H "$ACCEPT_HEADER" -H "$APIV_HEADER" \
       "$GH_API/repos/$REPO/issues/$ISSUE_NUM/labels/$enc" >/dev/null 2>&1; then
    echo "[estimate] removed '$name' from #$ISSUE_NUM"
  else
    echo "[estimate] WARNING: could not remove '$name' from #$ISSUE_NUM"
  fi
done < <(printf '%s' "$LABEL_PLAN" | python3 -c '
import json, sys, urllib.parse
for name in json.load(sys.stdin)["remove"]:
    print(urllib.parse.quote(name, safe="") + "\t" + name)' 2>/dev/null)

# --- one note ----------------------------------------------------------------

NOTE_PAYLOAD="$(POINTS="$POINTS" RATIONALE="$RATIONALE" \
  SPLIT_ENABLED="$SPLIT_ENABLED" python3 -c '
import json, os
import story_points
points = int(os.environ["POINTS"])
parts = ["\U0001F4D0 **Estimate: %d story point(s)**" % points]
rationale = os.environ.get("RATIONALE", "").strip()
if rationale:
    parts.append(rationale)
parts.append(story_points.describe(points))
if os.environ.get("SPLIT_ENABLED") == "1" and story_points.is_too_big(points):
    # Not a size the bot commits to: it is the name for "larger than the scale
    # carries". Starting it anyway is the failure the scale exists to prevent —
    # the run burns its whole budget and stops half-finished, leaving a branch
    # nobody asked for. Say so plainly rather than quietly picking it up.
    parts.append("This is larger than one run can carry. Please split it into "
                 "smaller issues and I will pick those up.")
parts.append("Estimated by the planning model; the size drives which model "
             "implements it. Change the `SP::` label to overrule this.")
print(json.dumps({"body": "\n\n".join(parts)}))' 2>/dev/null)"

if [ -n "$NOTE_PAYLOAD" ]; then
  curl -fsSL -X POST -H "$AUTH_HEADER" -H "$ACCEPT_HEADER" -H "$APIV_HEADER" \
    -H 'Content-Type: application/json' -d "$NOTE_PAYLOAD" \
    "$GH_API/repos/$REPO/issues/$ISSUE_NUM/comments" >/dev/null 2>&1 \
    && echo "[estimate] posted the estimate comment" \
    || echo "[estimate] WARNING: could not post the estimate comment"
fi

exit 0
