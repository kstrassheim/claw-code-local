"""forge: the questions this bot asks a code host, and one answer per host.

WHY AN INTERFACE AND NOT A CLIENT
---------------------------------
The planners and the runners spend their time deciding things: is this issue
parked, is this head green, has a person answered, may this be merged. Every
one of those decisions used to be written in the same expression as the
request that answered it, so "should this be parked?" and "which URL says so?"
were one piece of code. Adding a second code host then means editing every
place a decision is made, rather than adding one implementation.

So the seam is drawn around the QUESTIONS, not around anybody's REST API.
`Forge` below is the list of things the bot actually wants to know. Each
implementation answers them however its platform can, and the callers never
learn which platform answered.

TWO SHAPES ARE LOAD-BEARING, AND BOTH WERE LEARNED FROM AN OUTAGE
-----------------------------------------------------------------
`checks_state(repo, sha)` returns a DECISION — one of `green`, `failed`,
`pending`, `none` — and never raw payloads. One host reports CI in two
unrelated places that have to be merged; another has a single pipeline state.
If callers saw the raw data they would each re-derive the reduction, slightly
differently, and the difference would be invisible until a red pull request
was reviewed as green. `none` is deliberately NOT `pending`: a repository
with no CI configured at all is not a build in progress, and a gate that
confuses the two waits forever on every such repository.

`close_issue(repo, n, delivered)` takes the INTENT — the work shipped, or it
was called off — and never a platform reason string. One host records that in
a native close reason; another has no such field and has to say it another
way. The caller knows which of the two happened; it must not know, or care,
how the platform writes it down.

THE NEUTRAL SHAPES
------------------
Everything handed back is this repository's own vocabulary, so a caller can
be read without knowing where the data came from:

  issue          {"forge", "repo", "number", "title", "body", "url",
                  "labels": [name, ...], "state": "open"|"closed",
                  "closedAs": None|"delivered"|"revoked"}

  note           {"id", "body", "author": {"username": ...}}
                 — the shape lexical_guard already speaks, so the guard has
                 one definition of "has the bot already asked this?" rather
                 than one per platform.

  change request {"forge", "repo", "number", "title", "body", "url",
                  "state": "open"|"closed"|"merged", "draft": bool,
                  "headSha", "headRef", "baseRef", "labels", "mergeable"}
                 — a pull request or a merge request; the callers only ever
                 needed "the change somebody proposed".

  verdict        {"author", "verdict": "approved"|"changes_requested"
                  |"commented"|"dismissed", "body", "sha"}

  finding        {"id", "rule", "severity", "state", "title", "url"}

SELECTION IS PER ITEM, NEVER A GLOBAL SWITCH
--------------------------------------------
`configured()` returns a `Forges` holding one implementation per host whose
credentials are present — nothing else is constructed, so a deployment with
only one set of credentials behaves exactly as it did before there were two.
Discovery stamps every record it hands back with the forge it came from, and
`Forges.of(record)` reads that stamp. A repository is never routed by a
global mode flag, because the whole point is that both hosts are worked in
the same tick.

Env:
  GITHUB_TOKEN / GITHUB_API (or GITHUB_API_URL) — GitHub credentials & base
  GITLAB_API_TOKEN / GITLAB_URL                 — GitLab credentials & base
"""

from __future__ import annotations

import abc
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

HTTP_TIMEOUT = 15

# The four answers `checks_state` may give. `none` is separate from `pending`
# on purpose — see the module docstring.
GREEN, FAILED, PENDING, NONE = "green", "failed", "pending", "none"

# How a close is recorded, in intent rather than in any platform's spelling.
DELIVERED = "delivered"
REVOKED = "revoked"

GITHUB = "github"
GITLAB = "gitlab"


class ForgeError(RuntimeError):
    """A request to a code host failed.

    Carries the status code and the host's own words when there are any, so a
    planner can report "401" rather than "something went wrong" without
    knowing which host it was talking to.
    """

    def __init__(self, message: str, *, code: int | None = None,
                 reason: str = "", url: str = "", body: str = ""):
        super().__init__(message)
        self.code = code
        self.reason = reason
        self.url = url
        self.body = body


class RateLimited(ForgeError):
    """The host refused because the budget for this token is spent.

    Named separately because it arrives as an ordinary permission failure on
    at least one host: a 403 on a tick that made one request reads like a
    missing scope, and is not one.
    """


def _http(method: str, url: str, *, headers: dict, params: dict | None = None,
          json_body=None, form_body: dict | None = None,
          timeout: int = HTTP_TIMEOUT):
    """The one place in this module where a request leaves the process.

    Injected as `transport=` by the tests, which is what keeps every test in
    this repository network-free without also stubbing out the request
    building — the part most likely to be wrong.

    Returns the parsed JSON body, or None when the response had no body.
    Raises ForgeError for anything that is not a 2xx.
    """
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    data = None
    headers = dict(headers)
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        headers.setdefault("Content-Type", "application/json")
    elif form_body is not None:
        data = urllib.parse.urlencode(form_body).encode("utf-8")
        headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace") if e.fp else ""
        except Exception:  # noqa: BLE001 - the error is what matters, not this
            body = ""
        head = e.headers or {}
        if e.code == 403 and head.get("X-RateLimit-Remaining") == "0":
            raise RateLimited(
                f"rate limit exhausted on {url} "
                f"(resets at {head.get('X-RateLimit-Reset', '?')})",
                code=e.code, reason=str(e.reason or ""), url=url, body=body,
            ) from None
        raise ForgeError(f"{e.code} on {url}: {body[:200]}",
                         code=e.code, reason=str(e.reason or ""),
                         url=url, body=body) from None
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


