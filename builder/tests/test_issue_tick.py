"""What the issue planner is allowed to hand to a solver, and in what order.

WHY THESE AND NOT THE HTTP
--------------------------
Every gate in heartbeat-issue-tick decides whether a repository checkout, a
concurrency slot and a model turn get spent. Each of them fails in the same
shape when it is wrong: nothing crashes, the plan looks plausible, and the bot
works on something nobody asked it to — or stops working on everything and
looks merely idle.

So the tests here drive `main()` with the network replaced and assert on the
PLAN, which is the artefact the spawner acts on. Four gates, in the order the
planner applies them:

    allowlist   permission, before any per-issue call is even paid for
    status      Done / Won't do / Duplicate are finished or somebody's call
    On Hold     a question is pending; only a human takes the label off
    lexical     destructive wording is asked about before it is worked

and then the ordering rules, which decide WHICH issue a repo's single slot
goes to.

No network and no pod: `gh_get`, `gh_post` and the kubectl exec are replaced.
The exec stub answers by inspecting the script it was handed, so the allowlist
read and the queue publish are exercised for real rather than stubbed away.
"""

import contextlib
import io
import json
import unittest

from harness import load_script

tick = load_script("heartbeat-issue-tick.py")

ALLOWED = "o/r"


def issue(number, *, title="a task", labels=(), state="open",
          state_reason=None, body="", repo=ALLOWED):
    return {
        "number": number,
        "title": title,
        "body": body,
        "state": state,
        "state_reason": state_reason,
        "html_url": f"https://github.com/{repo}/issues/{number}",
        "repository_url": f"https://api.github.com/repos/{repo}",
        "labels": [{"name": n} for n in labels],
    }


class PlannerTestCase(unittest.TestCase):
    """A planner tick with the pod and GitHub replaced."""

    maxDiff = None

    def setUp(self):
        self.issues = []
        self.allowlist_text = ALLOWED + "\n"
        self.allowlist_readable = True
        self.locked = set()
        self.comments = {}          # issue number -> list of comment dicts
        self.posted = []            # (url, payload)
        self.published = []         # every queue-state snippet handed to exec

        self._saved = {
            k: getattr(tick, k) for k in
            ("gh_get", "gh_post", "kubectl_exec_capture",
             "k8s_find_openclaw_pod", "_read", "GITHUB_TOKEN",
             "MAX_PER_REPO", "_BOT_LOGIN_CACHE")
        }
        tick.gh_get = self._gh_get
        tick.gh_post = self._gh_post
        tick.kubectl_exec_capture = self._exec
        tick.k8s_find_openclaw_pod = lambda ns: "openclaw-0"
        tick._read = lambda path: "claw-code-local"
        tick.GITHUB_TOKEN = "token"
        tick._BOT_LOGIN_CACHE = "bot"

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(tick, k, v)

    # -- the fakes ------------------------------------------------------

    def _gh_get(self, url, params=None):
        if url.endswith("/issues") and "repos/" not in url:
            return list(self.issues)
        if url.endswith("/comments"):
            number = int(url.rsplit("/issues/", 1)[1].split("/")[0])
            return list(self.comments.get(number, []))
        if url.endswith("/user"):
            return {"login": "bot"}
        raise AssertionError(f"unexpected GET {url}")

    def _gh_post(self, url, payload):
        self.posted.append((url, payload))
        return True

    def _exec(self, namespace, pod, *cmd, timeout=15):
        """Answer the way the pod would, keyed on what was asked."""
        script = cmd[-1]
        if tick.project_allowlist.ALLOWED_MARKER in script:
            if not self.allowlist_readable:
                return (1, "", "exec failed")
            return (0, tick.project_allowlist.ALLOWED_MARKER + "\n"
                    + self.allowlist_text, "")
        if ".fixer-locks" in script:
            return (0, "\n".join(sorted(self.locked)), "")
        if "queue-state" in script:
            self.published.append(script)
            return (0, "", "")
        raise AssertionError(f"unexpected exec: {script[:80]}")

    # -- driving it -----------------------------------------------------

    def plan(self, max_per_repo=1):
        tick.MAX_PER_REPO = max_per_repo
        out = io.StringIO()
        with contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(io.StringIO()):
            rc = tick.main()
        self.assertEqual(rc, 0, out.getvalue())
        return json.loads(out.getvalue())

    def repo_entry(self, plan, repo=ALLOWED):
        for r in plan["repos"]:
            if r["repo"] == repo:
                return r
        raise AssertionError(f"{repo} missing from {plan['repos']}")

    def spawned(self, plan, repo=ALLOWED):
        return [e["issueNumber"] for e in self.repo_entry(plan, repo)["toSpawn"]]


