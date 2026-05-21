#!/bin/bash
# cron-tester-spawn: invoked by the `tester` CronJob every tick.
#
# Calls the read-only tester-tick planner to produce a JSON plan,
# then for each entry with toSpawn=true kubectl-exec's a tester-runner
# subprocess into the openclaw pod. The runner does its own thing
# (per-repo lock, last-head-changed gate, agent invocation, draft
# processing, issue creation) and exits.
#
# Concurrency lives entirely in the openclaw container's filesystem:
# per-repo lock dir under ~/.openclaw/.tester-locks/<owner>__<name>/.
# This script does NOT decide whether to spawn — it only translates
# the planner's output into kubectl invocations.
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

PLAN=$(/usr/local/bin/tester-tick)
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
for r in plan.get('repos', []):
    if not r.get('toSpawn'):
        skipped += 1
        continue
    repo = r['repo']

    # Build the exec command. setsid + redirected stdio detach the
    # tester-runner from the kubectl-exec connection so it survives
    # past this script's exit (otherwise it would get SIGHUP'd).
    runner_args = shlex.quote(repo)
    remote_cmd = (
        f'setsid bash -c '
        + shlex.quote(f'nohup /usr/local/bin/tester-runner {runner_args} >/dev/null 2>&1 </dev/null &')
        + ' >/dev/null 2>&1 </dev/null &'
    )
    proc = subprocess.run(
        ['kubectl', '-n', NAMESPACE, 'exec', OPENCLAW_POD, '-c', 'openclaw',
         '--', 'bash', '-c', remote_cmd],
        capture_output=True, text=True, timeout=30,
    )
    if proc.returncode != 0:
        print(f'ERROR exec for tester {repo}: rc={proc.returncode} stderr={proc.stderr.strip()}', file=sys.stderr)
    else:
        head = r.get('headSha','')[:7]
        prior = (r.get('priorHead') or '')[:7] or 'none'
        print(f'spawned tester for {repo}: {prior} -> {head}')
        spawned += 1

print(f'tester tick done: spawned={spawned}, skipped={skipped}')
"
