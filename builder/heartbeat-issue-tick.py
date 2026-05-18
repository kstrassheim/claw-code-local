#!/usr/bin/env python3
"""
heartbeat-issue-tick: deterministic concurrency gate for the heartbeat
issue-watcher loop.

Invoked from HEARTBEAT.md on every heartbeat tick. Lists each repo the
bot is a collaborator on, fetches open issues assigned to @me, and
decides which ones the agent should spawn a subagent for.

Concurrency rule (hard-bounded here, not in HEARTBEAT.md prose):
  - At most MAX_PER_REPO subagents per repo
  - Each subagent's slot is held for TTL_SECONDS, after which the slot
    is freed even if the subagent is still running (stuck-job guard)

The state is a lock-file directory in /tmp (cleared on every pod
restart, which is correct — restarts kill in-flight subagents anyway).
A lock is created here at "to_spawn" decision time, so even if the
agent never actually starts the subagent the slot is held for TTL.
That's the conservative side of the race; the alternative (LLM creates
the lock after spawn) would let a model that ignores the plan exceed
the limit.

Output: a single JSON document on stdout. HEARTBEAT.md tells the agent
to read it and spawn subagents only for entries under `to_spawn`.

Env:
  GITHUB_TOKEN         — bot's PAT (already wired)
  HEARTBEAT_MAX_PER_REPO (default 2)
  HEARTBEAT_TTL_SECONDS  (default 3600)
  HEARTBEAT_LOCK_DIR     (default /tmp/openclaw-issue-locks)
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

TOKEN = os.environ.get("GITHUB_TOKEN", "")
MAX_PER_REPO = int(os.environ.get("HEARTBEAT_MAX_PER_REPO", "2"))
TTL_SECONDS = int(os.environ.get("HEARTBEAT_TTL_SECONDS", "3600"))
LOCK_DIR = os.environ.get("HEARTBEAT_LOCK_DIR", "/tmp/openclaw-issue-locks")
TIMEOUT = 15


def gh_get(url: str, params: dict | None = None) -> list | dict:
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "openclaw-heartbeat-tick",
        },
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read())


def list_collaborator_repos() -> list[str]:
    repos: list[str] = []
    page = 1
    while True:
        batch = gh_get(
            "https://api.github.com/user/repos",
            {"affiliation": "collaborator", "per_page": 100, "page": page},
        )
        repos.extend(r["full_name"] for r in batch)
        if len(batch) < 100:
            break
        page += 1
    return sorted(set(repos))


def list_assigned_open_issues(repo: str) -> list[dict]:
    raw = gh_get(
        f"https://api.github.com/repos/{repo}/issues",
        {"assignee": "@me", "state": "open", "per_page": 50},
    )
    # GitHub's /issues endpoint returns PRs too; filter them out — the
    # bot's PR-handling lives in a different skill.
    return [i for i in raw if "pull_request" not in i]


def repo_slug(repo: str) -> str:
    return repo.replace("/", "__")


def list_active_locks(repo: str) -> list[dict]:
    """Locks newer than TTL_SECONDS hold a slot; older ones are released."""
    d = os.path.join(LOCK_DIR, repo_slug(repo))
    if not os.path.isdir(d):
        return []
    now = time.time()
    active: list[dict] = []
    for name in os.listdir(d):
        path = os.path.join(d, name)
        try:
            mtime = os.path.getmtime(path)
        except FileNotFoundError:
            continue
        if now - mtime > TTL_SECONDS:
            # Stale; remove so the slot is freed for new work.
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
            continue
        active.append(
            {
                "issueNumber": int(name.removesuffix(".lock")) if name.endswith(".lock") else None,
                "ageSeconds": int(now - mtime),
            }
        )
    return active


def take_lock(repo: str, issue_number: int) -> bool:
    """Atomic lock acquisition via O_CREAT|O_EXCL — returns False if taken."""
    d = os.path.join(LOCK_DIR, repo_slug(repo))
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f"{issue_number}.lock")
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return False
    os.write(fd, f"{int(time.time())}\n".encode())
    os.close(fd)
    return True


def main() -> int:
    if not TOKEN:
        json.dump({"error": "GITHUB_TOKEN not set; cannot list repos or issues"}, sys.stdout)
        return 1

    started = time.time()
    try:
        repos = list_collaborator_repos()
    except urllib.error.HTTPError as e:
        json.dump({"error": f"list collaborator repos failed: {e.code} {e.reason}"}, sys.stdout)
        return 2

    plan: dict = {
        "generatedAt": int(started),
        "ttlSeconds": TTL_SECONDS,
        "maxPerRepo": MAX_PER_REPO,
        "lockDir": LOCK_DIR,
        "repos": [],
    }

    for repo in repos:
        active = list_active_locks(repo)
        active_issue_numbers = {a["issueNumber"] for a in active if a["issueNumber"] is not None}
        slots = max(0, MAX_PER_REPO - len(active))

        try:
            issues = list_assigned_open_issues(repo)
        except urllib.error.HTTPError as e:
            plan["repos"].append(
                {"repo": repo, "error": f"list issues failed: {e.code} {e.reason}"}
            )
            continue

        # Skip issues already covered by a live lock; we don't want a
        # second subagent racing on the same issue.
        candidates = [i for i in issues if i["number"] not in active_issue_numbers]

        to_spawn: list[dict] = []
        for issue in candidates[:slots]:
            if not take_lock(repo, issue["number"]):
                # Another tick already grabbed this (cross-process race).
                continue
            to_spawn.append(
                {
                    "issueNumber": issue["number"],
                    "title": issue["title"],
                    "url": issue["html_url"],
                    "labels": [l["name"] for l in issue.get("labels", [])],
                    "lockPath": os.path.join(LOCK_DIR, repo_slug(repo), f"{issue['number']}.lock"),
                }
            )

        plan["repos"].append(
            {
                "repo": repo,
                "activeSlotsUsed": len(active),
                "activeIssueNumbers": sorted(active_issue_numbers),
                "openAssignedCount": len(issues),
                "candidateCount": len(candidates),
                "toSpawn": to_spawn,
                "deferredDueToLimit": max(0, len(candidates) - slots),
            }
        )

    plan["elapsedSeconds"] = round(time.time() - started, 2)
    json.dump(plan, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
