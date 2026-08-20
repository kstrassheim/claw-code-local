"""A forge that answers out of a dictionary, for the planner tests.

WHY A FAKE AND NOT A MOCK OF THE HTTP LAYER
-------------------------------------------
The planners' job is to decide things — what to spawn, in what order, what to
park — and every one of those decisions is now expressed against the forge
interface rather than against anybody's REST API. A test that stubbed HTTP
would be testing the interface's implementation twice and the decision once.

So the planner tests drive THIS, which implements the interface for real and
holds its answers in memory. Nothing leaves the process, no test needs a token,
and a change to the request shapes cannot make a planner test go green or red
— that is what the forge's own tests are for.

It subclasses `forge.Forge`, so a method added to the interface and forgotten
here fails loudly at construction rather than silently answering None.
"""

from __future__ import annotations

# Imported for its side effect: harness puts builder/ on sys.path, which is
# how the runtime imports these modules too. Without it `import forge` finds
# nothing, and the failure names the wrong module.
import harness  # noqa: F401

import forge


class FakeForge(forge.Forge):
    """An in-memory code host.

    Set the attributes a test cares about and leave the rest alone; every
    answer has an empty default, and the WRITES are recorded rather than
    performed so a test can assert that a gate wrote nothing at all.
    """

    def __init__(self, kind: str = forge.GITHUB, *, identity: str = "bot",
                 noun: str = "pull request"):
        self.kind = kind
        self.change_request_noun = noun
        self.identity = identity

        # What the host would answer.
        self.issues: list[dict] = []          # every assigned open issue
        self.notes: dict[int, list[dict]] = {}         # issue number -> notes
        self.change_requests: dict[int, dict] = {}     # number -> record
        self.change_request_notes: dict[int, list[dict]] = {}
        self.linked: dict[int, list[int]] = {}         # issue -> change requests
        self.verdicts: dict[int, list[dict]] = {}
        self.checks: dict[str, str] = {}               # sha -> state
        self.default_checks = forge.NONE
        self.repos: list[str] = []
        self.heads: dict[str, tuple[str, str]] = {}
        self.candidates: list[dict] = []
        self.findings: dict[int, list[dict]] = {}

        # What it refuses to answer. `raises` is a mapping from method name to
        # the exception to raise, so a test can make exactly one question fail
        # — which is how the real failures arrive.
        self.raises: dict[str, Exception] = {}

        # Writes, in order, as (kind, repo, number, payload). Never performed.
        self.writes: list[tuple] = []
        # When true every write reports failure, which several gates treat as
        # "leave the item alone".
        self.writes_fail = False

    # -- helpers a test uses ---------------------------------------------

    def _maybe_raise(self, name: str) -> None:
        if name in self.raises:
            raise self.raises[name]

    def _record(self, kind: str, repo: str, number: int, payload) -> bool:
        if self.writes_fail:
            return False
        self.writes.append((kind, repo, number, payload))
        return True

    def writes_of(self, kind: str) -> list:
        """Every payload written by one kind of write, in order."""
        return [w[3] for w in self.writes if w[0] == kind]

    # -- identity ---------------------------------------------------------

    def bot_identity(self) -> str:
        self._maybe_raise("bot_identity")
        return self.identity

    # -- discovery --------------------------------------------------------

    def assigned_open_issues(self) -> dict[str, list[dict]]:
        self._maybe_raise("assigned_open_issues")
        by_repo: dict[str, list[dict]] = {}
        for i in self.issues:
            by_repo.setdefault(i.get("repo") or "", []).append(i)
        return by_repo

    def reviewable_change_requests(self, limit: int) -> list[dict]:
        self._maybe_raise("reviewable_change_requests")
        return list(self.candidates)[:limit]

    def accessible_repos(self, limit: int) -> list[str]:
        self._maybe_raise("accessible_repos")
        return list(self.repos)[:limit]

    def default_branch_head(self, repo: str) -> tuple[str, str]:
        self._maybe_raise("default_branch_head")
        return self.heads.get(repo, ("", ""))

    # -- issues -----------------------------------------------------------

    def issue(self, repo: str, number: int) -> dict:
        self._maybe_raise("issue")
        for i in self.issues:
            if i.get("repo") == repo and i.get("number") == number:
                return i
        return {}

    def comments(self, repo: str, number: int) -> list[dict]:
        self._maybe_raise("comments")
        return list(self.notes.get(number, []))

    def post_comment(self, repo: str, number: int, body: str) -> bool:
        return self._record("comment", repo, number, body)

    def add_labels(self, repo: str, number: int, labels) -> bool:
        return self._record("labels", repo, number, list(labels))

    def remove_label(self, repo: str, number: int, label: str) -> bool:
        return self._record("unlabel", repo, number, label)

    def close_issue(self, repo: str, number: int, delivered: bool) -> bool:
        return self._record("close", repo, number, delivered)

    # -- change requests --------------------------------------------------

    def open_change_requests_for_issue(self, repo: str,
                                       number: int) -> list[int]:
        self._maybe_raise("open_change_requests_for_issue")
        return list(self.linked.get(number, []))

    def change_request(self, repo: str, number: int) -> dict:
        self._maybe_raise("change_request")
        return dict(self.change_requests.get(number, {}))

    def change_request_comments(self, repo: str, number: int) -> list[dict]:
        self._maybe_raise("change_request_comments")
        return list(self.change_request_notes.get(number, []))

    def checks_state(self, repo: str, sha: str) -> str:
        self._maybe_raise("checks_state")
        if not sha:
            return forge.PENDING
        return self.checks.get(sha, self.default_checks)

    def review_verdicts(self, repo: str, number: int) -> list[dict]:
        self._maybe_raise("review_verdicts")
        return list(self.verdicts.get(number, []))

    def merge(self, repo: str, number: int, squash: bool = True,
              delete_branch: bool = True) -> bool:
        return self._record("merge", repo, number,
                            {"squash": squash, "deleteBranch": delete_branch})

    def security_findings(self, repo: str, number: int) -> list[dict]:
        self._maybe_raise("security_findings")
        return list(self.findings.get(number, []))


def note(body: str, author: str, note_id: int = 0) -> dict:
    """One comment, in the shape every forge hands back."""
    return {"id": note_id, "body": body, "author": {"username": author}}
