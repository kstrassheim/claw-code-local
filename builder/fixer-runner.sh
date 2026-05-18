#!/bin/bash
# fixer-runner: backgrounded subprocess inside the openclaw container.
# Holds a per-repo lock, manages the shared git checkout under
# ~/.openclaw/projects/<repo>/, then runs `openclaw agent --local`
# in a poll loop: agent does one turn at a time, the wrapper checks
# the issue for new @-mention comments every POLL_INTERVAL, reacts
# :+1: to each, and re-invokes the agent with the comment as the
# next turn's user message (same --session-id so context persists).
# Exits when the agent has opened a PR on the branch, or after
# MAX_LIFETIME_SECONDS (whichever first).
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
#
# Lock semantics: `mkdir <lockdir>` is atomic. The lock dir is a
# sibling of the project tree, NOT inside it (a `.fixer.lock` inside
# the project dir broke `git clone` because the destination was
# non-empty). Trap clears the lock on every exit path.
set -uo pipefail

REPO="$1"
ISSUE_NUM="$2"
ISSUE_URL="$3"
ISSUE_TITLE="$4"

BOT_LOGIN="${FIXER_BOT_LOGIN:-cameron-claw}"
POLL_INTERVAL="${FIXER_POLL_INTERVAL:-300}"
MAX_LIFETIME_SECONDS="${FIXER_MAX_LIFETIME:-$((6 * 3600))}"
AGENT_TURN_TIMEOUT=3500  # per --local invocation, leave grace under the
                         # 6h overall budget

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

# Legacy cleanup: pre-.24 builds left a .fixer.lock inside the project
# dir, which would break the empty-dir check on the next clone.
rm -rf "$PROJECT_DIR/.fixer.lock" 2>/dev/null

exec >> "$LOG_FILE" 2>&1

echo "============================================================"
echo "[$(date -Iseconds)] fixer start  repo=$REPO  issue=#$ISSUE_NUM"
echo "============================================================"

# -- workspace setup (clone or update) --------------------------------
if [ ! -d "$PROJECT_DIR/.git" ]; then
  echo "[clone] $REPO → $PROJECT_DIR"
  git clone --quiet "https://github.com/$REPO.git" "$PROJECT_DIR"
fi
cd "$PROJECT_DIR"
git fetch --quiet origin
DEFAULT_BRANCH="$(git remote show origin | awk '/HEAD branch/ {print $NF}')"
echo "[checkout] default-branch=$DEFAULT_BRANCH"
git checkout --quiet "$DEFAULT_BRANCH"
git reset --hard --quiet "origin/$DEFAULT_BRANCH"
git clean -fdx --quiet
BRANCH="issue-$ISSUE_NUM-fix"
git branch -D "$BRANCH" 2>/dev/null || true
git checkout --quiet -b "$BRANCH"

# -- GH API helpers ---------------------------------------------------

GH_API="https://api.github.com"
AUTH_HEADER="Authorization: Bearer $GITHUB_TOKEN"
ACCEPT_HEADER="Accept: application/vnd.github+json"
APIV_HEADER="X-GitHub-Api-Version: 2022-11-28"

# Fetch new @-mention comments since cursor. Stdin: cursor id (or 0).
# Stdout: JSON array of {id, user, body, html_url} for matching comments.
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
        continue  # skip own comments — we don't react to our own posts
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

pr_exists_for_branch() {
  # Returns 0 if a PR is open from <branch> in this repo, else 1.
  local count
  count=$(curl -fsSL -H "$AUTH_HEADER" -H "$ACCEPT_HEADER" -H "$APIV_HEADER" \
    "$GH_API/repos/$REPO/pulls?head=${REPO%%/*}:${BRANCH}&state=all&per_page=1" \
    | python3 -c "import sys,json; print(len(json.load(sys.stdin)))")
  [ "${count:-0}" -ge 1 ]
}

most_recent_comment_id() {
  curl -fsSL -H "$AUTH_HEADER" -H "$ACCEPT_HEADER" -H "$APIV_HEADER" \
    "$GH_API/repos/$REPO/issues/$ISSUE_NUM/comments?per_page=100" \
    | python3 -c "import sys,json; cs=json.load(sys.stdin); print(max((c['id'] for c in cs), default=0))"
}

# Make the bot login visible to the inline python helpers.
export FIXER_BOT_LOGIN_VAL="$BOT_LOGIN"

# -- session + initial turn --------------------------------------------

# Fresh per-invocation session (no context bleed between fixer-runner
# lifetimes). Within this lifetime, every agent turn shares this id.
SESSION_ID="issue-${REPO//\//-}-${ISSUE_NUM}-$(date +%s)"

# Initialise the comment cursor: anchor on the newest existing comment
# so the first poll only picks up brand-new ones the user posts AFTER
# the fixer starts. (Otherwise the agent would react to old comments
# from prior fixers / unrelated discussion on first poll.)
if [ -f "$CURSOR_FILE" ]; then
  LAST_SEEN_ID="$(cat "$CURSOR_FILE")"
  echo "[cursor] resumed from $CURSOR_FILE = $LAST_SEEN_ID"
