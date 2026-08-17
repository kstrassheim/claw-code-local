"""Which merge request actually DELIVERED an issue — if any did.

WHAT THIS IS FOR
The solver marks a story delivered by writing `mergedAt` on its planning
document. Every report downstream is built on that field: completed points,
velocity, the burndown, the sprint's scope accounting, and "was this story
re-worked after it shipped". A wrong `mergedAt` is not a wrong number in one
report — it is a wrong number in all of them, and it does not look wrong.

The runner had asked GitLab `/issues/:iid/related_merge_requests` and taken the
last merged entry. That endpoint answers a LOOSER question than the one being
asked: it lists every MR that so much as mentions the issue.

THE FAILURE THIS WAS WRITTEN FROM
601/cloud/maintenance#63, read on 2026-08-10. The endpoint returned four MRs:

    !1    merged 2025-02-11  "Permit sp site access to app delegated"
    !132  opened             "Azure signing service …"      ← the real one
    !137  merged 2026-08-09  "Test setup: silence noise …"
    !138  merged 2026-08-09  "Fix CosmosDB publish …"

Three merged MRs, none of which closes #63, and one of them merged eighteen
months before the issue was created. The story was recorded as delivered with
mrOpenedAt and mergedAt three seconds apart in February 2025 — while its own
MR was still open and conflicted. Read back out of the store it looked like
the bot had re-processed a long-merged issue, so the anomaly it produced was
not even the anomaly it was.

THE RULE
Mentioning is not closing. An MR delivered this issue only if:

  - it is the branch this runner works the issue on, which is the ordinary
    case and needs no text at all; or
  - it says so with a closing keyword — GitLab's own vocabulary, since that is
    what actually makes GitLab close the issue on merge.

And when more than one qualifies, the NEWEST merge wins. The endpoint's order
is not documented as chronological, and "the last element" got it wrong the
one time it mattered.
"""

from __future__ import annotations

import datetime
import re


def _ts(value):
    """A GitLab timestamp as a comparable value, or None.

    Not string comparison. GitLab hands out both '…Z' and '…+02:00', and the
    two orderings disagree: 14:30+02:00 IS 12:30Z, earlier than 13:00Z, but as
    text it sorts after it. Every project here is on a +01:00/+02:00 offset,
    so comparing the strings would be wrong locally and right in CI — the
    worst way for a comparison to be wrong. Same rule, and the same reason, as
    merge_approval._ts.
    """
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed

# GitLab's own closing vocabulary, and only it. This module decides whether an
# issue was DELIVERED; honouring wording GitLab ignores would record a delivery
# that never happened, and the platform's list is the definition of the thing
# being detected.
_KEYWORD = re.compile(
    r"\b(?:clos(?:e|es|ed|ing)|fix(?:e|es|ed|ing)?"
    r"|resolv(?:e|es|ed|ing)|implement(?:s|ed|ing)?)\b:?",
    re.IGNORECASE)

# What may follow a keyword and still be part of the same closing statement:
# "#63", "issue #63", ", #64", "and #65". Anchored with .match() at the point
# the previous reference ended, so the list is CONSUMED rather than searched —
# that is what stops "Closes the gap … discussed in #63" from reading as a
# closing reference, and what lets "Closes #63, #64" close both.
_REFERENCE = re.compile(r"\s*(?:and\s+)?,?\s*(?:issues?\s+)?#(\d+)",
                        re.IGNORECASE)


def closed_issues(description) -> set:
    """Every issue number this description actually CLOSES, as strings."""
    text = str(description or "")
    found = set()
    for keyword in _KEYWORD.finditer(text):
        position = keyword.end()
        while True:
            reference = _REFERENCE.match(text, position)
            if not reference:
                break
            found.add(reference.group(1))
            position = reference.end()
    return found


def closes(mr, iid, branch: str = "") -> bool:
    """Does this merge request close issue `iid`?"""
    if not isinstance(mr, dict):
        return False
    if branch and mr.get("source_branch") == branch:
        return True
    return str(iid) in closed_issues(mr.get("description"))


def pick(mrs, iid, branch: str = "", not_before: str = ""):
    """The MR that delivered issue `iid`, or None.

    `not_before` is the issue's creation time, when the caller has it. A merge
    that predates the issue cannot have delivered it — that is what !1 above
    was — and this is the one check that does not depend on anybody having
    written the reference correctly. Omitted rather than guessed when the
    caller has no creation time: a missing guard is better than one that
    silently drops real deliveries because a clock disagreed.
    """
    candidates = [m for m in (mrs or [])
                  if isinstance(m, dict)
                  and m.get("state") == "merged"
                  and closes(m, iid, branch)]
    floor = _ts(not_before)
    if floor is not None:
        # An unparseable or absent merge time is KEPT. The guard exists to
        # throw out a merge that provably predates the issue; a timestamp it
        # cannot read proves nothing, and dropping the story on that would
        # lose a real delivery to a formatting change.
        candidates = [m for m in candidates
                      if _ts(m.get("merged_at")) is None
                      or _ts(m["merged_at"]) >= floor]
    if not candidates:
        return None
    epoch = datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)
    return max(candidates, key=lambda m: _ts(m.get("merged_at")) or epoch)
