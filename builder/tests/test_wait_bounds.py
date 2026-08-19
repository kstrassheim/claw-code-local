"""The bounds on waiting: nothing the bot waits for may be waited for forever.

WHY THIS FILE EXISTS
--------------------
The solver deadlocked in production. It had asked its own reviewer for a
verdict, recorded that it was waiting, and then waited — through every tick,
for hours, spending zero model calls each time. The planner, seeing a solver
mid-review, kept handing the repo's single slot back to the same issue. Both
halves behaved exactly as designed and nothing moved.

Every wait in the solver has that failure available to it, because every one
of them is a claim about somebody else's future behaviour:

  - "the reviewer will post a verdict"      — it may have crashed
  - "the agent will address these findings" — the run may have died at a 429
  - "the next push will fix the checks"     — it may not have

So each has a bound, and the bounds are what these tests are about:

  REVIEW_WAIT_TTL     an awaiting-review marker STOPS BEING BELIEVED once it
                      goes stale — the planner ignores it and the solver asks
                      again;
  REVIEW_RETRY_CAP    an unaddressed verdict wakes the agent a bounded number
                      of times, then asks a person;
  CI_RED_RETRY_CAP    an unchanged red CI does the same.

Two properties matter as much as the bounds themselves and are tested for
each: the budget RESETS when the situation actually changes (a new head, a new
verdict, a human saying something), or a bot that was working would be cut off
mid-job; and the escalation is ONE comment per condition, or a pull request
becomes unreadable at a comment every five minutes.

The shell blocks are EXTRACTED from fixer-runner.sh rather than copied, so a
restructure fails loudly instead of leaving tests that pass against code
nobody ships. The planner's marker read is exercised by running the snippet it
sends to the pod through a REAL bash against real files with real mtimes — the
TTL lives inside that snippet, and a stubbed answer would test nothing at all.
"""

import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest

import harness
from test_issue_tick import ALLOWED, PlannerTestCase, issue, tick
from test_solver_gates import RunnerBlock

RUNNER = "fixer-runner.sh"

HEAD = "abc1234abc1234abc1234abc1234abc1234abcd"
NEXT = "fedcba9876543210fedcba9876543210fedcba98"


def _q(s):
    return "'" + str(s).replace("'", "'\\''") + "'"


class WaitBlock(RunnerBlock):
    """A runner block, plus the fixtures every wait-bound test needs."""

    def setUp(self):
        super().setUp()
        self.head(HEAD)
        self.fixture("repos_o_r_issues_7_comments", [])
        self.fixture("POST_repos_o_r_issues_7_comments", {"id": 1})
        self.kubectl("echo false")          # the reviewer CronJob is active

    def head(self, sha, state="clean"):
        self.fixture("repos_o_r_pulls_7", {"head": {"sha": sha},
                                           "mergeable_state": state,
                                           "draft": False})

    def verdict(self, kind=None, sha=HEAD, login="bot"):
        """Put (or clear) the reviewer's verdict comment on the issue."""
        if kind is None:
            self.fixture("repos_o_r_issues_7_comments", [])
            return
        word = "APPROVED" if kind == "approved" else "CHANGES REQUIRED"
        self.fixture("repos_o_r_issues_7_comments",
                     [{"user": {"login": login},
                       "body": f"🔎 REVIEW RESULT: {word} (sha {sha})"}])

    def comments_posted(self):
        return [p for p in self.by_method("POST") if "issues/7/comments" in p]

    def extra_block(self, name, start, end):
        """Extract a block this suite pins that the gate tests do not."""
        src = self.extract_block(RUNNER, start, end)
        dst = os.path.join(self.home, f"{name}.sh")
        shutil.move(src, dst)
        return f"{name}.sh"


# ---------------------------------------------------------------------------


