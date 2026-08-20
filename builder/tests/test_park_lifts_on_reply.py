"""A park on a human must lift when the human answers.

An issue awaiting a person ranks LAST so the bot works something it can
influence. The original design let the SOLVER clear the park when it next ran
and saw the reply. That cannot work, and the failure is circular:

    ranked last  ->  never spawned  ->  marker never cleared  ->  ranked last

With any backlog at all the park becomes permanent, and answering the question
does not release it. Observed on an issue that sat seven hours after the human
had replied, the reply unread on the issue the whole time.

So the PLANNER decides it from the API, without the solver running. Both
places a person can answer are checked, because a handoff asks them to act in
either: the issue, or the pull request.
"""

import unittest
from unittest import mock

from harness import load


class ParkLiftsWhenAnswered(unittest.TestCase):
    def setUp(self):
        self.h = load("heartbeat-issue-tick")

    def responses(self, issue_comments=None, pr_list=None,
                  pr_comments=None, reviews=None):
        """Route gh_get by URL shape so a test states only what it cares about."""
        def fake(url, params=None):
            if url.endswith("/pulls"):
                return pr_list if pr_list is not None else []
            if "/pulls/" in url and url.endswith("/reviews"):
                return reviews if reviews is not None else []
            if "/issues/" in url and url.endswith("/comments"):
                # Distinguish the PR's comment thread from the issue's.
                if pr_comments is not None and "/issues/7/" in url:
                    return pr_comments
                return issue_comments if issue_comments is not None else []
            return []
        return fake

    def answered(self, **kw):
        with mock.patch.object(self.h, "gh_get", self.responses(**kw)):
            return self.h.human_has_answered("o/r", {"number": 5}, "bot")

    def test_the_bots_unanswered_question_is_still_a_wait(self):
        self.assertFalse(self.answered(
            issue_comments=[{"user": {"login": "bot"}, "body": "@owner ?"}]))

    def test_a_reply_on_the_issue_lifts_the_park(self):
        # The exact case that sat for seven hours.
        self.assertTrue(self.answered(issue_comments=[
            {"user": {"login": "bot"}, "body": "@owner ?"},
            {"user": {"login": "owner"}, "body": "fix it autonomously"},
        ]))

    def test_a_reply_on_the_pull_request_lifts_it_too(self):
        # A handoff asks the person to act on the PR, so that is where they
        # often answer. Missing this parks an issue that was answered.
        self.assertTrue(self.answered(
            issue_comments=[{"user": {"login": "bot"}, "body": "@owner ?"}],
            pr_list=[{"number": 7, "head": {"ref": "issue-5-fix"}, "body": ""}],
            pr_comments=[{"user": {"login": "bot"}, "body": "asked"},
                         {"user": {"login": "owner"}, "body": "go ahead"}]))

    def test_a_review_by_a_person_lifts_it(self):
        self.assertTrue(self.answered(
            issue_comments=[{"user": {"login": "bot"}, "body": "@owner ?"}],
            pr_list=[{"number": 7, "head": {"ref": "issue-5-fix"}, "body": ""}],
            pr_comments=[{"user": {"login": "bot"}, "body": "asked"}],
            reviews=[{"user": {"login": "owner"}, "state": "COMMENTED"}]))

    def test_the_bots_own_review_does_not_count_as_an_answer(self):
        self.assertFalse(self.answered(
            issue_comments=[{"user": {"login": "bot"}, "body": "@owner ?"}],
            pr_list=[{"number": 7, "head": {"ref": "issue-5-fix"}, "body": ""}],
            pr_comments=[{"user": {"login": "bot"}, "body": "asked"}],
            reviews=[{"user": {"login": "bot"}, "state": "APPROVED"}]))

    def test_no_comments_at_all_is_not_a_wait(self):
        # Nobody has been asked anything, so nobody is being waited on.
        self.assertTrue(self.answered(issue_comments=[]))

    def test_an_api_failure_resumes_rather_than_parks(self):
        # Being wrong this way costs one spawn that exits in seconds. Being
        # wrong the other way is a park that never lifts.
        with mock.patch.object(self.h, "gh_get", lambda *a, **k: {"message": "boom"}):
            self.assertTrue(
                self.h.human_has_answered("o/r", {"number": 5}, "bot"))


class LinkingPullRequestsToTheIssue(unittest.TestCase):
    def setUp(self):
        self.h = load("heartbeat-issue-tick")

    def prs(self, rows):
        with mock.patch.object(self.h, "gh_get", lambda *a, **k: rows):
            return self.h.open_prs_for_issue("o/r", 5)

    def test_the_runners_branch_naming_links_it(self):
        self.assertEqual(
            self.prs([{"number": 7, "head": {"ref": "issue-5-fix"}, "body": ""}]), [7])

    def test_a_closing_keyword_links_it(self):
        self.assertEqual(
            self.prs([{"number": 8, "head": {"ref": "whatever"},
                       "body": "closes #5"}]), [8])

    def test_a_mere_mention_does_not(self):
        self.assertEqual(
            self.prs([{"number": 9, "head": {"ref": "x"},
                       "body": "unlike #5, this one"}]), [])

    def test_another_issues_branch_does_not(self):
        self.assertEqual(
            self.prs([{"number": 10, "head": {"ref": "issue-51-fix"}, "body": ""}]), [])


if __name__ == "__main__":
    unittest.main()
