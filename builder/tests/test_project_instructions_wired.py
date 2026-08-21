"""Every runner must actually READ the instruction file its project ships.

`project-instructions.sh` documents itself as "Sourced (not executed) by the
three runners" and names a file per runner. Only the reviewer ever called the
loader. The solver sourced the library and never called it; the tester did not
source it at all. So CLAWCODE-issuesolver-instructions.md and
CLAWCODE-tester-instructions.md sat in target repositories being read by
nobody, and nothing said so — the wiring is invisible when it is missing,
which is exactly why it needs a test rather than a reading.

The contract these lock down:
  - each runner loads ITS OWN file, not another runner's;
  - a repository that ships no file is unaffected (empty string, no block);
  - a blank file counts as absent, so an empty file cannot inject a header
    with nothing under it.
"""

import os
import subprocess
import tempfile
import unittest

from harness import BUILDER

LIB = os.path.join(BUILDER, "project-instructions.sh")

EXPECTED = {
    "fixer-runner.sh": "CLAWCODE-issuesolver-instructions.md",
    "reviewer-runner.sh": "CLAWCODE-reviewer-instructions.md",
    "tester-runner.sh": "CLAWCODE-tester-instructions.md",
}


class EveryRunnerLoadsItsOwnFile(unittest.TestCase):
    def source_of(self, runner):
        with open(os.path.join(BUILDER, runner), encoding="utf-8") as fh:
            return fh.read()

    def test_each_runner_calls_the_loader(self):
        for runner in EXPECTED:
            with self.subTest(runner=runner):
                self.assertIn("load_project_instructions", self.source_of(runner),
                              f"{runner} never calls the loader")

    def test_each_runner_names_its_own_file(self):
        for runner, wanted in EXPECTED.items():
            with self.subTest(runner=runner):
                src = self.source_of(runner)
                self.assertIn(wanted, src, f"{runner} does not name {wanted}")
                for other in set(EXPECTED.values()) - {wanted}:
                    self.assertNotIn(other, src,
                                     f"{runner} reads {other}, which is not its file")

    def test_each_runner_sources_the_library_before_using_it(self):
        # command -v on a function that was never sourced is False, so a
        # missing source degrades to "no instructions" rather than an error —
        # but the source has to be there for the feature to work at all.
        for runner in EXPECTED:
            with self.subTest(runner=runner):
                self.assertIn("project-instructions", self.source_of(runner))


class AMissingFileChangesNothing(unittest.TestCase):
    """The loader itself, driven through bash against a real directory."""

    def load(self, filename, contents=None):
        with tempfile.TemporaryDirectory() as d:
            if contents is not None:
                with open(os.path.join(d, filename), "w", encoding="utf-8") as fh:
                    fh.write(contents)
            script = (
                f'. "{LIB}"\n'
                f'load_project_instructions "{filename}" "" "{d}"\n'
            )
            return subprocess.run(["bash", "-c", script], capture_output=True,
                                  text=True, timeout=60)

    def test_no_file_produces_no_output_and_no_error(self):
        r = self.load("CLAWCODE-issuesolver-instructions.md")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")

    def test_a_blank_file_counts_as_absent(self):
        r = self.load("CLAWCODE-issuesolver-instructions.md", "   \n\n\t\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")

    def test_a_real_file_is_returned_inside_its_framing(self):
        r = self.load("CLAWCODE-issuesolver-instructions.md",
                      "Do not refile the accepted finding about X.")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Do not refile the accepted finding about X.", r.stdout)
        self.assertIn("BEGIN CLAWCODE-issuesolver-instructions.md", r.stdout)
        # The authority bound travels with the content, always.
        self.assertIn("Its authority stops at the safety rules.", r.stdout)

    def test_an_oversized_file_is_truncated_and_says_so(self):
        r = self.load("CLAWCODE-issuesolver-instructions.md", "x" * 9000)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("TRUNCATED", r.stdout)


if __name__ == "__main__":
    unittest.main()


class AskingAPersonIsVisibleOnTheIssue(unittest.TestCase):
    """Every path that stops and waits for a human must park On Hold.

    The marker files live on a volume only the solver pod can read. Without
    the label the issue still reads as in progress: nobody knows the bot is
    waiting on them, and the only symptom is that it quietly stopped moving.
    `park_on_hold` exists to prevent exactly that and says so in its own
    comment — but the lexical-guard ASK, which stops just as hard as an
    escalation, only touched its marker and exited. Seen on
    k8s-ultimate-web-stack#113: asked at 02:20, still unlabelled hours later.
    """

    def setUp(self):
        with open(os.path.join(BUILDER, "fixer-runner.sh"), encoding="utf-8") as fh:
            self.src = fh.read()

    def block_after(self, needle, lines=14):
        i = self.src.index(needle)
        return "\n".join(self.src[i:].splitlines()[:lines])

    def test_the_lexical_guard_ask_parks_before_exiting(self):
        block = self.block_after('if post_issue_comment "$ASK_BODY"; then')
        self.assertIn("park_on_hold", block,
                      "the ASK stops and waits for a person but never labels the issue")

    def test_the_escalation_path_still_parks(self):
        # The path that always did it — pinned so a refactor cannot drop it.
        self.assertIn("park_on_hold", self.block_after("escalated '$fp' to @", 12))

    def test_park_on_hold_is_defined_before_both_callers(self):
        definition = self.src.index("\npark_on_hold() {")
        for caller in [m for m in range(len(self.src))
                       if self.src.startswith("      park_on_hold\n", m)]:
            self.assertGreater(caller, definition,
                               "park_on_hold called before it is defined")
