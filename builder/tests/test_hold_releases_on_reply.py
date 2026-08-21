"""The `On Hold` label must come off when the person answers.

The ask the bot posts offers two ways out — "Reply mentioning `@bot` (or
remove the **On Hold** label) and I'll proceed". Only the parenthetical was
ever wired: `remove_label` existed on every forge with no caller anywhere, and
the label gate dropped the issue before a single comment was read. So the
reply the message asks for did nothing, on every host. Observed on eight
issues answered inside ten minutes of each other, not one of which moved.

The bar here is deliberately HIGHER than `human_has_answered`, which ranks a
marker park and only asks whether a person spoke last. This park guards
destructive-sounding work, so what lifts it is the act the ask names: a reply
that @-mentions the bot, after the bot asked. Everything unreadable stays
parked — a park that outlives its answer costs a reply, a park lifted on an
unanswered question costs whatever the issue asked for.
"""

import unittest

import fakeforge
import forge
from fakeforge import note
from harness import load

ASK = "🛑 DESTRUCTIVE CHANGE — PLEASE CONFIRM\n\n@someone — I need clarification"


class HoldReleasesWhenAnswered(unittest.TestCase):
    def setUp(self):
        self.h = load("heartbeat-issue-tick")
        self.forge = fakeforge.FakeForge(identity="bot")
        self.h.FORGES = forge.Forges([self.forge])

    def release(self, notes, labels=("On Hold",)):
        self.forge.notes[5] = list(notes)
        issue = {"number": 5, "labels": list(labels)}
        return self.h.release_hold(self.forge, "o/r", issue, "bot")

    # -- what lifts it ----------------------------------------------------

    def test_a_reply_mentioning_the_bot_after_the_ask_releases(self):
        self.assertTrue(self.release([
            note(ASK, "bot", 1),
            note("@bot go ahead, there is no staging there", "human", 2),
        ]))
        self.assertEqual(self.forge.writes_of("unlabel"), ["On Hold"])

    def test_the_label_is_removed_by_the_name_the_issue_uses(self):
        # `_fold` recognises the park through scope prefixes and punctuation,
        # but the host deletes by exact name — so passing the literal
        # "On Hold" would 404 on an issue that spells it differently.
        self.assertTrue(self.release([
            note(ASK, "bot", 1),
            note("@bot yes please", "human", 2),
        ], labels=("Status::On Hold",)))
        self.assertEqual(self.forge.writes_of("unlabel"), ["Status::On Hold"])

    def test_a_park_with_no_ask_note_anchors_on_the_bots_last_word(self):
        # The solver parks too (fixer-runner's `park`), and that park carries
        # no ASK marker. The bot's newest note is where that wait started.
        self.assertTrue(self.release([
            note("I need a decision before I continue.", "bot", 1),
            note("@bot close it", "human", 2),
        ]))

    # -- what does not ----------------------------------------------------

    def test_a_reply_without_a_mention_is_not_an_answer(self):
        # Somebody talking about the issue is not somebody answering it.
        self.assertFalse(self.release([
            note(ASK, "bot", 1),
            note("this one bit me too", "human", 2),
        ]))
        self.assertEqual(self.forge.writes_of("unlabel"), [])

    def test_a_mention_that_predates_the_ask_is_not_an_answer(self):
        # It answered whatever came before, not the question just asked.
        self.assertFalse(self.release([
            note("@bot have a look at this", "human", 1),
            note(ASK, "bot", 2),
        ]))

    def test_the_bot_mentioning_itself_is_not_an_answer(self):
        self.assertFalse(self.release([
            note(ASK, "bot", 1),
            note("@bot still waiting on a person", "bot", 2),
        ]))

    def test_a_park_nobody_asked_for_is_not_the_bots_to_lift(self):
        # No note from the bot at all: a human applied the label by hand, and
        # a human takes it off by hand.
        self.assertFalse(self.release([
            note("@bot do this", "human", 1),
        ]))

    def test_an_issue_without_the_label_is_not_released(self):
        self.assertFalse(self.release([
            note(ASK, "bot", 1),
            note("@bot go", "human", 2),
        ], labels=("bug",)))

    # -- failure direction ------------------------------------------------

    def test_an_unreadable_thread_stays_parked(self):
        # Opposite direction to human_has_answered, and deliberately so.
        self.forge.raises["comments"] = forge.ForgeError("boom")
        self.assertFalse(self.release([note(ASK, "bot", 1)]))

    def test_a_failed_removal_stays_parked(self):
        # The label is still on the issue, so the planner must still treat it
        # as parked — otherwise it spawns every tick against a live park.
        self.forge.writes_fail = True
        self.assertFalse(self.release([
            note(ASK, "bot", 1),
            note("@bot go ahead", "human", 2),
        ]))


class TheParkLabelIsFoundByItsSpelling(unittest.TestCase):
    """`on_hold_label_name` — which label to delete, given the issue.

    Separate from the release decision because the two fail differently: not
    recognising the park leaves the issue stuck, and recognising it under a
    name the host does not have 404s the delete.
    """

    def setUp(self):
        self.h = load("heartbeat-issue-tick")

    def name_of(self, *labels):
        return self.h.on_hold_label_name({"number": 5, "labels": list(labels)})

    def test_the_plain_spelling(self):
        self.assertEqual(self.name_of("bug", "On Hold"), "On Hold")

    def test_a_scope_prefix_is_tolerated_and_returned_intact(self):
        self.assertEqual(self.name_of("Status::On Hold"), "Status::On Hold")

    def test_punctuation_and_case_are_tolerated(self):
        self.assertEqual(self.name_of("on-hold"), "on-hold")
        self.assertEqual(self.name_of("ON_HOLD"), "ON_HOLD")

    def test_an_issue_with_no_park_has_no_name(self):
        self.assertEqual(self.name_of("bug", "Priority::High"), "")

    def test_no_labels_at_all(self):
        self.assertEqual(self.h.on_hold_label_name({"number": 5}), "")


class TheAskNoteIsFound(unittest.TestCase):
    """`lexical_guard.ask_note_id` — the anchor both halves of the fix use.

    The planner reads it to know when the wait started; the solver anchors its
    first-run cursor on it so the reply that arrived before the run existed is
    still unread. It shipped with no caller at all until now, so this is its
    first exercise.
    """

    def setUp(self):
        self.g = load("lexical_guard")

    def test_the_bots_ask_is_found(self):
        self.assertEqual(self.g.ask_note_id([
            note("chatter", "human", 1),
            note(ASK, "bot", 2),
        ], "bot"), 2)

    def test_the_newest_ask_wins_when_it_was_asked_twice(self):
        # A reworded issue can be asked about again; the live wait is the
        # latest one, and anchoring on the older would read the first reply
        # as an answer to the second question.
        self.assertEqual(self.g.ask_note_id([
            note(ASK, "bot", 2),
            note("@bot ok", "human", 3),
            note(ASK, "bot", 7),
        ], "bot"), 7)

    def test_an_ask_worded_by_somebody_else_is_not_the_bots(self):
        self.assertIsNone(self.g.ask_note_id([note(ASK, "human", 1)], "bot"))

    def test_an_ordinary_bot_comment_is_not_an_ask(self):
        self.assertIsNone(self.g.ask_note_id([
            note("opened a pull request", "bot", 1),
        ], "bot"))

    def test_no_notes_at_all(self):
        self.assertIsNone(self.g.ask_note_id([], "bot"))


if __name__ == "__main__":
    unittest.main()