class AllowlistGate(PlannerTestCase):
    """Being assigned an issue is a request. This list is the answer."""

    def test_a_permitted_repo_is_planned(self):
        self.issues = [issue(1)]
        plan = self.plan()
        self.assertEqual(self.spawned(plan), [1])
        self.assertNotIn("reason", self.repo_entry(plan))

    def test_a_repo_not_on_the_list_spawns_nothing(self):
        self.issues = [issue(1, repo="stranger/repo")]
        plan = self.plan()
        entry = self.repo_entry(plan, "stranger/repo")
        self.assertEqual(entry["toSpawn"], [])
        self.assertEqual(entry["reason"], "not-permitted")

    def test_an_empty_list_is_reported_as_empty_not_as_a_refusal(self):
        # "The owner has permitted nothing" and "this repo was not chosen" are
        # different operational situations and the spawner reports them apart.
        self.allowlist_text = "# nothing granted yet\n"
        self.issues = [issue(1)]
        plan = self.plan()
        self.assertEqual(self.repo_entry(plan)["reason"], "allowlist-empty")
        self.assertEqual(plan["allowedProjects"], 0)

    def test_an_unreadable_list_permits_nothing_and_says_so(self):
        # Fail CLOSED, and be loud about which failure it was: falling back to
        # "everything" would turn a read error into exactly the unrestricted
        # behaviour the list exists to prevent.
        self.allowlist_readable = False
        self.issues = [issue(1)]
        plan = self.plan()
        self.assertIs(plan["allowlistAvailable"], False)
        self.assertIsNone(plan["allowedProjects"])
        self.assertEqual(self.repo_entry(plan)["reason"], "allowlist-unavailable")
        self.assertEqual(self.repo_entry(plan)["toSpawn"], [])

    def test_a_refused_repo_is_never_asked_about(self):
        # The gate is first for a reason: a repo the owner did not permit must
        # not cost a single per-issue call, and must never be written to.
        self.issues = [issue(1, repo="stranger/repo",
                             body="remove the tests, they slow us down")]
        self.plan()
        self.assertEqual(self.posted, [])


class StatusFilter(PlannerTestCase):
    """Only `To do` and `In progress` are a planner's to pick up."""

    def test_to_do_is_workable(self):
        self.issues = [issue(1)]
        self.assertEqual(self.spawned(self.plan()), [1])

    def test_in_progress_is_workable(self):
        self.issues = [issue(1, labels=["status::in-progress"])]
        self.assertEqual(self.spawned(self.plan()), [1])

    def test_wont_do_is_not_planned_even_while_the_issue_is_open(self):
        # A human's terminal call. The issue is still open — GitHub has no
        # other way to say it — and re-planning it is how a bot re-opens work
        # somebody deliberately ended.
        self.issues = [issue(1, labels=["status::wont-do"])]
        plan = self.plan()
        self.assertEqual(self.spawned(plan), [])
        self.assertEqual(self.repo_entry(plan)["notWorkable"], 1)

    def test_duplicate_is_not_planned(self):
        self.issues = [issue(1, labels=["status::duplicate"])]
        self.assertEqual(self.spawned(self.plan()), [])

    def test_a_closed_issue_is_not_planned(self):
        # The lister asks for open issues only, but the status gate must not
        # depend on that: a state that changed between the fetch and the plan
        # would otherwise spawn a solver on a delivered story.
        self.issues = [issue(1, state="closed", state_reason="completed")]
        self.assertEqual(self.spawned(self.plan()), [])

    def test_an_unknown_status_label_does_not_wedge_the_rest_of_the_tick(self):
        # A `status::` label naming nothing we recognise resolves to To do
        # rather than raising. A planner that raises on one mislabelled issue
        # stops planning every OTHER issue in the same tick.
        self.issues = [issue(1, labels=["status::whatever"]), issue(2)]
        self.assertEqual(self.spawned(self.plan(max_per_repo=5)), [1, 2])