class Forge(abc.ABC):
    """Everything the bot needs to know about a code host.

    Implementations answer in the neutral shapes described at the top of this
    module. A method a platform cannot answer yet raises NotImplementedError
    with a sentence saying so — never a silent empty result, which a caller
    would read as "there is nothing there".
    """

    #: `github` / `gitlab`. Stamped onto every record this forge hands back,
    #: and what `Forges.of` routes on.
    kind: str = ""

    #: What a human calls the thing this host proposes changes with. Used in
    #: anything a person reads, because "pull request" in a merge-request
    #: project is a bug report waiting to happen.
    change_request_noun: str = "change request"

    # -- identity -------------------------------------------------------

    @abc.abstractmethod
    def bot_identity(self) -> str:
        """The account name these credentials authenticate as.

        Resolved rather than configured: sibling deployments run under
        different accounts, and a hardcoded name makes "has the bot already
        said this?" answer about somebody else.
        """

    # -- discovery ------------------------------------------------------

    @abc.abstractmethod
    def assigned_open_issues(self) -> dict[str, list[dict]]:
        """Every open issue assigned to the bot, keyed by `owner/name`."""

    @abc.abstractmethod
    def reviewable_change_requests(self, limit: int) -> list[dict]:
        """Open change requests this bot should look at, newest first.

        Records are stubs — repo, number, labels — because the listing
        endpoints do not carry a head commit anywhere. `change_request` is
        what fills that in, and it costs a call per candidate.
        """

    @abc.abstractmethod
    def accessible_repos(self, limit: int) -> list[str]:
        """Repositories these credentials can reach, most recently pushed
        first. A fallback candidate source only — see tester-tick on why an
        activity-ordered list must not be the normal path."""

    @abc.abstractmethod
    def default_branch_head(self, repo: str) -> tuple[str, str]:
        """(default branch, head sha). Empty strings when it cannot be read;
        the caller treats that as "skip this repository"."""

    # -- issues ---------------------------------------------------------

    @abc.abstractmethod
    def issue(self, repo: str, number: int) -> dict:
        """One issue, in the neutral shape. {} when it cannot be read."""

    @abc.abstractmethod
    def comments(self, repo: str, number: int) -> list[dict]:
        """Every note on an issue, oldest first."""

    @abc.abstractmethod
    def post_comment(self, repo: str, number: int, body: str) -> bool:
        """Say something on an issue. False on any failure, never raises:
        a planner that cannot comment must still plan the rest of the tick."""

    @abc.abstractmethod
    def add_labels(self, repo: str, number: int, labels) -> bool:
        """Add labels to an issue. Mutual exclusion between `scope::value`
        labels is issue_status' business, not the transport's."""

    @abc.abstractmethod
    def remove_label(self, repo: str, number: int, label: str) -> bool:
        """Take one label off an issue."""

    @abc.abstractmethod
    def close_issue(self, repo: str, number: int, delivered: bool) -> bool:
        """Close an issue, recording WHETHER THE WORK SHIPPED.

        `delivered=True` means the change was made and merged;
        `delivered=False` means it was called off — a duplicate, a refusal, a
        decision not to do it. The caller knows which; each platform records
        it in whatever field or label it has for the purpose.
        """

    # -- change requests ------------------------------------------------

    @abc.abstractmethod
    def open_change_requests_for_issue(self, repo: str,
                                       number: int) -> list[int]:
        """Open change requests that would close this issue."""

    @abc.abstractmethod
    def change_request(self, repo: str, number: int) -> dict:
        """One change request, in the neutral shape. {} when unreadable."""

    @abc.abstractmethod
    def change_request_comments(self, repo: str, number: int) -> list[dict]:
        """Every note on a change request, oldest first.

        Separate from `comments` because the two are separate things on at
        least one host, and because a caller asking about a change request
        should say so.
        """

    @abc.abstractmethod
    def checks_state(self, repo: str, sha: str) -> str:
        """CI for one commit, reduced to green / failed / pending / none.

        THE ONLY PLACE CHECK SEMANTICS ARE DECIDED. Every caller gates on the
        answer and none of them may re-derive it. Anything unknown — an
        unreadable state, an unrecognised conclusion, a lookup that failed —
        is `pending`, because an unknown CI state must never read as green.
        """

    @abc.abstractmethod
    def review_verdicts(self, repo: str, number: int) -> list[dict]:
        """Reviews left on a change request, oldest first."""

    @abc.abstractmethod
    def merge(self, repo: str, number: int, squash: bool = True,
              delete_branch: bool = True) -> bool:
        """Merge a change request. False on any refusal."""

    @abc.abstractmethod
    def security_findings(self, repo: str, number: int) -> list[dict]:
        """Security findings the host itself raised against a change."""


# ---------------------------------------------------------------------------
# GitHub
# ---------------------------------------------------------------------------


# What a completed check-run conclusion means for the gate.
#
# `action_required` is a failure on purpose: it will never turn green on its
# own, so waiting for it is waiting forever. `stale` is NOT — a run is marked
# stale when a newer one supersedes it, so the honest answer is "the real
# answer has not arrived yet".
PASSING_CONCLUSIONS = frozenset({"success", "neutral", "skipped"})
FAILING_CONCLUSIONS = frozenset({
    "failure", "timed_out", "cancelled", "canceled", "action_required",
    "startup_failure",
})

# The native close reasons, and what each one means to this bot.
_COMPLETED = "completed"
_NOT_PLANNED = "not_planned"


