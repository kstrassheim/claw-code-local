#!/bin/bash
# cron-reviewer-spawn: invoked by the `pr-reviewer` CronJob every tick.
#
# Calls the read-only reviewer-tick planner to produce a JSON plan, then
# for each entry with toSpawn=true kubectl-exec's a reviewer-runner
# subprocess into the openclaw pod. The runner does its own thing
# (per-repo lock, checkout of the pull request's head branch, local
# validation, verdict comment + review) and exits.
#
# Concurrency lives entirely in the openclaw container's filesystem:
# per-repo lock dir under ~/.openclaw/.reviewer-locks/<owner>__<name>/.
# This script does NOT decide whether to spawn — it only translates
# the planner's output into kubectl invocations.
#
# The schedule (2-59/5) is deliberately offset from the issue-watcher
# (*/5) and the tester (*/10) so the three planners never fire in the
# same minute and race each other for a model slot.
set -euo pipefail

NAMESPACE=$(cat /var/run/secrets/kubernetes.io/serviceaccount/namespace)

OPENCLAW_POD=$(kubectl -n "$NAMESPACE" get pod \
    -l app=openclaw,component=server \
    -o jsonpath='{.items[?(@.status.phase=="Running")].metadata.name}' \
    | awk '{print $1}')
if [ -z "$OPENCLAW_POD" ]; then
  OPENCLAW_POD=$(kubectl -n "$NAMESPACE" get pod \
      -l app=openclaw \
      -o jsonpath='{.items[?(@.status.phase=="Running")].metadata.name}' \
      | awk '{print $1}')
fi
test -n "$OPENCLAW_POD" || { echo "ERROR: no Running openclaw pod found in $NAMESPACE" >&2; exit 1; }
export OPENCLAW_POD
echo "openclaw pod: $OPENCLAW_POD"

PLAN=$(/usr/local/bin/reviewer-tick)
echo "$PLAN" | python3 -c "
import json, os, subprocess, sys, shlex
plan = json.load(sys.stdin)

OPENCLAW_POD = os.environ['OPENCLAW_POD']
NAMESPACE = plan['namespace']

if plan.get('error'):
    print(f\"planner error: {plan['error']}\", file=sys.stderr)
    sys.exit(1)

spawned = 0
skipped = 0
for p in plan.get('prs', []):
    if not p.get('toSpawn'):
        skipped += 1
        continue
    repo = p['repo']
    number = p['prNumber']

    # setsid + redirected stdio detach the reviewer-runner from the
    # kubectl-exec connection so it survives past this script's exit
    # (otherwise it would get SIGHUP'd the moment the exec closes).
    runner_args = ' '.join(shlex.quote(str(a)) for a in
                           [repo, number, p.get('headRef',''), p.get('headSha','')])
    remote_cmd = (
        f'setsid bash -c '
        + shlex.quote(f'nohup /usr/local/bin/reviewer-runner {runner_args} >/dev/null 2>&1 </dev/null &')
        + ' >/dev/null 2>&1 </dev/null &'
    )
    proc = subprocess.run(
        ['kubectl', '-n', NAMESPACE, 'exec', OPENCLAW_POD, '-c', 'openclaw',
         '--', 'bash', '-c', remote_cmd],
        capture_output=True, text=True, timeout=30,
    )
    if proc.returncode != 0:
        print(f'ERROR exec for review {repo}#{number}: rc={proc.returncode} stderr={proc.stderr.strip()}', file=sys.stderr)
    else:
        print(f'spawned reviewer for {repo}#{number} @ {p.get(\"headSha\",\"\")[:7]}')
        spawned += 1

denied = sorted({p['repo'] for p in plan.get('prs', [])
                 if str(p.get('reason','')) == 'not-permitted'})
if plan.get('allowlistAvailable') is False:
    print('WARNING: could not read the allowed-projects list — nothing was reviewed. '
          'Check the openclaw pod and ~/.openclaw/projects-allowed.list', file=sys.stderr)
elif denied:
    print(f'not permitted ({len(denied)}): ' + ', '.join(denied[:10]))

waiting = [p for p in plan.get('prs', []) if p.get('reason') == 'wait-checks']
red = [p for p in plan.get('prs', []) if p.get('reason') == 'checks-failed']
if waiting:
    print(f'waiting on checks ({len(waiting)}): '
          + ', '.join(f\"{p['repo']}#{p['prNumber']}\" for p in waiting[:10]))
if red:
    print(f'checks failed, not reviewing ({len(red)}): '
          + ', '.join(f\"{p['repo']}#{p['prNumber']}\" for p in red[:10]))
print(f'reviewer tick done: spawned={spawned}, skipped={skipped}, '
      f'pending={plan.get(\"pendingReviews\")}')
"
