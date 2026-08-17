"""project_allowlist: which projects is the bot PERMITTED to work on?

Imported by `project-allow` (the chat-facing CLI that edits the list) and by
all three planners — heartbeat-issue-tick, tester-tick, reviewer-tick — so the
four of them cannot drift apart on what a project path is or what an absent
list means. Same reasoning as project-kind.sh for project detection.

THE PROBLEM THIS SOLVES
-----------------------
Every subsystem discovered its own work from the GitLab API, and each of those
queries is instance-wide:

  issue-solver   /issues?scope=assigned_to_me     — any project, if assigned
  reviewer       /merge_requests?reviewer_id=<bot> — any project, if reviewer
  tester         /projects?membership=true         — every project it is in

So anyone anywhere on the instance could put the bot to work on their repo by
assigning it an issue, and the tester would start testing a project the moment
the bot was added as a member. Membership and assignment are not permission —
they are how you ASK. The allowlist is the answer, and it is kept by the bot's
owner from chat (`projects add|revoke|list`, see k8s/053-projects.yaml).

WHERE IT LIVES
--------------
~/.openclaw/projects-allowed.list, on the workspace PVC — so it survives pod
restarts and redeploys, which the previous TESTER_GITLAB_PROJECTS env var did
not (that needed a secret edit and a rollout to change).

One project path per line, `#` comments and blank lines ignored:

    # granted 2026-07-31 by konstantin.strassheim
    601/ai/claw-code-web-test
    common/claw-code

Edits go through `project-allow`, which writes atomically and appends to
projects-allowed.log. Nothing stops a human editing the file by hand; the
parser is deliberately forgiving about whitespace, URLs and .git suffixes so a
hand edit is unlikely to silently mean nothing.

FAIL CLOSED
-----------
No file, empty file, unreadable file, pod unreachable → NO project is allowed
and every subsystem idles. The alternative (fall back to "everything") would
turn any read failure into exactly the unrestricted behaviour this exists to
prevent. An idle tick costs one cycle and is visible in the plan JSON as a
`reason`; a permissive tick is the bot working on a repo nobody permitted.
"""

import os
import re
import unicodedata

# Relative to the openclaw state dir, so both in-pod code (read_local) and the
# planners' `kubectl exec` snippet (POD_READ_SNIPPET) name the same file once.
LIST_REL = "projects-allowed.list"
LOG_REL = "projects-allowed.log"

# A GitLab path_with_namespace: at least one namespace segment and a project
# segment. GitLab itself allows [A-Za-z0-9_.-] per segment (leading char must
# be alphanumeric); subgroups nest with '/'.
_SEGMENT = r"[A-Za-z0-9][A-Za-z0-9._-]*"
_PATH_RE = re.compile(rf"^{_SEGMENT}(?:/{_SEGMENT})+$")


def state_root() -> str:
    return os.path.join(os.environ.get("HOME", "/home/node"), ".openclaw")


def list_path() -> str:
    return os.path.join(state_root(), LIST_REL)


def log_path() -> str:
    return os.path.join(state_root(), LOG_REL)


def normalize(raw: str) -> str:
    """A user-typed project reference → bare path_with_namespace, or "" if it
    could not be one.

    Accepts what a human actually pastes into chat: the browser URL, the clone
    URL, a stray trailing slash. Getting this wrong is expensive in a way a
    normal parse error is not — a path that does not match is not a loud
    failure, it is the bot quietly never working on that project again — so it
    is worth being generous here and strict in the regex at the end.
    """
    s = (raw or "").strip().strip("\"'")
    if not s:
        return ""
    # Unicode dashes: a path pasted out of a chat client or a wiki table often
    # comes back with an en-dash where the user typed a hyphen. It would match
    # nothing, forever, and look identical in the reply.
    s = "".join("-" if unicodedata.category(c) == "Pd" else c for c in s)
    # git@host:group/name.git
    if s.startswith("git@") and ":" in s:
        s = s.split(":", 1)[1]
    # scheme://host/group/name — drop scheme, host, and any userinfo
    if "://" in s:
        s = s.split("://", 1)[1]
        s = s.split("/", 1)[1] if "/" in s else ""
    # GitLab web URLs continue past the project: /-/issues/7, /-/tree/main, ...
    if "/-/" in s:
        s = s.split("/-/", 1)[0]
    s = s.strip("/")
    if s.endswith(".git"):
        s = s[: -len(".git")]
    s = s.strip("/")
    return s if _PATH_RE.match(s) else ""


def parse(text: str) -> list[str]:
    """File content → ordered, de-duplicated project paths. Unparseable lines
    are dropped: the file is a permission grant, and a line that cannot be
    understood has not granted anything."""
    out: list[str] = []
    seen: set[str] = set()
    for line in (text or "").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        path = normalize(line)
        if not path:
            continue
        key = path.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(path)
    return out


class Allowlist:
    """The permitted set, plus whether we managed to READ it at all.

    `available` is the distinction that matters: an empty list means the owner
    has permitted nothing, an unavailable one means we could not find out.
    Both deny — see FAIL CLOSED above — but they are different operational
    situations and the planners report them with different reasons.
    """

    def __init__(self, entries: list[str], available: bool = True) -> None:
        self.entries = entries
        self.available = available
        self._keys = {e.casefold() for e in entries}

    def __len__(self) -> int:
        return len(self.entries)

    def __contains__(self, path: str) -> bool:
        return self.allows(path)

    def allows(self, path: str) -> bool:
        if not self.available:
            return False
        norm = normalize(path)
        # Compare case-insensitively: GitLab treats the path as case-insensitive
        # for lookup, so `601/AI/foo` and `601/ai/foo` are one project and must
        # not be two permission states.
        return bool(norm) and norm.casefold() in self._keys

    def deny_reason(self, path: str) -> str:
        """Short machine-ish reason for a plan entry / log line. "" if allowed."""
        if self.allows(path):
            return ""
        if not self.available:
            return "allowlist-unavailable"
        if not self.entries:
            return "allowlist-empty"
        return "not-permitted"

    @staticmethod
    def denied() -> "Allowlist":
        return Allowlist([], available=False)


def read_local() -> Allowlist:
    """Read the list from this container's own filesystem. Used inside the
    openclaw pod (the CLI, and the runners' guard)."""
    try:
        with open(list_path(), encoding="utf-8") as f:
            return Allowlist(parse(f.read()))
    except FileNotFoundError:
        # Never permitted anything yet. Empty, but we did read it.
        return Allowlist([])
    except OSError:
        return Allowlist.denied()


# --- planner side: reading the list out of the openclaw pod ----------------
#
# The planners run in their own CronJob pods and already `kubectl exec` into
# the openclaw pod once per tick to inspect locks and state markers. The
# allowlist lives on that pod's PVC, so it rides along in the SAME exec as one
# more section rather than costing another round trip.
#
# `|| true` matters: those scripts run under `set -eu`, where a missing file
# would abort the whole state query and take the lock/marker sections with it.

ALLOWED_MARKER = "==ALLOWED=="


def pod_read_snippet() -> str:
    return (
        f'echo "{ALLOWED_MARKER}"; '
        f'cat "$HOME/.openclaw/{LIST_REL}" 2>/dev/null || true; '
    )


def from_section(lines: list[str], available: bool = True) -> Allowlist:
    """Build the allowlist from the lines the exec printed under ==ALLOWED==."""
    if not available:
        return Allowlist.denied()
    return Allowlist(parse("\n".join(lines)))