class OnHoldParking(PlannerTestCase):
    """A question is pending. Only a human hands the issue back."""

    def test_an_on_hold_issue_is_not_spawned(self):
        self.issues = [issue(1, labels=["On Hold"])]
        plan = self.plan()
        self.assertEqual(self.spawned(plan), [])
        self.assertEqual(self.repo_entry(plan)["onHold"], 1)

    def test_the_label_is_matched_case_insensitively_and_through_a_scope(self):
        # It is applied by hand, and nobody types a label the same way twice.
        for spelling in ("on hold", "ON HOLD", "On-Hold", "Status::On Hold",
                         "on_hold"):
            with self.subTest(spelling=spelling):
                self.issues = [issue(1, labels=[spelling])]
                self.assertEqual(self.spawned(self.plan()), [],
                                 f"{spelling!r} did not park the issue")

    def test_removing_the_label_hands_the_issue_back(self):
        # The release is the whole design: On Hold must not be a one-way trap.
        self.issues = [issue(1, labels=["On Hold"])]
        self.assertEqual(self.spawned(self.plan()), [])
        self.issues = [issue(1)]
        self.assertEqual(self.spawned(self.plan()), [1])

    def test_a_similar_label_does_not_park_anything(self):
        # "onboarding" contains neither word; the fold must not be so eager
        # that an ordinary label silently stops work.
        self.issues = [issue(1, labels=["onboarding"]), issue(2, labels=["hold"])]
        self.assertEqual(self.spawned(self.plan(max_per_repo=5)), [1, 2])

    def test_a_parked_issue_is_not_counted_as_pending_work(self):
        # queue_state feeds the tester's "solve and merge first" rule. An issue
        # waiting on a human is not pending work, and counting it as such would
        # disable deployment testing indefinitely.
        self.issues = [issue(1, labels=["On Hold"])]
        plan = self.plan()
        self.assertEqual(plan["pendingIssues"], 0)


class PriorityOrder(PlannerTestCase):
    """Priority orders what is workable. It never overrides what is in flight."""

    def test_the_more_urgent_issue_takes_the_repos_single_slot(self):
        self.issues = [issue(1, labels=["Priority::Low"]),
                       issue(2, labels=["Priority::Very High"])]
        self.assertEqual(self.spawned(self.plan()), [2])

    def test_equal_priority_is_oldest_first(self):
        self.issues = [issue(9), issue(4), issue(7)]
        self.assertEqual(self.spawned(self.plan(max_per_repo=3)), [4, 7, 9])

    def test_an_unlabelled_issue_sits_between_high_and_low(self):
        self.issues = [issue(1, labels=["Priority::High"]),
                       issue(2),
                       issue(3, labels=["Priority::Very Low"])]
        self.assertEqual(self.spawned(self.plan(max_per_repo=3)), [1, 2, 3])

    def test_work_already_in_progress_outranks_a_more_urgent_fresh_issue(self):
        # The in-flight rule exists so the bot converges on one issue instead
        # of leaving a trail of half-finished branches. A label must not undo
        # it — that is the whole distinction issue_priority documents.
        self.issues = [issue(1, labels=["status::in-progress", "Priority::Low"]),
                       issue(2, labels=["Priority::Very High"])]
        self.assertEqual(self.spawned(self.plan()), [1])

    def test_the_plan_names_the_priority_it_sorted_on(self):
        self.issues = [issue(1, labels=["priority::high"])]
        entry = self.repo_entry(self.plan())["toSpawn"][0]
        self.assertEqual(entry["priority"], "High")


class EstimationRouting(PlannerTestCase):
    """Size first, implement next tick — the model depends on the size."""

    def test_an_unsized_issue_asks_to_be_estimated(self):
        self.issues = [issue(1)]
        entry = self.repo_entry(self.plan())["toSpawn"][0]
        self.assertIs(entry["needsEstimate"], True)
        self.assertIs(entry["pointsDefaulted"], True)
        self.assertEqual(entry["storyPoints"], 8)

    def test_a_sized_issue_carries_its_points_to_the_solver(self):
        self.issues = [issue(1, labels=["SP::3"])]
        entry = self.repo_entry(self.plan())["toSpawn"][0]
        self.assertIs(entry["needsEstimate"], False)
        self.assertIs(entry["pointsDefaulted"], False)
        self.assertEqual(entry["storyPoints"], 3)

    def test_an_estimate_request_re_sizes_an_already_sized_issue(self):
        # The label is a REQUEST, not a result: it means "size this again".
        self.issues = [issue(1, labels=["SP::3", "estimate"])]
        entry = self.repo_entry(self.plan())["toSpawn"][0]
        self.assertIs(entry["needsEstimate"], True)

    def test_a_hand_typed_size_scope_is_read_rather_than_re_estimated(self):
        self.issues = [issue(1, labels=["story points::5"])]
        entry = self.repo_entry(self.plan())["toSpawn"][0]
        self.assertIs(entry["needsEstimate"], False)
        self.assertEqual(entry["storyPoints"], 5)


