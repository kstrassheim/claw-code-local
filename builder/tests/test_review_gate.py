"""What the reviewer planner decides, and why it may not get it wrong.

THE TWO DECISIONS THIS PINS
---------------------------
1. **Is the head green?** GitHub reports CI in two unrelated places — the
   Checks API (GitHub Actions and anything modern) and classic commit
   statuses (older integrations) — and neither the search results nor the
   pull-request list endpoint carries either. A gate that read only one of
   them would review a pull request whose real CI is red, and a gate that
   read neither would review everything.

2. **Has this already been reviewed?** Keyed on the head SHA alone, a verdict
   about the DESCRIPTION could never be cleared: the author edits the body,
   the SHA does not move, nothing looks again, and the pull request sits
   until a human intervenes. The record therefore carries the SHA *and* a
   digest of the prose — see review_subject.py.

No network: gh_get is replaced where a call would otherwise be made.
"""

import unittest

from harness import load, load_script

rt = load_script("reviewer-tick.py")
rs = load("review_subject")


def check_runs(*specs):
    """A check-runs payload. Each spec is (status, conclusion)."""
    return {"check_runs": [{"status": s, "conclusion": c} for s, c in specs]}


def statuses(*states):
    """A combined-status payload with one classic status per state."""
    return {"state": states[0] if states else "pending",
            "statuses": [{"state": s} for s in states]}


NO_RUNS = {"check_runs": []}
# What GitHub answers for a commit nobody posted a status on: state `pending`
# with an empty list. Reading the top-level state alone makes every such
# commit look like a build in progress, forever.
NO_STATUSES = {"state": "pending", "statuses": [], "total_count": 0}


class CheckGate(unittest.TestCase):
    def test_all_successful_is_green(self):
        self.assertEqual(
            rt.check_state(check_runs(("completed", "success"),
                                      ("completed", "success")), NO_STATUSES),
            rt.GREEN)

    def test_neutral_and_skipped_count_as_passing(self):
        # A skipped job is not a failure, and a path-filtered workflow skips
        # constantly. Treating either as "not green" would mean the reviewer
        # never runs on a repo that uses path filters.
        self.assertEqual(
            rt.check_state(check_runs(("completed", "success"),
                                      ("completed", "neutral"),
                                      ("completed", "skipped")), NO_STATUSES),
            rt.GREEN)

    def test_a_failure_is_not_green(self):
        self.assertEqual(
            rt.check_state(check_runs(("completed", "success"),
                                      ("completed", "failure")), NO_STATUSES),
            rt.FAILED)

    def test_timed_out_and_cancelled_are_failures(self):
        for conclusion in ("timed_out", "cancelled", "canceled"):
            with self.subTest(conclusion=conclusion):
                self.assertEqual(
                    rt.check_state(check_runs(("completed", conclusion)),
                                   NO_STATUSES),
                    rt.FAILED)

    def test_action_required_is_a_failure_not_a_wait(self):
        # It will never turn green on its own, so waiting for it is waiting
        # forever.
        self.assertEqual(
            rt.check_state(check_runs(("completed", "action_required")),
                           NO_STATUSES),
            rt.FAILED)

    def test_queued_and_in_progress_are_pending(self):
        for status in ("queued", "in_progress"):
            with self.subTest(status=status):
                self.assertEqual(
                    rt.check_state(check_runs((status, None)), NO_STATUSES),
                    rt.PENDING)

    def test_a_failure_beats_a_pending(self):
        # Waiting for the rest of a red build changes nothing.
        self.assertEqual(
            rt.check_state(check_runs(("queued", None),
                                      ("completed", "failure")), NO_STATUSES),
            rt.FAILED)

    def test_a_pending_beats_a_success(self):
        self.assertEqual(
            rt.check_state(check_runs(("completed", "success"),
                                      ("in_progress", None)), NO_STATUSES),
            rt.PENDING)

    def test_an_unrecognised_conclusion_waits_rather_than_passes(self):
        # "I don't know what that means" must never read as green.
        self.assertEqual(
            rt.check_state(check_runs(("completed", "brand_new_thing")),
                           NO_STATUSES),
            rt.PENDING)

    def test_a_stale_run_waits_for_the_fresh_one(self):
        self.assertEqual(
            rt.check_state(check_runs(("completed", "stale")), NO_STATUSES),
            rt.PENDING)

    def test_classic_statuses_are_read_too(self):
        # A repo using only commit statuses has no check-runs at all. Reading
        # only the Checks API would call that green.
        self.assertEqual(rt.check_state(NO_RUNS, statuses("success")), rt.GREEN)
        self.assertEqual(rt.check_state(NO_RUNS, statuses("failure")), rt.FAILED)
        self.assertEqual(rt.check_state(NO_RUNS, statuses("error")), rt.FAILED)
        self.assertEqual(rt.check_state(NO_RUNS, statuses("pending")), rt.PENDING)

    def test_a_red_classic_status_sinks_green_check_runs(self):
        self.assertEqual(
            rt.check_state(check_runs(("completed", "success")),
                           statuses("failure")),
            rt.FAILED)

    def test_no_ci_at_all_is_its_own_answer(self):
        # NOT pending: an empty combined status reports state=pending, and a
        # planner that believed it would wait forever on every repo with no
        # CI configured.
        self.assertEqual(rt.check_state(NO_RUNS, NO_STATUSES), rt.NONE)

    def test_missing_payloads_are_treated_as_no_ci(self):
        self.assertEqual(rt.check_state(None, None), rt.NONE)
        self.assertEqual(rt.check_state({}, {}), rt.NONE)