class ReviewWaitTtl(WaitBlock):
    """A wait on somebody else's verdict has to have an end."""

    def gate(self, **env):
        return self.sh(self.preamble()
                       + self.sources("api", "status", "facts", "review"),
                       **env)

    def request(self, ttl=None):
        """One tick of the review gate against a head with no verdict yet."""
        script = self.preamble()
        if ttl is not None:
            script += f"REVIEW_WAIT_TTL={ttl}\n"
        script += (self.sources("api", "status", "facts", "review")
                   + "if review_gate 7; then echo MAY_MERGE; else echo HELD; fi\n")
        return self.sh(script)

    def age_the_marker(self, seconds):
        path = os.path.join(self.home, "awaiting-review")
        old = time.time() - seconds
        os.utime(path, (old, old))

    def test_a_fresh_wait_is_believed_and_says_nothing_twice(self):
        # The baseline the TTL must not break: one comment per head sha, not
        # one per tick.
        self.request()
        self.assertEqual(len(self.comments_posted()), 1)
        rc, out, err = self.request()
        self.assertIn("already requested", out, out + err)
        self.assertEqual(len(self.comments_posted()), 1)

    def test_a_wait_older_than_the_ttl_is_asked_again(self):
        # THE DEADLOCK. A reviewer that crashed, was suspended mid-run, or
        # simply never posted its verdict leaves a marker that would otherwise
        # be believed forever — and the solver would wait on it forever,
        # spending nothing, every five minutes. Past the TTL the wait is
        # treated as if it had never happened.
        self.request()
        self.age_the_marker(7201)
        rc, out, err = self.request()
        self.assertIn("pending for over", out, out + err)
        self.assertEqual(len(self.comments_posted()), 2)

    def test_the_ttl_is_overridable(self):
        self.request(ttl=60)
        self.age_the_marker(90)
        rc, out, _ = self.request(ttl=60)
        self.assertIn("pending for over", out)

    def test_a_wait_younger_than_the_ttl_is_still_a_wait(self):
        self.request(ttl=600)
        self.age_the_marker(90)
        rc, out, _ = self.request(ttl=600)
        self.assertIn("already requested", out)
        self.assertEqual(len(self.comments_posted()), 1)

    def test_the_re_request_records_the_same_head_again(self):
        # Re-asking must leave the marker pointing at the head it is waiting
        # on, or the next tick has nothing to compare against and asks again
        # immediately — trading one deadlock for a comment every five minutes.
        self.request()
        self.age_the_marker(7201)
        self.request()
        self.assertEqual(self.state("awaiting-review"), HEAD)


# ---------------------------------------------------------------------------