class QueuePublication(PlannerTestCase):
    """The tester holds off while there is anything left to solve."""

    def test_the_post_gate_count_is_published(self):
        self.issues = [issue(1), issue(2), issue(3, labels=["status::wont-do"])]
        plan = self.plan()
        self.assertEqual(plan["pendingIssues"], 2)
        self.assertEqual(len(self.published), 1)
        self.assertIn("queue-state/solver", self.published[0])
        self.assertIn("2 ", self.published[0])

    def test_a_repo_that_is_not_permitted_contributes_nothing(self):
        self.issues = [issue(1, repo="stranger/repo"), issue(2)]
        self.assertEqual(self.plan()["pendingIssues"], 1)

    def test_a_failed_publish_does_not_fail_the_tick(self):
        # A stale marker reads as unknown to the tester, which then runs. That
        # is the safe direction; losing the whole plan is not.
        def boom(*a, **kw):
            if "queue-state" in a[-1]:
                raise RuntimeError("exec died")
            return self._exec(*a, **kw)
        tick.kubectl_exec_capture = boom
        self.issues = [issue(1)]
        self.assertEqual(self.spawned(self.plan()), [1])


class AskBeforeSpawning(PlannerTestCase):
    """Destructive wording is put to a human before a solver is started."""

    DESTRUCTIVE = "Please remove the test suite, it slows down the build."

    def test_a_destructive_issue_is_questioned_instead_of_spawned(self):
        self.issues = [issue(1, body=self.DESTRUCTIVE)]
        plan = self.plan()
        self.assertEqual(self.spawned(plan), [])
        self.assertEqual(self.repo_entry(plan)["awaitingConfirmation"], 1)

    def test_the_question_itself_is_posted(self):
        self.issues = [issue(1, body=self.DESTRUCTIVE)]
        self.plan()
        bodies = [p["body"] for url, p in self.posted if url.endswith("/comments")]
        self.assertEqual(len(bodies), 1, self.posted)
        self.assertIn(tick.lexical_guard.ASK_MARKER, bodies[0])

    def test_the_issue_is_parked_so_the_question_is_not_re_asked_every_tick(self):
        self.issues = [issue(1, body=self.DESTRUCTIVE)]
        self.plan()
        labels = [p for url, p in self.posted if url.endswith("/labels")]
        self.assertEqual(labels, [{"labels": ["On Hold"]}])

    def test_a_question_already_on_the_record_is_not_asked_twice(self):
        self.issues = [issue(1, body=self.DESTRUCTIVE, labels=["On Hold"])]
        self.comments[1] = [{
            "id": 5, "user": {"login": "bot"},
            "body": f"🛑 {tick.lexical_guard.ASK_MARKER}\n\nwell?",
        }]
        # Parked, so it never even reaches the guard — and nothing is posted.
        plan = self.plan()
        self.assertEqual(self.spawned(plan), [])
        self.assertEqual(self.posted, [])

    def test_an_unanswered_question_keeps_the_issue_out_of_the_queue(self):
        # The label was taken off but the answer never came: the guard still
        # sees its own note and declines to spawn, rather than asking again.
        self.issues = [issue(1, body=self.DESTRUCTIVE)]
        self.comments[1] = [{
            "id": 5, "user": {"login": "bot"},
            "body": f"🛑 {tick.lexical_guard.ASK_MARKER}\n\nwell?",
        }]
        plan = self.plan()
        self.assertEqual(self.spawned(plan), [])
        self.assertEqual(self.posted, [])

    def test_ordinary_wording_is_spawned_untouched(self):
        self.issues = [issue(1, body="Add a dark mode toggle to the settings page.")]
        self.assertEqual(self.spawned(self.plan()), [1])
        self.assertEqual(self.posted, [])

    def test_a_prohibition_is_not_read_as_a_request(self):
        # "must not delete the tests" is the issue telling you NOT to.
        self.issues = [issue(1, body="The change must not delete any tests.")]
        self.assertEqual(self.spawned(self.plan()), [1])
        self.assertEqual(self.posted, [])

    def test_an_unpostable_question_still_stops_the_spawn(self):
        # An issue that should have been questioned and could not be is an
        # issue to leave alone — the safe direction is the one that does no
        # work.
        tick.gh_post = lambda url, payload: False
        self.issues = [issue(1, body=self.DESTRUCTIVE)]
        self.assertEqual(self.spawned(self.plan()), [])


if __name__ == "__main__":
    unittest.main()
