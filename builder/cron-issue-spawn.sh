#!/bin/bash
# cron-issue-spawn: invoked by the issue-watcher CronJob every tick.
#
# Calls the (read-only) tick planner to produce a JSON spawn plan, then
# for each entry kubectl-exec's into the openclaw pod and backgrounds
# /usr/local/bin/fixer-runner.sh there. The fixer runs as a subprocess
# inside the openclaw container — it shares the pod's network, secrets,
# config, and persistent workspace volume (so it can keep a long-lived
# git checkout under ~/.openclaw/projects/<repo>/).
#
# Concurrency lives in the openclaw container's filesystem: one mkdir
# lock per repo, max 1 fixer per repo. Fewer than 2 (the previous cap)
# because two subprocesses can't safely share the same on-disk
# checkout. Issues queued for a busy repo wait for the next tick.
#
# This script does NOT decide what to spawn. It only translates the
# planner's `toSpawn` array into kubectl-exec invocations.
set -euo pipefail

NAMESPACE=$(cat /var/run/secrets/kubernetes.io/serviceaccount/namespace)

# Resolve the running openclaw pod once per tick.
OPENCLAW_POD=$(kubectl -n "$NAMESPACE" get pod \
    -l app=openclaw,component=server \
    -o jsonpath='{.items[?(@.status.phase=="Running")].metadata.name}' \
    | awk '{print $1}')
if [ -z "$OPENCLAW_POD" ]; then
  # Some deployments don't carry the component=server label.
  OPENCLAW_POD=$(kubectl -n "$NAMESPACE" get pod \
      -l app=openclaw \
      -o jsonpath='{.items[?(@.status.phase=="Running")].metadata.name}' \
      | awk '{print $1}')
fi
test -n "$OPENCLAW_POD" || { echo "ERROR: no Running openclaw pod found in $NAMESPACE" >&2; exit 1; }
echo "openclaw pod: $OPENCLAW_POD"

PLAN=$(/usr/local/bin/heartbeat-issue-tick)
echo "$PLAN" | python3 -c "
import json, os, subprocess, sys, shlex
plan = json.load(sys.stdin)

OPENCLAW_POD = os.environ['OPENCLAW_POD']
NAMESPACE = plan['namespace']

errors = [r for r in plan['repos'] if r.get('error')]
for e in errors:
    print(f\"ERROR {e['repo']}: {e['error']}\", file=sys.stderr)

spawned = 0
for r in plan['repos']:
    if r.get('error'):
        continue
    for issue in r.get('toSpawn', []):
        repo = r['repo']
        n = issue['issueNumber']
        url = issue['url']
        title = issue['title']

        # Build the exec command. setsid + redirected stdio detach the
        # fixer-runner from the kubectl-exec connection so it survives
        # past this script's exit (otherwise it would get SIGHUP'd).
        # Args are shell-escaped to survive the bash-c wrapper.
        runner_args = ' '.join(shlex.quote(a) for a in [repo, str(n), url, title])
        remote_cmd = (
            f'setsid bash -c '
            + shlex.quote(f'nohup /usr/local/bin/fixer-runner {runner_args} >/dev/null 2>&1 </dev/null &')
            + ' >/dev/null 2>&1 </dev/null &'
        )

        proc = subprocess.run(
            ['kubectl', '-n', NAMESPACE, 'exec', OPENCLAW_POD, '-c', 'openclaw',
             '--', 'bash', '-c', remote_cmd],
            capture_output=True, text=True, timeout=30,
        )
        if proc.returncode != 0:
            print(f'ERROR exec for {repo}#{n}: rc={proc.returncode} stderr={proc.stderr.strip()}', file=sys.stderr)
        else:
            print(f'spawned fixer for {repo}#{n}: {title}')
            spawned += 1

deferred = sum(r.get('deferredDueToLimit', 0) for r in plan['repos'])
print(f'tick done: spawned={spawned}, deferred_due_to_limit={deferred}')
"