class ReviewRetry(WaitBlock):
    """An unaddressed verdict is work, not a wait — but not endless work."""

    def check(self, cap=4):
        return self.sh(self.preamble()
                       + f"REVIEW_RETRY_CAP={cap}\n"
                       + self.sources("api", "status", "facts", "review",
                                      "escalate", "review_retry")
                       + "if review_needs_agent 7; then echo WAKE; else echo SLEEP; fi\n")

    def test_no_verdict_wakes_nobody(self):
        rc, out, err = self.check()
        self.assertIn("SLEEP", out, out + err)
        self.assertEqual(self.state("review-fp"), "none:abc1234")

    def test_an_approval_wakes_nobody(self):
        self.verdict("approved")
        rc, out, err = self.check()
        self.assertIn("SLEEP", out, out + err)

    def test_a_changes_verdict_for_the_current_head_wakes_the_agent(self):
        self.verdict("changes")
        rc, out, err = self.check()
        self.assertIn("WAKE", out, out + err)
        self.assertIn("attempt 1/4", out)
        self.assertEqual(self.state("review-retries"), "1")

    def test_the_arriving_verdict_ends_the_wait_for_it(self):
        with open(os.path.join(self.home, "awaiting-review"), "w",
                  encoding="utf-8", newline="\n") as f:
            f.write(HEAD)
        self.verdict("changes")
        self.check()
        self.assertIsNone(self.state("awaiting-review"))

    def test_the_same_verdict_wakes_the_agent_again(self):
        # THE BUG THIS EXISTS FOR, and the twin of the rebase-conflict one.
        # The fingerprint is written when the verdict is OBSERVED, before the
        # agent has done anything with it — so a run woken and then killed
        # spent the trigger for good. Every later tick found the fingerprint
        # unchanged, declined to wake, and both sides waited forever.
        self.verdict("changes")
        self.check()
        rc, out, err = self.check()
        self.assertIn("WAKE", out, out + err)
        self.assertIn("attempt 2/4", out)

    def test_the_retries_are_bounded_and_then_a_person_is_asked(self):
        self.verdict("changes")
        for _ in range(4):
            self.assertIn("WAKE", self.check()[1])
        rc, out, err = self.check()
        self.assertIn("SLEEP", out, out + err)
        self.assertIn("a human should look", out)
        self.assertEqual(len(self.comments_posted()), 1, self.requests())
        self.assertIn("@o", json.dumps(self.comments_posted()) + out)

    def test_the_escalation_is_one_comment_per_condition_not_per_tick(self):
        self.verdict("changes")
        for _ in range(10):
            self.check()
        self.assertEqual(len(self.comments_posted()), 1, self.requests())

    def test_the_escalation_parks_the_issue_on_a_human(self):
        self.verdict("changes")
        for _ in range(6):
            self.check()
        self.assertIsNotNone(self.state("awaiting-human"))

    def test_a_new_head_stops_the_retries_and_resets_the_budget(self):
        # A push is how findings get addressed. The verdict now describes a
        # commit that is no longer open, so it is neither unaddressed nor a
        # reason to wake anybody — and the budget it spent is returned.
        self.verdict("changes")
        for _ in range(5):
            self.check()
        self.head(NEXT)
        rc, out, err = self.check()
        self.assertIn("SLEEP", out, out + err)
        self.assertIsNone(self.state("review-retries"))

    def test_a_new_verdict_gets_a_fresh_budget(self):
        self.verdict("changes")
        for _ in range(5):
            self.check()
        self.assertIn("SLEEP", self.check()[1])
        self.head(NEXT)
        self.verdict("changes", sha=NEXT)
        rc, out, err = self.check()
        self.assertIn("WAKE", out, out + err)
        self.assertIn("attempt 1/4", out)
        self.assertEqual(self.state("review-retries"), "1")

    def test_a_second_dead_end_may_be_reported_again(self):
        # One comment per CONDITION, not one per issue: a fresh verdict that
        # also runs out of budget is a new thing to tell somebody about.
        self.verdict("changes")
        for _ in range(6):
            self.check()
        self.head(NEXT)
        self.verdict("changes", sha=NEXT)
        for _ in range(6):
            self.check()
        self.assertEqual(len(self.comments_posted()), 2, self.requests())

    def test_a_verdict_about_an_older_commit_is_not_unaddressed_work(self):
        self.verdict("changes", sha="9999999999999999")
        rc, out, err = self.check()
        self.assertIn("SLEEP", out, out + err)

    def test_a_corrupt_counter_does_not_wedge_the_wake(self):
        self.verdict("changes")
        self.check()
        with open(os.path.join(self.home, "review-retries"), "w",
                  encoding="utf-8", newline="\n") as f:
            f.write("not a number")
        rc, out, _ = self.check()
        self.assertIn("WAKE", out)

    def test_a_suspended_reviewer_clears_the_state_and_wakes_nobody(self):
        self.verdict("changes")
        self.check()
        self.kubectl("echo true")
        rc, out, err = self.check()
        self.assertIn("SLEEP", out, out + err)
        self.assertIsNone(self.state("review-fp"))
        self.assertIsNone(self.state("review-retries"))

    def test_an_unresolvable_head_wakes_nobody(self):
        os.remove(os.path.join(self.fixtures, "repos_o_r_pulls_7"))
        rc, out, _ = self.check()
        self.assertIn("SLEEP", out)


# ---------------------------------------------------------------------------