else
  LAST_SEEN_ID="$(most_recent_comment_id)"
  echo "$LAST_SEEN_ID" > "$CURSOR_FILE"
  echo "[cursor] initialised at $LAST_SEEN_ID (anchor to current latest)"
fi

INITIAL_PROMPT="You are working autonomously to fix GitHub issue $ISSUE_URL — \"$ISSUE_TITLE\".

You are in a fresh checkout of $REPO at $(pwd) on branch $BRANCH
(already branched off $DEFAULT_BRANCH). The git author identity is
cameron-claw (via \$GITHUB_TOKEN). You have the github MCP server
available for issue/PR operations.

## Protocol — follow this exactly

1. **Post an initial status comment** on issue #$ISSUE_NUM via the
   github MCP \`add_issue_comment\` tool. One short line: what you
   are about to do.

2. **Work as autonomously as possible.** Read the codebase,
   implement the change, run tests if any exist, commit, push. Use
   the descriptive message style of recent commits on the default
   branch. Do not delegate to subagents.

3. **When you finish:** open a PR back to $DEFAULT_BRANCH with
   \"Closes #$ISSUE_NUM\" in the body. Then post a **final status
   comment** on the issue with the PR link. Then stop your turn.

4. **If you get blocked** — i.e., the issue is genuinely ambiguous
   and you'd be guessing — **DO NOT guess**. Post a comment on the
   issue tagging \`@kstrassheim\` with ONE specific question, then
   stop your turn. A wrapper will keep polling for the user's
   reply; when they answer (by tagging you @$BOT_LOGIN), you will
   be re-invoked in the same session with their reply as the next
   user message. Resume from where you left off — do NOT start
   over.

5. **Comment hygiene:** keep status comments terse. One line is
   often enough. The user is reading these in their notification
   feed, not a meeting prep doc.

6. **Reactions:** do NOT add reactions yourself. The wrapper handles
   marking comments as read with :+1: after each poll.

Begin."

echo "[turn 1] initial agent invocation"
openclaw agent --local \
  --timeout "$AGENT_TURN_TIMEOUT" \
  --session-id "$SESSION_ID" \
  --message "$INITIAL_PROMPT" || echo "[agent] turn 1 exited non-zero ($?) — continuing into poll loop"

# -- poll loop ---------------------------------------------------------

START_TIME=$(date +%s)
turn=2

while :; do
  elapsed=$(( $(date +%s) - START_TIME ))
  if [ "$elapsed" -ge "$MAX_LIFETIME_SECONDS" ]; then
    echo "[$(date -Iseconds)] max lifetime ($MAX_LIFETIME_SECONDS s) reached — exiting"
    break
  fi

  if pr_exists_for_branch; then
    echo "[$(date -Iseconds)] PR exists for $BRANCH — exiting"
    break
  fi

  echo "[poll] sleeping $POLL_INTERVAL s (last_seen=$LAST_SEEN_ID)"
  sleep "$POLL_INTERVAL"

  # Re-check PR after the sleep in case the agent's first turn took a
  # while and finished asynchronously.
  if pr_exists_for_branch; then
    echo "[$(date -Iseconds)] PR exists for $BRANCH — exiting"
    break
  fi

  NEW_JSON="$(fetch_new_mentions "$LAST_SEEN_ID" 2>/dev/null || echo '[]')"
  if [ -z "$NEW_JSON" ] || [ "$NEW_JSON" = "[]" ]; then
    echo "[poll] no new @-mention comments"
    continue
  fi

  # React to each new comment, advance cursor.
  # Process substitution (not a pipe) — keeps the loop in the parent
  # shell so LAST_SEEN_ID updates persist.
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

  # Build the follow-up prompt from the new comments.
  FOLLOWUP_PROMPT="New comments on issue #$ISSUE_NUM in which you are mentioned:

$(echo "$NEW_JSON" | python3 -c "
import sys, json
for c in json.load(sys.stdin):
    print(f'--- @{c[\"user\"]}: ---')
    print(c['body'])
    print()
")

Take these into account and continue from where you left off. If a
comment answers a question you previously asked, apply the answer
and resume work. If a comment redirects the work, adjust accordingly."

  echo "[turn $turn] re-invoking agent with $(echo "$NEW_JSON" | python3 -c 'import sys,json;print(len(json.load(sys.stdin)))') new comment(s)"
  openclaw agent --local \
    --timeout "$AGENT_TURN_TIMEOUT" \
    --session-id "$SESSION_ID" \
    --message "$FOLLOWUP_PROMPT" || echo "[agent] turn $turn exited non-zero ($?) — continuing"

  turn=$(( turn + 1 ))
done

echo "[$(date -Iseconds)] fixer exit  repo=$REPO  issue=#$ISSUE_NUM  turns=$(( turn - 1 ))"