class CheckGateFetch(unittest.TestCase):
    """head_check_state pairs the two endpoints — and fails towards waiting."""

    def setUp(self):
        self._real = rt.gh_get
        self.urls = []

    def tearDown(self):
        rt.gh_get = self._real

    def _serve(self, runs=NO_RUNS, combined=NO_STATUSES, raise_on=None):
        def fake(url, params=None):
            self.urls.append(url)
            if raise_on and raise_on in url:
                raise RuntimeError("GitHub 500")
            if "check-runs" in url:
                return runs
            return combined
        rt.gh_get = fake

    def test_it_asks_both_endpoints(self):
        self._serve(check_runs(("completed", "success")))
        rt.head_check_state("o/r", "abc123")
        self.assertTrue(any(u.endswith("/commits/abc123/check-runs")
                            for u in self.urls), self.urls)
        self.assertTrue(any(u.endswith("/commits/abc123/status")
                            for u in self.urls), self.urls)

    def test_a_failed_lookup_is_pending_never_green(self):
        # An unknown CI state must not be able to trigger a review.
        self._serve(check_runs(("completed", "success")), raise_on="check-runs")
        self.assertEqual(rt.head_check_state("o/r", "abc123"), rt.PENDING)
        self._serve(check_runs(("completed", "success")), raise_on="/status")
        self.assertEqual(rt.head_check_state("o/r", "abc123"), rt.PENDING)

    def test_no_sha_is_pending_and_costs_no_call(self):
        self._serve()
        self.assertEqual(rt.head_check_state("o/r", ""), rt.PENDING)
        self.assertEqual(self.urls, [])


class AlreadyReviewed(unittest.TestCase):
    """The fingerprint that lets an edited description clear a verdict."""

    SHA = "c6ca71f0000000000000000000000000000000aa"

    def stamp(self, title, body, sha=None):
        return rs.stamp(sha or self.SHA, title, body)

    def test_the_same_commit_and_prose_is_skipped(self):
        stored = self.stamp("Add versions endpoint", "Closes #5")
        self.assertTrue(rs.already_reviewed(
            stored, self.SHA, "Add versions endpoint", "Closes #5"))

    def test_editing_the_DESCRIPTION_clears_the_verdict(self):
        # The regression this module exists for. The reviewer asked for the
        # "Closes #5" line to go; the author removed it; the SHA did not move.
        # Keyed on the SHA alone nothing looked again, and both sides waited
        # correctly while nothing moved.
        stored = self.stamp("Add versions endpoint", "Closes #5")
        self.assertFalse(rs.already_reviewed(
            stored, self.SHA, "Add versions endpoint", "part of #5"))

    def test_editing_the_TITLE_clears_it_too(self):
        stored = self.stamp("Add versions endpoint", "Closes #5")
        self.assertFalse(rs.already_reviewed(
            stored, self.SHA, "Add /api/version", "Closes #5"))

    def test_a_new_commit_clears_it(self):
        stored = self.stamp("t", "b")
        self.assertFalse(rs.already_reviewed(stored, "deadbeef", "t", "b"))

    def test_invisible_whitespace_does_NOT_clear_it(self):
        # The web editor and the API disagree about trailing newlines and
        # CRLF. Re-reviewing on that would loop forever.
        stored = self.stamp("t", "line one\nline two")
        self.assertTrue(rs.already_reviewed(
            stored, self.SHA, "t ", "line one\r\nline two\n"))

    def test_a_legacy_bare_sha_earns_exactly_one_re_review(self):
        # State written before the digest existed. Fails towards reviewing.
        self.assertFalse(rs.already_reviewed(self.SHA, self.SHA, "t", "b"))
        self.assertTrue(rs.already_reviewed(
            self.stamp("t", "b"), self.SHA, "t", "b"))

    def test_nothing_recorded_means_review_it(self):
        self.assertFalse(rs.already_reviewed(None, self.SHA, "t", "b"))
        self.assertFalse(rs.already_reviewed("", self.SHA, "t", "b"))

    def test_the_planner_uses_this_module_not_a_copy(self):
        # Two implementations of the digest would mean every pull request
        # looked changed forever. (Same file, not the same object: the
        # harness re-imports modules fresh for each test module.)
        self.assertEqual(rt.review_subject.__file__, rs.__file__)
        self.assertEqual(rt.review_subject.fingerprint("t", "b"),
                         rs.fingerprint("t", "b"))