class CiRedRetry(WaitBlock):
    """A red pipeline is unresolved work — for a bounded number of turns."""

    FP = "settled:abc1234:0f0f0f0f0f0f0f0f"
    FP2 = "settled:fedcba9:1a1a1a1a1a1a1a1a"

    def setUp(self):
        super().setUp()
        self.red()

    def red(self):
        self.fixture("repos_o_r_commits", {"check_runs": [
            {"name": "build", "status": "completed", "conclusion": "failure"}]})

    def green(self):
        self.fixture("repos_o_r_commits", {"check_runs": [
            {"name": "build", "status": "completed", "conclusion": "success"}]})

    def check(self, fp=None, changed=0, cap=4):
        fp = self.FP if fp is None else fp
        return self.sh(self.preamble()
                       + f"CI_RED_RETRY_CAP={cap}\n"
                       + self.sources("api", "status", "escalate", "ci_red_retry")
                       + f"if ci_red_needs_agent 7 {_q(fp)} {changed}; "
                         "then echo WAKE; else echo SLEEP; fi\n")

    def test_a_pipeline_still_running_is_not_a_red_one(self):
        rc, out, err = self.check(fp="in-progress:abc1234")
        self.assertIn("SLEEP", out, out + err)
        self.assertIsNone(self.state("ci-red-retries"))

    def test_green_checks_wake_nobody_and_clear_the_budget(self):
        self.check()
        self.green()
        rc, out, err = self.check()
        self.assertIn("SLEEP", out, out + err)
        self.assertIsNone(self.state("ci-red-retries"))

    def test_a_newly_red_pipeline_spends_the_first_attempt(self):
        # The ci-change wake the preflight is about to do IS attempt one, so
        # the cap counts total agent attempts on one red commit, not cap+1.
        rc, out, err = self.check(changed=1)
        self.assertIn("SLEEP", out, out + err)
        self.assertEqual(self.state("ci-red-retries"), "1")

    def test_an_unchanged_red_pipeline_wakes_the_agent_again(self):
        self.check(changed=1)
        rc, out, err = self.check()
        self.assertIn("WAKE", out, out + err)
        self.assertIn("attempt 2/4", out)

    def test_the_retries_are_bounded_and_then_a_person_is_asked(self):
        self.check(changed=1)
        for _ in range(3):
            self.assertIn("WAKE", self.check()[1])
        rc, out, err = self.check()
        self.assertIn("SLEEP", out, out + err)
        self.assertIn("a human should look", out)
        self.assertEqual(len(self.comments_posted()), 1, self.requests())

    def test_the_escalation_is_one_comment_per_condition_not_per_tick(self):
        for _ in range(12):
            self.check()
        self.assertEqual(len(self.comments_posted()), 1, self.requests())

    def test_the_escalation_parks_the_issue_on_a_human(self):
        for _ in range(6):
            self.check()
        self.assertIsNotNone(self.state("awaiting-human"))

    def test_a_new_fingerprint_gets_a_fresh_budget(self):
        for _ in range(6):
            self.check()
        self.assertIn("SLEEP", self.check()[1])
        rc, out, err = self.check(fp=self.FP2, changed=1)
        self.assertEqual(self.state("ci-red-retries"), "1")
        rc, out, err = self.check(fp=self.FP2)
        self.assertIn("WAKE", out, out + err)
        self.assertIn("attempt 2/4", out)

    def test_a_corrupt_counter_does_not_wedge_the_wake(self):
        self.check(changed=1)
        with open(os.path.join(self.home, "ci-red-retries"), "w",
                  encoding="utf-8", newline="\n") as f:
            f.write("")
        rc, out, _ = self.check()
        self.assertIn("WAKE", out)


# ---------------------------------------------------------------------------


class HumanInputResets(WaitBlock):
    """Somebody said something. That is a new start, not a continuation."""

    START = "  # A person said something new."
    END = "  # A CI that is red and has NOT changed"

    FILES = ("awaiting-human", "ci-red-retries", "ci-red-escalated",
             "review-retries", "review-escalated")

    def run_reset(self, count):
        for name in self.FILES:
            with open(os.path.join(self.home, name), "w",
                      encoding="utf-8", newline="\n") as f:
                f.write("x")
        block = self.extra_block("human_reset", self.START, self.END)
        rc, out, err = self.sh(self.preamble()
                               + f"PREFLIGHT_NEW_COUNT={count}\n"
                               + f'source "$PWD/{block}"\n')
        self.assertEqual(rc, 0, out + err)

    def test_a_new_comment_clears_every_budget_and_the_human_park(self):
        self.run_reset(2)
        for name in self.FILES:
            self.assertIsNone(self.state(name), name)

    def test_silence_changes_nothing(self):
        # The budgets must not reset just because a tick happened, or the caps
        # never bind and the escalation never arrives.
        self.run_reset(0)
        for name in self.FILES:
            self.assertEqual(self.state(name), "x", name)


# ---------------------------------------------------------------------------