class GitHubForge(Forge):
    """GitHub, over its REST API.

    Every quirk here was written from a failure in production, and each is
    commented where it lives rather than in a list nobody reads.
    """

    kind = GITHUB
    change_request_noun = "pull request"

    def __init__(self, token: str, api: str = "https://api.github.com",
                 *, transport=_http, user_agent: str = "openclaw-forge/1.0"):
        self.token = token or ""
        self.api = (api or "https://api.github.com").rstrip("/")
        self._transport = transport
        self._user_agent = user_agent
        self._identity: str | None = None

    # -- transport ------------------------------------------------------

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": self._user_agent,
        }

    def _get(self, path: str, params: dict | None = None):
        return self._transport("GET", f"{self.api}{path}",
                               headers=self._headers(), params=params)

    def _write(self, method: str, path: str, payload=None):
        """A write that reports rather than raises.

        Callers decide what a failed write means — usually "leave the item
        alone" — and none of them want a planner-wide crash for it.
        """
        try:
            self._transport(method, f"{self.api}{path}",
                            headers=self._headers(), json_body=payload)
            return True
        except Exception:  # noqa: BLE001 - any failure is "did not write"
            return False

    # -- identity -------------------------------------------------------

    def bot_identity(self) -> str:
        if self._identity is None:
            me = self._get("/user")
            self._identity = str((me or {}).get("login") or "") \
                if isinstance(me, dict) else ""
        return self._identity

    # -- neutral shapes -------------------------------------------------

    def _labels(self, raw) -> list[str]:
        """Label names, whatever shape the API handed back.

        Both shapes really do occur: the issues endpoints return objects, and
        some search payloads have been seen returning bare strings.
        """
        out = []
        for item in raw or []:
            name = item.get("name", "") if isinstance(item, dict) else item
            name = str(name or "").strip()
            if name:
                out.append(name)
        return out

    def _issue(self, raw: dict, repo: str = "") -> dict:
        state = str(raw.get("state") or "open").lower()
        closed_as = None
        if state == "closed":
            # An issue closed with no reason at all predates the field, and
            # every one of those was a delivery. `not_planned` is the only
            # value that means the work was called off.
            reason = str(raw.get("state_reason") or "").lower()
            closed_as = REVOKED if reason == _NOT_PLANNED else DELIVERED
        return {
            "forge": self.kind,
            "repo": repo or _repo_from_api_url(raw.get("repository_url") or ""),
            "number": raw.get("number"),
            "title": raw.get("title") or "",
            "body": raw.get("body") or "",
            "url": raw.get("html_url") or "",
            "labels": self._labels(raw.get("labels")),
            "state": state,
            "closedAs": closed_as,
        }

    def _note(self, raw: dict) -> dict:
        return {
            "id": raw.get("id"),
            "body": raw.get("body") or "",
            "author": {"username": ((raw.get("user") or {}).get("login") or "")},
        }

    def _change_request(self, raw: dict, repo: str) -> dict:
        head = raw.get("head") or {}
        base = raw.get("base") or {}
        state = str(raw.get("state") or "").lower()
        if raw.get("merged_at") or raw.get("merged"):
            state = "merged"
        return {
            "forge": self.kind,
            "repo": repo,
            "number": raw.get("number"),
            "title": raw.get("title") or "",
            "body": raw.get("body") or "",
            "url": raw.get("html_url") or "",
            "state": state,
            "draft": bool(raw.get("draft")),
            "headSha": head.get("sha") or "",
            "headRef": head.get("ref") or "",
            "baseRef": base.get("ref") or "",
            "labels": self._labels(raw.get("labels")),
            "mergeable": raw.get("mergeable"),
        }

    # -- discovery ------------------------------------------------------

    def assigned_open_issues(self) -> dict[str, list[dict]]:
        """Every open issue assigned to the bot, in ONE cross-repository call
        per page.

        Pull requests come back from the same endpoint and are dropped: they
        are the reviewer's business, and planning one as an issue would spawn
        a solver on the bot's own branch.
        """
        by_repo: dict[str, list[dict]] = {}
        page = 1
        while True:
            batch = self._get("/issues", {"filter": "assigned",
                                          "state": "open",
                                          "per_page": 100, "page": page})
            batch = batch if isinstance(batch, list) else []
            for raw in batch:
                if "pull_request" in raw:
                    continue
                repo = _repo_from_api_url(raw.get("repository_url") or "")
                if not repo:
                    continue
                by_repo.setdefault(repo, []).append(self._issue(raw, repo))
            if len(batch) < 100:
                break
            page += 1
        return by_repo

    def reviewable_change_requests(self, limit: int) -> list[dict]:
        """Open pull requests this bot should review: its OWN, plus any it was
        asked to review.

        TWO SEARCHES, AND THE FIRST ONE IS THE IMPORTANT ONE.

        The obvious design is "review what I was asked to review" — the solver
        requests the bot as reviewer, the reviewer picks it up. That is
        impossible here. GitHub refuses to let a pull request's AUTHOR be
        added as its reviewer:

            422  Review cannot be requested from pull request author.

        The bot authors every pull request it opens, so it can never appear in
        its own `review-requested:` results. Keying discovery on that alone
        deadlocks the whole pipeline silently: the solver waits for a verdict,
        the reviewer never sees the pull request, and every tick costs nothing
        and does nothing — which reads as "the bot is idle" rather than as a
        fault.

        So authorship is the primary signal. `review-requested:` is kept
        because it is the only way to catch a pull request a HUMAN asked the
        bot to look at, including via a team, which authorship would miss.

        Results are merged and de-duplicated: a pull request the bot authored
        AND was somehow requested on must be reviewed once, not twice.

        Search is a different service from the REST API and behaves like one —
        its own rate limit, its own response shape, and eventual consistency —
        which is why exactly these two requests are made and everything else
        is a core-API call.
        """
        login = self.bot_identity()
        if not login:
            return []

        def search(query: str) -> list[dict]:
            data = self._get("/search/issues", {
                "q": query, "sort": "updated", "order": "desc",
                "per_page": str(limit),
            })
            items = data.get("items", []) if isinstance(data, dict) else []
            return [i for i in items if isinstance(i, dict)]

        seen: set[tuple[str, int]] = set()
        out: list[dict] = []
        for query in (f"is:pr is:open author:{login}",
                      f"is:pr is:open review-requested:{login}"):
            for item in search(query):
                repo = _repo_from_api_url(item.get("repository_url") or "")
                key = (repo, item.get("number"))
                if key in seen:
                    continue
                seen.add(key)
                out.append({
                    "forge": self.kind,
                    "repo": repo,
                    "number": item.get("number"),
                    "title": item.get("title") or "",
                    "labels": self._labels(item.get("labels")),
                })
        return out[:limit]

    def accessible_repos(self, limit: int) -> list[str]:
        # `affiliation=owner,collaborator` excludes org-membership noise, and
        # `type` is mutually exclusive with it (the API answers 422), so only
        # affiliation is sent.
        repos = self._get("/user/repos", {
            "affiliation": "owner,collaborator",
            "sort": "pushed", "direction": "desc",
            "per_page": str(limit),
        })
        rows = repos if isinstance(repos, list) else []
        return [r["full_name"] for r in rows][:limit]

    def default_branch_head(self, repo: str) -> tuple[str, str]:
        try:
            raw = self._get(f"/repos/{repo}")
        except Exception:  # noqa: BLE001
            return ("", "")
        branch = (raw or {}).get("default_branch") or "main"
        try:
            data = self._get(f"/repos/{repo}/branches/{branch}")
        except Exception:  # noqa: BLE001
            return (branch, "")
        sha = ((data.get("commit") or {}).get("sha")) or ""
        return (branch, sha)

    # -- issues ---------------------------------------------------------

    def issue(self, repo: str, number: int) -> dict:
        try:
            raw = self._get(f"/repos/{repo}/issues/{number}")
        except Exception:  # noqa: BLE001
            return {}
        return self._issue(raw, repo) if isinstance(raw, dict) else {}

    def comments(self, repo: str, number: int) -> list[dict]:
        rows = self._get(f"/repos/{repo}/issues/{number}/comments",
                         {"per_page": "100"})
        if not isinstance(rows, list):
            raise ForgeError(f"unexpected comments payload for {repo}#{number}")
        return [self._note(r) for r in rows if isinstance(r, dict)]

    def post_comment(self, repo: str, number: int, body: str) -> bool:
        return self._write("POST", f"/repos/{repo}/issues/{number}/comments",
                           {"body": body})

    def add_labels(self, repo: str, number: int, labels) -> bool:
        names = [str(l) for l in (labels or []) if str(l).strip()]
        if not names:
            return True
        return self._write("POST", f"/repos/{repo}/issues/{number}/labels",
                           {"labels": names})

    def remove_label(self, repo: str, number: int, label: str) -> bool:
        enc = urllib.parse.quote(str(label), safe="")
        return self._write("DELETE",
                           f"/repos/{repo}/issues/{number}/labels/{enc}")

    def close_issue(self, repo: str, number: int, delivered: bool) -> bool:
        """Close, recording delivered versus called-off natively.

        `state_reason` is what separates the two here, and it is the reason
        this method takes an intent rather than a string: it is the field the
        status model reads back to tell `Done` from `Won't do`, and a caller
        that had to name the field would have to know it exists.
        """
        return self._write("PATCH", f"/repos/{repo}/issues/{number}", {
            "state": "closed",
            "state_reason": _COMPLETED if delivered else _NOT_PLANNED,
        })

    # -- change requests ------------------------------------------------

    def open_change_requests_for_issue(self, repo: str,
                                       number: int) -> list[int]:
        """Open pull requests that close this issue, by branch or by keyword.

        Both signals are needed: the runner names its branch after the issue,
        while a human writes `closes #n` in the description. A bare mention of
        the number is NOT a link — "unlike #5, this one" would otherwise
        attach somebody else's work to the issue.
        """
        rows = self._get(f"/repos/{repo}/pulls",
                         {"state": "open", "per_page": "100"})
        if not isinstance(rows, list):
            return []
        out = []
        for pr in rows:
            if not isinstance(pr, dict):
                continue
            head = ((pr.get("head") or {}).get("ref") or "")
            body = pr.get("body") or ""
            if head.startswith(f"issue-{number}-") or re.search(
                    rf"\b(clos(e|es|ed)|fix(es|ed)?|resolv(e|es|ed))\s+"
                    rf"#{number}\b", body, re.IGNORECASE):
                out.append(pr.get("number"))
        return [n for n in out if n]

    def change_request(self, repo: str, number: int) -> dict:
        """The pull request itself.

        Needed because neither the search results nor the pulls LIST endpoint
        carries the head sha the review gate turns on.
        """
        try:
            raw = self._get(f"/repos/{repo}/pulls/{number}")
        except Exception:  # noqa: BLE001
            return {}
        return self._change_request(raw, repo) if isinstance(raw, dict) else {}

    def change_request_comments(self, repo: str, number: int) -> list[dict]:
        # A pull request is an issue here, and its conversation lives on the
        # issue endpoint. Review comments are a different thing and are read
        # through review_verdicts.
        return self.comments(repo, number)

    def checks_state(self, repo: str, sha: str) -> str:
        """Two calls, because the two CI mechanisms are two endpoints.

        Actions and anything using the Checks API report as check-runs; older
        integrations post classic commit statuses. A repository can use
        either, or both, and a gate that reads only one of them approves a
        pull request whose real CI is red.

        A lookup that fails is `pending`: an unknown CI state must never read
        as green, and there is no sha to check before there is a commit.
        """
        if not sha:
            return PENDING
        try:
            runs = self._get(f"/repos/{repo}/commits/{sha}/check-runs",
                             {"per_page": "100"})
        except Exception:  # noqa: BLE001
            return PENDING
        try:
            combined = self._get(f"/repos/{repo}/commits/{sha}/status",
                                 {"per_page": "100"})
        except Exception:  # noqa: BLE001
            return PENDING
        return self.reduce_checks(runs, combined)

    @staticmethod
    def reduce_checks(check_runs, combined_status) -> str:
        """Reduce a head commit's CI to one of green / failed / pending / none.

        Precedence is failed > pending > green:
          - a failure is decisive — waiting for the rest of a red build
            changes nothing;
          - anything unfinished, or any conclusion this code does not
            recognise, means wait. Never treat "I don't know what that means"
            as passing.

        `none` is its own answer: no check-runs and no statuses at all. That is
        a repository with no CI wired up, not a build in progress — and it
        must not be confused with `pending`, because an empty combined status
        reports `state: pending`, which a planner that believed it would wait
        on forever.
        """
        runs = (check_runs or {}).get("check_runs") or []
        statuses = (combined_status or {}).get("statuses") or []

        failed = pending = passed = 0

        for run in runs:
            if not isinstance(run, dict):
                continue
            status = str(run.get("status") or "").lower()
            conclusion = str(run.get("conclusion") or "").lower()
            if status != "completed" or not conclusion:
                pending += 1
            elif conclusion in FAILING_CONCLUSIONS:
                failed += 1
            elif conclusion in PASSING_CONCLUSIONS:
                passed += 1
            else:
                pending += 1

        for st in statuses:
            if not isinstance(st, dict):
                continue
            state = str(st.get("state") or "").lower()
            if state == "success":
                passed += 1
            elif state in ("failure", "error"):
                failed += 1
            else:
                # `pending`, and anything unrecognised.
                pending += 1

        if failed:
            return FAILED
        if pending:
            return PENDING
        if passed:
            return GREEN
        return NONE

    def review_verdicts(self, repo: str, number: int) -> list[dict]:
        rows = self._get(f"/repos/{repo}/pulls/{number}/reviews",
                         {"per_page": "100"})
        if not isinstance(rows, list):
            return []
        out = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            out.append({
                "author": ((r.get("user") or {}).get("login") or ""),
                "verdict": _VERDICTS.get(
                    str(r.get("state") or "").upper(), "commented"),
                "body": r.get("body") or "",
                "sha": r.get("commit_id") or "",
            })
        return out

    def merge(self, repo: str, number: int, squash: bool = True,
              delete_branch: bool = True) -> bool:
        if not self._write("PUT", f"/repos/{repo}/pulls/{number}/merge",
                           {"merge_method": "squash" if squash else "merge"}):
            return False
        if delete_branch:
            # Best effort: the merge is the thing that mattered, and a branch
            # that outlives it is untidy rather than wrong.
            pr = self.change_request(repo, number)
            ref = pr.get("headRef") or ""
            if ref:
                self._write("DELETE", f"/repos/{repo}/git/refs/heads/{ref}")
        return True

    def security_findings(self, repo: str, number: int) -> list[dict]:
        try:
            rows = self._get(f"/repos/{repo}/code-scanning/alerts",
                             {"ref": f"refs/pull/{number}/head",
                              "state": "open", "per_page": "100"})
        except Exception:  # noqa: BLE001
            # A repository without code scanning answers 404, and "the feature
            # is off" is not a finding.
            return []
        if not isinstance(rows, list):
            return []
        out = []
        for a in rows:
            if not isinstance(a, dict):
                continue
            rule = a.get("rule") or {}
            out.append({
                "id": a.get("number"),
                "rule": rule.get("id") or "",
                "severity": (rule.get("security_severity_level")
                             or rule.get("severity") or ""),
                "state": a.get("state") or "",
                "title": rule.get("description") or "",
                "url": a.get("html_url") or "",
            })
        return out