class Discovery(unittest.TestCase):
    """The search query, and the repository it names."""

    def setUp(self):
        self._real = rt.gh_get
        self.calls = []

    def tearDown(self):
        rt.gh_get = self._real

    def test_it_searches_by_authorship_first(self):
        # Authorship is the PRIMARY signal, and it has to be, because GitHub
        # refuses to let a pull request's author be added as its reviewer
        # (422). The bot authors everything it opens, so a `review-requested:`
        # search can never return its own work — keying discovery on that
        # alone leaves the solver waiting for a verdict that cannot arrive.
        def fake(url, params=None):
            self.calls.append((url, params or {}))
            return {"items": [{"number": 1,
                               "repository_url": "https://api.github.com/repos/o/r"}]}
        rt.gh_get = fake
        items = rt.list_reviewable_prs("cameron-claw")
        url, params = self.calls[0]
        self.assertTrue(url.endswith("/search/issues"), url)
        self.assertEqual(params["q"], "is:pr is:open author:cameron-claw")
        self.assertEqual(len(items), 1)

    def test_it_also_searches_what_it_was_asked_to_review(self):
        # Kept because it is the only way to catch a pull request a HUMAN
        # asked the bot to look at, which authorship would miss.
        def fake(url, params=None):
            self.calls.append((url, params or {}))
            return {"items": []}
        rt.gh_get = fake
        rt.list_reviewable_prs("cameron-claw")
        queries = [p["q"] for _, p in self.calls]
        self.assertIn("is:pr is:open review-requested:cameron-claw", queries)

    def test_a_pull_request_found_twice_is_reviewed_once(self):
        def fake(url, params=None):
            self.calls.append((url, params or {}))
            return {"items": [{"number": 7,
                               "repository_url": "https://api.github.com/repos/o/r"}]}
        rt.gh_get = fake
        items = rt.list_reviewable_prs("cameron-claw")
        self.assertEqual(len(items), 1, "authored AND requested must not double up")

    def test_no_login_means_no_search_at_all(self):
        def fake(url, params=None):
            self.calls.append((url, params or {}))
            return {"items": []}
        rt.gh_get = fake
        self.assertEqual(rt.list_reviewable_prs(""), [])
        self.assertEqual(self.calls, [])

    def test_the_repo_comes_from_repository_url(self):
        # Search items name the repository only by its API URL.
        self.assertEqual(
            rt.repo_of({"repository_url": "https://api.github.com/repos/o/r"}),
            "o/r")

    def test_a_malformed_item_names_no_repo(self):
        self.assertEqual(rt.repo_of({}), "")
        self.assertEqual(rt.repo_of({"repository_url": "nonsense"}), "")


class Permissions(unittest.TestCase):
    """`project-allow check`: exit 2 is an answer, anything else is not."""

    def test_zero_is_permitted(self):
        self.assertEqual(rt.permission_reason(0), "")

    def test_two_is_not_permitted(self):
        self.assertEqual(rt.permission_reason(2), "not-permitted")

    def test_anything_else_fails_closed(self):
        # The CLI missing, the list unreadable, the exec failing. We cannot
        # tell what is permitted, which permits nothing.
        for code in (1, 3, 127, None):
            with self.subTest(code=code):
                self.assertEqual(rt.permission_reason(code),
                                 "allowlist-unavailable")


if __name__ == "__main__":
    unittest.main()