class WaitRanking(PlannerTestCase):
    """Which wait the planner finishes, and which one it walks away from.

    The marker snippet runs for REAL here — the same string the planner hands
    the pod, through bash, against files whose mtimes this test sets. The TTL
    is IN that snippet, so anything less would be testing the stub.
    """

    def setUp(self):
        super().setUp()
        os.makedirs(harness.TMP_ROOT, exist_ok=True)
        self.pod_home = tempfile.mkdtemp(prefix="podhome-", dir=harness.TMP_ROOT)
        self.markers = os.path.join(self.pod_home, ".openclaw", "issue-markers")
        os.makedirs(self.markers, exist_ok=True)
        self.markers_readable = True
        self._ttl = tick.REVIEW_WAIT_TTL

    def tearDown(self):
        tick.REVIEW_WAIT_TTL = self._ttl
        shutil.rmtree(self.pod_home, ignore_errors=True)
        super().tearDown()

    def _exec(self, namespace, pod, *cmd, timeout=15):
        script = cmd[-1]
        if "issue-markers" in script:
            if not self.markers_readable:
                return (1, "", "exec failed")
            env = dict(os.environ)
            env["HOME"] = self.pod_home
            p = subprocess.run(["bash", "-c", script], capture_output=True,
                               text=True, env=env, timeout=60)
            return (p.returncode, p.stdout, p.stderr)
        return super()._exec(namespace, pod, *cmd, timeout=timeout)

    def marker(self, kind, number, age=0, repo=ALLOWED):
        path = os.path.join(
            self.markers, f"{repo.replace('/', '__')}-{number}.awaiting-{kind}")
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write("sha")
        if age:
            when = time.time() - age
            os.utime(path, (when, when))

    def order(self):
        return self.spawned(self.plan(max_per_repo=9))

    # -- the bot's own review comes first -------------------------------

    def test_an_issue_awaiting_our_own_review_is_finished_first(self):
        # The work is still the bot's: it must see that issue through to the
        # merge before starting another one.
        self.issues = [issue(1, labels=["status::in-progress"]), issue(2)]
        self.marker("review", 2)
        self.assertEqual(self.order(), [2, 1])

    def test_it_outranks_even_a_more_urgent_fresh_issue(self):
        self.issues = [issue(1, labels=["priority::high"]), issue(2)]
        self.marker("review", 2)
        self.assertEqual(self.order(), [2, 1])

    def test_a_stale_marker_is_ignored(self):
        # THE SAFETY NET. Ranking an issue first because the solver is waiting
        # is only correct while somebody is going to answer. A reviewer that
        # never delivers would otherwise pin the repo's one slot on an issue
        # that spends zero model calls per tick, forever.
        self.issues = [issue(1, labels=["status::in-progress"]), issue(2)]
        self.marker("review", 2, age=7201)
        self.assertEqual(self.order(), [1, 2])

    def test_a_marker_just_inside_the_ttl_is_still_believed(self):
        self.issues = [issue(1, labels=["status::in-progress"]), issue(2)]
        self.marker("review", 2, age=60)
        self.assertEqual(self.order(), [2, 1])

    def test_the_ttl_is_overridable(self):
        tick.REVIEW_WAIT_TTL = 100
        self.issues = [issue(1, labels=["status::in-progress"]), issue(2)]
        self.marker("review", 2, age=300)
        self.assertEqual(self.order(), [1, 2])

    # -- a wait on a person comes last ----------------------------------

    def test_an_issue_waiting_on_a_human_is_worked_last(self):
        # Out of the bot's hands, possibly for days. The bot spends its slot
        # on something it can actually move.
        self.issues = [issue(1), issue(2)]
        self.marker("human", 1)
        self.assertEqual(self.order(), [2, 1])

    def test_it_ranks_last_even_when_it_is_the_most_urgent(self):
        self.issues = [issue(1, labels=["priority::high"]), issue(2)]
        self.marker("human", 1)
        self.assertEqual(self.order(), [2, 1])

    def test_the_two_waits_bracket_the_ordinary_work(self):
        self.issues = [issue(1), issue(2), issue(3),
                       issue(4, labels=["status::in-progress"])]
        self.marker("human", 1)
        self.marker("review", 3)
        self.assertEqual(self.order(), [3, 4, 2, 1])

    def test_a_human_wait_that_has_gone_stale_still_ranks_last(self):
        # Only the review wait expires. A person who has not answered in two
        # hours has not answered; re-ranking the issue would put the bot back
        # to holding the slot on a question it cannot answer itself.
        self.issues = [issue(1), issue(2)]
        self.marker("human", 1, age=99999)
        self.assertEqual(self.order(), [2, 1])

    # -- reading the markers at all -------------------------------------

    def test_a_marker_for_another_issue_is_not_applied_to_this_one(self):
        self.issues = [issue(1), issue(2)]
        self.marker("review", 5)
        self.marker("human", 6, repo="other/repo")
        self.assertEqual(self.order(), [1, 2])

    def test_an_unreadable_marker_directory_does_not_stop_the_tick(self):
        # The ordering is an optimisation. A tick that cannot read the markers
        # still has to plan — failing here would stop the bot entirely for the
        # sake of a sort.
        self.markers_readable = False
        self.issues = [issue(1), issue(2)]
        self.assertEqual(self.order(), [1, 2])

    def test_no_markers_at_all_is_the_ordinary_order(self):
        self.issues = [issue(2), issue(1)]
        self.assertEqual(self.order(), [1, 2])


if __name__ == "__main__":
    unittest.main()