_VERDICTS = {
    "APPROVED": "approved",
    "CHANGES_REQUESTED": "changes_requested",
    "COMMENTED": "commented",
    "DISMISSED": "dismissed",
}


def _repo_from_api_url(url: str) -> str:
    """`owner/name` out of an API URL that names a repository.

    Listings and search results name the repository only by its API URL, so
    this is the only way to find out which repository an item belongs to.
    """
    marker = "/repos/"
    text = str(url or "")
    if marker not in text:
        return ""
    return text.split(marker, 1)[1].strip("/")


# ---------------------------------------------------------------------------
# GitLab
# ---------------------------------------------------------------------------


# Pipeline statuses, reduced the same way the check-runs are. `manual` and
# `scheduled` are failures for the same reason `action_required` is: they will
# not run on their own, so waiting for them is waiting forever.
_PIPELINE_PASSING = frozenset({"success", "skipped", "manual_success"})
_PIPELINE_FAILING = frozenset({"failed", "canceled", "cancelled", "manual",
                               "scheduled"})


class GitLabForge(Forge):
    """GitLab, over its REST API.

    Where the two hosts genuinely differ, this class carries the difference so
    that no caller has to:

      - there is no native close reason, so `close_issue` records the intent
        with the status label the model already defines and closes the issue;
      - a merge request has one pipeline rather than two independent check
        systems, so `checks_state` reduces one status instead of merging two;
      - a project is addressed by its URL-encoded path, not by `owner/name`.

    Methods this bot does not need from GitLab yet raise NotImplementedError
    naming what is missing. That is deliberate: an empty list would read to a
    caller as "there is nothing there", which is a different and much quieter
    kind of wrong.
    """

    kind = GITLAB
    change_request_noun = "merge request"

    def __init__(self, url: str, token: str, *, transport=_http):
        self.url = (url or "").rstrip("/")
        self.token = token or ""
        self.api = f"{self.url}/api/v4"
        self._transport = transport
        self._identity: str | None = None

    # -- transport ------------------------------------------------------

    def _headers(self) -> dict:
        return {"PRIVATE-TOKEN": self.token}

    @staticmethod
    def _project(repo: str) -> str:
        """A project path as the API addresses it.

        The path is URL-encoded whole, slashes included, because a GitLab
        project lives at `group/subgroup/name` and the API takes that as one
        path segment.
        """
        return urllib.parse.quote(str(repo or ""), safe="")

    def _get(self, path: str, params: dict | None = None):
        return self._transport("GET", f"{self.api}{path}",
                               headers=self._headers(), params=params)

    def _write(self, method: str, path: str, fields: dict | None = None):
        try:
            self._transport(method, f"{self.api}{path}",
                            headers=self._headers(), form_body=fields or {})
            return True
        except Exception:  # noqa: BLE001 - any failure is "did not write"
            return False

    # -- neutral shapes -------------------------------------------------

    def _issue(self, raw: dict, repo: str = "") -> dict:
        state = str(raw.get("state") or "opened").lower()
        # `opened` and `closed` are the two states; the vocabulary the rest of
        # this bot speaks is `open`.
        neutral_state = "closed" if state == "closed" else "open"
        closed_as = None
        if neutral_state == "closed":
            # There is no native close reason. The status label is where the
            # intent was written down when the issue was closed — see
            # close_issue.
            names = [str(l).strip().lower() for l in (raw.get("labels") or [])]
            revoked = any(n.endswith("wont-do") or n.endswith("won't do")
                          or n.endswith("wontdo") or n.endswith("duplicate")
                          for n in names)
            closed_as = REVOKED if revoked else DELIVERED
        return {
            "forge": self.kind,
            "repo": repo or _project_from_web_url(raw.get("web_url") or "",
                                                  self.url),
            "number": raw.get("iid"),
            "title": raw.get("title") or "",
            # The description is the body everywhere else in this bot, and the
            # destructive-wording guard reads it.
            "body": raw.get("description") or "",
            "url": raw.get("web_url") or "",
            "labels": [str(l) for l in (raw.get("labels") or []) if str(l).strip()],
            "state": neutral_state,
            "closedAs": closed_as,
        }

    @staticmethod
    def _note(raw: dict) -> dict:
        return {
            "id": raw.get("id"),
            "body": raw.get("body") or "",
            "author": {"username": ((raw.get("author") or {}).get("username") or "")},
        }

    def _change_request(self, raw: dict, repo: str) -> dict:
        state = str(raw.get("state") or "").lower()
        neutral = {"opened": "open", "locked": "open",
                   "merged": "merged", "closed": "closed"}.get(state, state)
        return {
            "forge": self.kind,
            "repo": repo,
            "number": raw.get("iid"),
            "title": raw.get("title") or "",
            "body": raw.get("description") or "",
            "url": raw.get("web_url") or "",
            "state": neutral,
            # A draft is spelled in the title here, and also reported as a
            # field on recent versions. Read both, because a title-only draft
            # on an older instance would otherwise be reviewed early.
            "draft": bool(raw.get("draft") or raw.get("work_in_progress")
                          or str(raw.get("title") or "").lower().startswith("draft:")),
            "headSha": raw.get("sha") or "",
            "headRef": raw.get("source_branch") or "",
            "baseRef": raw.get("target_branch") or "",
            "labels": [str(l) for l in (raw.get("labels") or []) if str(l).strip()],
            "mergeable": (None if raw.get("merge_status") in (None, "checking",
                                                              "unchecked")
                          else raw.get("merge_status") == "can_be_merged"),
        }

    # -- identity -------------------------------------------------------

    def bot_identity(self) -> str:
        if self._identity is None:
            me = self._get("/user")
            self._identity = str((me or {}).get("username") or "") \
                if isinstance(me, dict) else ""
        return self._identity

    # -- discovery ------------------------------------------------------

    def assigned_open_issues(self) -> dict[str, list[dict]]:
        """Open work items assigned to the bot, keyed by project path.

        Both issues and TASKS are listed. Tasks share the issue iid space and
        every `/issues/:iid` endpoint, so a solver handles them identically —
        but some versions omit them from the default listing, which is why a
        second explicit pass is made and the two are de-duplicated.
        """
        by_repo: dict[str, list[dict]] = {}
        seen: set[tuple] = set()

        def collect(extra: dict) -> None:
            page = 1
            while True:
                batch = self._get("/issues", {
                    "scope": "assigned_to_me", "state": "opened",
                    "per_page": 100, "page": page, **extra,
                })
                rows = batch if isinstance(batch, list) else []
                for raw in rows:
                    key = (raw.get("project_id"), raw.get("iid"))
                    if key in seen:
                        continue
                    repo = _project_from_web_url(raw.get("web_url") or "",
                                                 self.url)
                    if not repo:
                        continue
                    seen.add(key)
                    by_repo.setdefault(repo, []).append(self._issue(raw, repo))
                if len(rows) < 100:
                    break
                page += 1

        collect({})
        collect({"issue_type": "task"})
        return by_repo

    def reviewable_change_requests(self, limit: int) -> list[dict]:
        """Open merge requests this bot should look at.

        AUTHORSHIP AND REVIEWER-SHIP ARE BOTH REAL SIGNALS HERE, which is the
        one place this differs from the other host: GitLab lets an author be
        a reviewer of their own merge request, so the bot's own work turns up
        under both scopes and the de-duplication is doing real work rather
        than guarding against a theoretical overlap.
        """
        login = self.bot_identity()
        if not login:
            return []
        seen: set[tuple[str, int]] = set()
        out: list[dict] = []
        for scope in ({"author_username": login},
                      {"reviewer_username": login}):
            rows = self._get("/merge_requests", {
                "state": "opened", "scope": "all",
                "order_by": "updated_at", "sort": "desc",
                "per_page": limit, **scope,
            })
            for raw in rows if isinstance(rows, list) else []:
                if not isinstance(raw, dict):
                    continue
                repo = _project_from_web_url(raw.get("web_url") or "", self.url)
                key = (repo, raw.get("iid"))
                if not repo or key in seen:
                    continue
                seen.add(key)
                out.append({
                    "forge": self.kind,
                    "repo": repo,
                    "number": raw.get("iid"),
                    "title": raw.get("title") or "",
                    "labels": [str(l) for l in (raw.get("labels") or [])],
                })
        return out[:limit]

    def accessible_repos(self, limit: int) -> list[str]:
        rows = self._get("/projects", {
            "membership": "true", "order_by": "last_activity_at",
            "sort": "desc", "per_page": limit, "simple": "true",
        })
        out = []
        for raw in rows if isinstance(rows, list) else []:
            path = raw.get("path_with_namespace") if isinstance(raw, dict) else None
            if path:
                out.append(str(path))
        return out[:limit]

    def default_branch_head(self, repo: str) -> tuple[str, str]:
        project = self._project(repo)
        try:
            raw = self._get(f"/projects/{project}")
        except Exception:  # noqa: BLE001
            return ("", "")
        branch = (raw or {}).get("default_branch") or "main"
        try:
            data = self._get(
                f"/projects/{project}/repository/branches/"
                f"{urllib.parse.quote(str(branch), safe='')}")
        except Exception:  # noqa: BLE001
            return (branch, "")
        sha = ((data.get("commit") or {}).get("id")) or ""
        return (branch, sha)

    # -- issues ---------------------------------------------------------

    def issue(self, repo: str, number: int) -> dict:
        try:
            raw = self._get(f"/projects/{self._project(repo)}/issues/{number}")
        except Exception:  # noqa: BLE001
            return {}
        return self._issue(raw, repo) if isinstance(raw, dict) else {}

    def comments(self, repo: str, number: int) -> list[dict]:
        """Notes on an issue, oldest first.

        System notes are dropped. GitLab records label changes, assignments
        and every other bookkeeping event as a note authored by whoever caused
        it, so leaving them in would make "a person replied" true the moment
        the bot itself added a label.
        """
        rows = self._get(f"/projects/{self._project(repo)}/issues/{number}/notes",
                         {"per_page": "100", "sort": "asc",
                          "order_by": "created_at"})
        if not isinstance(rows, list):
            raise ForgeError(f"unexpected notes payload for {repo}#{number}")
        return [self._note(r) for r in rows
                if isinstance(r, dict) and not r.get("system")]

    def post_comment(self, repo: str, number: int, body: str) -> bool:
        return self._write(
            "POST", f"/projects/{self._project(repo)}/issues/{number}/notes",
            {"body": body})

    def add_labels(self, repo: str, number: int, labels) -> bool:
        names = [str(l) for l in (labels or []) if str(l).strip()]
        if not names:
            return True
        return self._write(
            "PUT", f"/projects/{self._project(repo)}/issues/{number}",
            {"add_labels": ",".join(names)})

    def remove_label(self, repo: str, number: int, label: str) -> bool:
        return self._write(
            "PUT", f"/projects/{self._project(repo)}/issues/{number}",
            {"remove_labels": str(label)})

    def close_issue(self, repo: str, number: int, delivered: bool) -> bool:
        """Close, recording the intent WITHOUT a native close reason.

        There is no field here that separates "shipped" from "called off", so
        the status label carries it: work that was called off is labelled
        before the close, and work that shipped is closed with the terminal
        labels cleared. The caller never learns any of that — it says which of
        the two happened and this decides how to write it down.
        """
        path = f"/projects/{self._project(repo)}/issues/{number}"
        fields = {"state_event": "close"}
        if delivered:
            fields["remove_labels"] = "status::wont-do,status::duplicate"
        else:
            fields["add_labels"] = "status::wont-do"
        return self._write("PUT", path, fields)

    # -- change requests ------------------------------------------------

    def open_change_requests_for_issue(self, repo: str,
                                       number: int) -> list[int]:
        rows = self._get(
            f"/projects/{self._project(repo)}/issues/{number}"
            "/related_merge_requests", {"per_page": "100"})
        out = []
        for raw in rows if isinstance(rows, list) else []:
            if not isinstance(raw, dict):
                continue
            if str(raw.get("state") or "").lower() != "opened":
                continue
            if raw.get("iid"):
                out.append(raw["iid"])
        return out

    def change_request(self, repo: str, number: int) -> dict:
        try:
            raw = self._get(
                f"/projects/{self._project(repo)}/merge_requests/{number}")
        except Exception:  # noqa: BLE001
            return {}
        return self._change_request(raw, repo) if isinstance(raw, dict) else {}

    def change_request_comments(self, repo: str, number: int) -> list[dict]:
        rows = self._get(
            f"/projects/{self._project(repo)}/merge_requests/{number}/notes",
            {"per_page": "100", "sort": "asc", "order_by": "created_at"})
        if not isinstance(rows, list):
            raise ForgeError(
                f"unexpected notes payload for {repo}!{number}")
        return [self._note(r) for r in rows
                if isinstance(r, dict) and not r.get("system")]

    def checks_state(self, repo: str, sha: str) -> str:
        """The newest pipeline for one commit, reduced the same four ways.

        One pipeline, not two check systems — but the reduction has to land on
        the same four answers, or the callers would need to know which host
        they were gated by. A commit with no pipeline at all is `none`, never
        `pending`: nothing is going to arrive.
        """
        if not sha:
            return PENDING
        try:
            rows = self._get(f"/projects/{self._project(repo)}/pipelines", {
                "sha": sha, "per_page": "1",
                "order_by": "id", "sort": "desc",
            })
        except Exception:  # noqa: BLE001
            return PENDING
        rows = [r for r in (rows or []) if isinstance(r, dict)]
        if not rows:
            return NONE
        return self.reduce_pipeline(str(rows[0].get("status") or ""))

    @staticmethod
    def reduce_pipeline(status: str) -> str:
        """One pipeline status → green / failed / pending / none."""
        state = str(status or "").strip().lower()
        if not state:
            return PENDING
        if state in _PIPELINE_FAILING:
            return FAILED
        if state in _PIPELINE_PASSING:
            return GREEN
        # `created`, `waiting_for_resource`, `preparing`, `pending`,
        # `running`, and anything a newer version invents. Unknown waits.
        return PENDING

    def review_verdicts(self, repo: str, number: int) -> list[dict]:
        """Who approved this merge request.

        Approvals are the only structured verdict here — there is no
        "changes requested" state — so a rejection travels as an ordinary
        note and is read through `change_request_comments`.
        """
        try:
            raw = self._get(
                f"/projects/{self._project(repo)}/merge_requests/{number}"
                "/approvals")
        except Exception:  # noqa: BLE001
            return []
        sha = (raw or {}).get("sha") or ""
        out = []
        for who in (raw or {}).get("approved_by") or []:
            user = (who or {}).get("user") or {}
            out.append({
                "author": user.get("username") or "",
                "verdict": "approved",
                "body": "",
                "sha": sha,
            })
        return out

    def merge(self, repo: str, number: int, squash: bool = True,
              delete_branch: bool = True) -> bool:
        return self._write(
            "PUT",
            f"/projects/{self._project(repo)}/merge_requests/{number}/merge",
            {"squash": "true" if squash else "false",
             "should_remove_source_branch": "true" if delete_branch else "false"})

    def security_findings(self, repo: str, number: int) -> list[dict]:
        raise NotImplementedError(
            "GitLab security findings are not read yet: they live behind the "
            "merge request's vulnerability report, which needs an Ultimate "
            "licence and a different endpoint from the one this bot uses "
            "elsewhere. Nothing calls this on GitLab today.")


def _project_from_web_url(web_url: str, base: str) -> str:
    """The project path out of a browser URL.

    Work items are served under `/-/work_items/<iid>` on newer versions and
    `/-/issues/<iid>` on older ones, and merge requests under
    `/-/merge_requests/<iid>`. Anything else is not ours to interpret, and
    guessing would attach an item to the wrong project.
    """
    prefix = f"{str(base or '').rstrip('/')}/"
    text = str(web_url or "")
    if not prefix or not text.startswith(prefix):
        return ""
    rest = text[len(prefix):]
    for marker in ("/-/issues/", "/-/work_items/", "/-/merge_requests/"):
        if marker in rest:
            return rest.split(marker)[0]
    return ""


# ---------------------------------------------------------------------------
# selection
# ---------------------------------------------------------------------------


class Forges:
    """The configured hosts, and which one a given item came from.

    Selection is per item. Every record discovery hands back carries the
    `forge` it came from, and `of()` reads that stamp — there is no mode flag
    anywhere, because both hosts are worked in the same tick and a switch
    would mean choosing one of them per deployment.

    Repositories that were NOT discovered (the tester's candidates come from
    the owner's permitted list, which is a list of names and nothing more) are
    routed by what discovery recorded for them earlier in the tick, and
    failing that by the first configured host. With one host configured that
    is exactly today's behaviour; with two it is the reason `remember()`
    exists.
    """

    def __init__(self, forges=()):
        self._forges = list(forges)
        self._by_kind = {f.kind: f for f in self._forges}
        self._by_repo: dict[str, Forge] = {}

    def __iter__(self):
        return iter(self._forges)

    def __len__(self):
        return len(self._forges)

    def __bool__(self):
        return bool(self._forges)

    def kinds(self) -> list[str]:
        return [f.kind for f in self._forges]

    def by_kind(self, kind: str):
        return self._by_kind.get(kind)

    def remember(self, repo: str, f: Forge) -> None:
        """Record where a repository was discovered."""
        if repo:
            self._by_repo[repo] = f

    def of(self, item):
        """The forge an item belongs to.

        `item` is either a record carrying a `forge` stamp, or a repository
        name. Raises when nothing is configured at all, because a caller
        acting on "no forge" would silently do nothing.
        """
        if not self._forges:
            raise ForgeError("no forge is configured")
        if isinstance(item, dict):
            stamped = self._by_kind.get(item.get("forge") or "")
            if stamped is not None:
                return stamped
            item = item.get("repo") or ""
        found = self._by_repo.get(str(item or ""))
        return found if found is not None else self._forges[0]

    # -- cross-host discovery -------------------------------------------

    def assigned_open_issues(self) -> dict[str, list[dict]]:
        """Every host's assigned open issues, merged and remembered.

        A repository name is unique within a host but not across them, so the
        mapping recorded here is what lets every later question about that
        repository go back to the host that answered the first one.
        """
        by_repo: dict[str, list[dict]] = {}
        for f in self._forges:
            for repo, issues in f.assigned_open_issues().items():
                self.remember(repo, f)
                by_repo.setdefault(repo, []).extend(issues)
        return by_repo

    def reviewable_change_requests(self, limit: int) -> list[dict]:
        """Every host's review candidates, merged and remembered.

        The cap is applied per host and then again to the merged list, so one
        busy host cannot crowd the other out of a tick entirely.
        """
        out: list[dict] = []
        for f in self._forges:
            for item in f.reviewable_change_requests(limit):
                self.remember(item.get("repo") or "", f)
                out.append(item)
        return out[:limit]

    def accessible_repos(self, limit: int) -> list[str]:
        out: list[str] = []
        for f in self._forges:
            for repo in f.accessible_repos(limit):
                self.remember(repo, f)
                out.append(repo)
        return out[:limit]


def configured(env=None) -> Forges:
    """The hosts this deployment has credentials for.

    Each is skipped when its credentials are unset, and skipped SILENTLY:
    "GitLab is not configured here" is the normal state of most deployments,
    not a fault to report. A deployment with only one set of credentials
    therefore constructs exactly one implementation and behaves precisely as
    it did before there were two.
    """
    env = os.environ if env is None else env
    out = []
    token = env.get("GITHUB_TOKEN", "")
    if token:
        out.append(GitHubForge(
            token,
            env.get("GITHUB_API") or env.get("GITHUB_API_URL")
            or "https://api.github.com"))
    gitlab_url = (env.get("GITLAB_URL", "") or "").rstrip("/")
    gitlab_token = env.get("GITLAB_API_TOKEN", "")
    if gitlab_url and gitlab_token:
        out.append(GitLabForge(gitlab_url, gitlab_token))
    return Forges(out)
