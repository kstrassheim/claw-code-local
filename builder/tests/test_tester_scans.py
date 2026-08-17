"""The tester's three optional passes, and the gate that guards the live one.

WHY THESE ARE SHELL TESTS
-------------------------
All of it lives in tester-runner.sh, and the blocks are EXTRACTED from the
runner rather than copied, so a restructure fails loudly instead of leaving a
test that passes against code nobody ships.

WHAT MUST NOT BE GOT WRONG
--------------------------
1. **Every pass is OFF until a human switches it on.** The switch is the
   PRESENCE of a flag file, so "a fresh pod" and "off" are the same state. A
   default-on scan is a pod that starts scanning things nobody asked it to.

2. **The pen test is triple-gated**, because actively scanning a host you were
   not authorised to scan can be illegal. The deploy checks must have
   succeeded, the target repository must ship its own `PENTEST_ALLOWED_HOSTS`
   file, and the chat switch must be on. Any one missing and the scan is
   skipped WITH A STATED REASON — a silent skip is indistinguishable from a
   scan that ran and found nothing.

3. **The chat switch cannot authorise a repository.** The scanner's host
   allowlist is set from the target repository's own file and from NOTHING
   else — not from the environment, not from the switch. That is the property
   that makes the switch safe to leave on, so it is pinned here twice: once
   for the unauthorised repository, once for a hostile value sitting in the
   environment.

`curl` is the fake on PATH: no network, no scan, no model call.
"""

import json
import os
import shutil
import unittest

from harness import ShellTestCase

RUNNER = "tester-runner.sh"

SWITCH_START = "# ---- scan switches"
SWITCH_END = "# ---- workspace setup"
DEPLOY_START = "# ---- deploy checks"
DEPLOY_END = "# ---- pentest authorisation gate"
GATE_START = "# ---- pentest authorisation gate"
GATE_END = "# ---- scan prompt sections"
DEDUP_START = "# ---- dedup guard"
DEDUP_END = "CREATED_ISSUES=()"
SLOT_START = "# Concurrency gate"
SLOT_END = "# Remember where the log was"

# A slot is held only by a process whose cmdline still looks like one of this
# repo's runners — a PID alone would let a recycled PID wedge the gate — so a
# test that needs a busy slot parks the stub, whose name carries the marker.
HOLD_A_SLOT = (
    'bash "$PWD/bin/fixer-runner-stub" 30 >/dev/null 2>&1 &\n'
    "OWNER=$!\n"
    "mkdir -p $HOME/.openclaw/.agent-slots/slot-1\n"
    "echo $OWNER > $HOME/.openclaw/.agent-slots/slot-1/pid\n"
    "echo solver > $HOME/.openclaw/.agent-slots/slot-1/owner\n"
    "until tr '\\0' ' ' < /proc/$OWNER/cmdline 2>/dev/null "
    "| grep -q fixer-runner; do sleep 0.1; done\n")

# The API preamble every extracted block assumes the runner already ran.
PREAMBLE = (
    "set -u\n"
    'STATE_ROOT="$PWD/.openclaw"\n'
    "REPO=o/r\n"
    "HEAD_SHA=abc1234def\n"
    "GH_API=https://api.github.com\n"
    'AUTH_HEADER="Authorization: Bearer t"\n'
    'ACCEPT_HEADER="Accept: application/vnd.github+json"\n'
    'APIV_HEADER="X-GitHub-Api-Version: 2022-11-28"\n'
)


class _Blocks(ShellTestCase):
    """Shared plumbing: extract named blocks, and serve canned API bodies."""

    def setUp(self):
        super().setUp()
        self.state = os.path.join(self.home, ".openclaw")
        os.makedirs(self.state, exist_ok=True)
        self.fixtures = os.path.join(self.home, "fixtures")
        os.makedirs(self.fixtures, exist_ok=True)
        self.env["FAKE_CURL_DIR"] = "$PWD/fixtures"
        self.env["FAKE_CURL_LOG"] = "$PWD/curl.log"

    def block(self, start, end, name):
        """extract_block always writes to the same path; keep several."""
        src = self.extract_block(RUNNER, start, end)
        shutil.move(src, os.path.join(self.home, name))
        return name

    def fixture(self, slug, body):
        with open(os.path.join(self.fixtures, slug), "w",
                  encoding="utf-8", newline="\n") as f:
            f.write(body if isinstance(body, str) else json.dumps(body))

    def flag(self, name, on=True):
        path = os.path.join(self.state, name)
        if on:
            open(path, "w").close()
        elif os.path.exists(path):
            os.remove(path)

    def values(self, out):
        """`KEY=value` lines the test script echoed, as a dict."""
        got = {}
        for line in out.splitlines():
            if "=" in line and line.split("=", 1)[0].isupper():
                k, v = line.split("=", 1)
                got[k] = v
        return got


class ScanSwitches(_Blocks):
    """Three independent switches, all of them off by default."""

    def setUp(self):
        super().setUp()
        self.switches = self.block(SWITCH_START, SWITCH_END, "switches.sh")

    def read(self, **flags):
        for name, on in flags.items():
            self.flag(name, on)
        rc, out, err = self.sh(
            PREAMBLE
            + f'source "$PWD/{self.switches}"\n'
            "echo SAST=$SAST_ON\n"
            "echo PENTEST=$PENTEST_ON\n"
            "echo CODEREVIEW=$CR_ON\n")
        self.assertEqual(rc, 0, out + err)
        return self.values(out)

    def test_all_three_are_off_on_a_fresh_pod(self):
        # No flag files anywhere: the state a deployment nobody has touched is
        # in. Every optional pass must be something a human turned on.
        self.assertEqual(
            self.read(),
            {"SAST": "0", "PENTEST": "0", "CODEREVIEW": "0"})

    def test_each_flag_file_switches_on_only_its_own_pass(self):
        for flag, key in ((".sast-enabled", "SAST"),
                          (".pentest-enabled", "PENTEST"),
                          (".codereview-enabled", "CODEREVIEW")):
            with self.subTest(flag=flag):
                got = self.read(**{flag: True})
                self.assertEqual(got[key], "1")
                for other in ("SAST", "PENTEST", "CODEREVIEW"):
                    if other != key:
                        self.assertEqual(got[other], "0",
                                         f"{flag} also switched on {other}")
                self.flag(flag, False)

    def test_removing_the_flag_switches_the_pass_back_off(self):
        # `tester sast off` is `rm -f` on the flag, so off has to be the
        # absence of the file and not a value inside it.
        self.assertEqual(self.read(**{".sast-enabled": True})["SAST"], "1")
        self.assertEqual(self.read(**{".sast-enabled": False})["SAST"], "0")

    def test_the_switch_states_are_logged(self):
        # The run report and the chat status line both quote this; a run whose
        # log does not say which passes were on cannot be read afterwards.
        rc, out, _ = self.sh(PREAMBLE + f'source "$PWD/{self.switches}"\n')
        self.assertIn("SAST=OFF", out)
        self.assertIn("pen-test=OFF", out)
        self.assertIn("AI-code-review=OFF", out)


class DeployChecksGate(_Blocks):
    """Was there a deployment of THIS commit at all?"""

    def setUp(self):
        super().setUp()
        self.deploy = self.block(DEPLOY_START, DEPLOY_END, "deploy.sh")

    def state_for(self, runs):
        if runs is not None:
            self.fixture("repos_o_r_commits_abc1234def_check-runs",
                         {"check_runs": runs})
        rc, out, err = self.sh(
            PREAMBLE + f'source "$PWD/{self.deploy}"\n'
            "echo STATE=$DEPLOY_CHECKS\n")
        self.assertEqual(rc, 0, out + err)
        return self.values(out)["STATE"]

    def test_all_green_is_success(self):
        self.assertEqual(self.state_for([
            {"status": "completed", "conclusion": "success"},
            {"status": "completed", "conclusion": "skipped"}]), "success")

    def test_a_failure_is_failed(self):
        self.assertEqual(self.state_for([
            {"status": "completed", "conclusion": "success"},
            {"status": "completed", "conclusion": "failure"}]), "failed")

    def test_a_run_still_going_is_pending(self):
        self.assertEqual(self.state_for([
            {"status": "in_progress", "conclusion": None}]), "pending")

    def test_no_checks_is_none_and_NOT_success(self):
        # A repository with no checks on this commit has told us nothing about
        # whether it deployed. Reading that as "green" would let the live scan
        # run against whatever happened to be up.
        self.assertEqual(self.state_for([]), "none")

    def test_an_unreachable_api_is_none(self):
        # No fixture: the fake curl exits like `curl -f` on a 404. A failure to
        # ask must not read as a successful deploy.
        self.assertEqual(self.state_for(None), "none")


class PentestTripleGate(_Blocks):
    """Three authorisations, and the scan runs only with all three."""

    HOSTS_SLUG = "repos_o_r_contents_PENTEST_ALLOWED_HOSTS"

    def setUp(self):
        super().setUp()
        self.switches = self.block(SWITCH_START, SWITCH_END, "switches.sh")
        self.gate = self.block(GATE_START, GATE_END, "gate.sh")

    def run_gate(self, *, switch=True, deploy="success", hosts_file=None,
                 inherited=None):
        self.flag(".pentest-enabled", switch)
        if hosts_file is not None:
            self.fixture(self.HOSTS_SLUG, hosts_file)
        pre = ""
        if inherited is not None:
            pre = f'export PENTEST_ALLOWED_HOSTS="{inherited}"\n'
        rc, out, err = self.sh(
            PREAMBLE + pre
            + f'source "$PWD/{self.switches}"\n'
            f"DEPLOY_CHECKS={deploy}\n"
            f'source "$PWD/{self.gate}"\n'
            "echo ACTIVE=$PENTEST_ACTIVE\n"
            "echo HOSTS=$PENTEST_ALLOWED_HOSTS\n"
            "echo REASON=$PENTEST_SKIP_REASON\n")
        self.assertEqual(rc, 0, out + err)
        got = self.values(out)
        got.setdefault("HOSTS", "")
        got.setdefault("REASON", "")
        return got

    # -- each way the gate can refuse -----------------------------------

    def test_switched_off_skips_even_when_everything_else_is_ready(self):
        got = self.run_gate(switch=False, hosts_file="dev.example.test\n")
        self.assertEqual(got["ACTIVE"], "0")
        self.assertEqual(got["HOSTS"], "")
        self.assertIn("switched off", got["REASON"])
        self.assertIn("tester pentest on", got["REASON"])

    def test_an_unauthorised_repository_skips_even_with_the_switch_on(self):
        # No PENTEST_ALLOWED_HOSTS in the repository root at this commit.
        got = self.run_gate(hosts_file=None)
        self.assertEqual(got["ACTIVE"], "0")
        self.assertEqual(got["HOSTS"], "")
        self.assertIn("has not authorised", got["REASON"])
        self.assertIn("PENTEST_ALLOWED_HOSTS", got["REASON"])

    def test_a_comments_only_file_is_not_an_authorisation(self):
        # A file that names no host authorises no host. Treating "present" as
        # "authorised" would let an empty placeholder open the gate.
        got = self.run_gate(hosts_file="# we will fill this in later\n\n")
        self.assertEqual(got["ACTIVE"], "0")
        self.assertEqual(got["HOSTS"], "")
        self.assertIn("has not authorised", got["REASON"])

    def test_failed_deploy_checks_skip_the_scan(self):
        got = self.run_gate(deploy="failed", hosts_file="dev.example.test\n")
        self.assertEqual(got["ACTIVE"], "0")
        self.assertEqual(got["HOSTS"], "")
        self.assertIn("deploy checks", got["REASON"])
        self.assertIn("failed", got["REASON"])

    def test_pending_and_absent_deploy_checks_also_skip_the_scan(self):
        # Scanning here would scan whatever was live BEFORE this commit and
        # report the result against a commit that never shipped.
        for state in ("pending", "none"):
            with self.subTest(deploy=state):
                got = self.run_gate(deploy=state,
                                    hosts_file="dev.example.test\n")
                self.assertEqual(got["ACTIVE"], "0")
                self.assertIn("deploy checks", got["REASON"])
                self.assertIn(state, got["REASON"])

    def test_every_refusal_states_a_reason(self):
        # A skip with no reason is indistinguishable from a scan that ran and
        # found nothing — the run report quotes this string verbatim.
        for kw in (dict(switch=False), dict(hosts_file=None),
                   dict(deploy="failed", hosts_file="dev.example.test\n")):
            with self.subTest(**kw):
                self.assertTrue(self.run_gate(**kw)["REASON"].strip())

    # -- the one way it can authorise -----------------------------------

    def test_all_three_present_authorises_the_scan(self):
        got = self.run_gate(hosts_file="dev.example.test\n")
        self.assertEqual(got["ACTIVE"], "1")
        self.assertEqual(got["HOSTS"], "dev.example.test")
        self.assertEqual(got["REASON"], "")

    def test_the_file_is_reduced_to_bare_hostnames(self):
        got = self.run_gate(hosts_file=(
            "# the dev deployment, and only that\n"
            "https://dev.example.test/app\n"
            "\n"
            "other.example.test:8443   # the API\n"
            "dev.example.test\n"))
        self.assertEqual(got["HOSTS"], "dev.example.test,other.example.test")

    # -- what the switch may never do -----------------------------------

    def test_the_chat_switch_cannot_authorise_a_repository(self):
        # The whole reason the switch is safe to leave on. Switched on, deploy
        # green, and the repository never opted in: nothing is scanned and the
        # scanner's allowlist stays empty.
        got = self.run_gate(hosts_file=None)
        self.assertEqual(got["ACTIVE"], "0")
        self.assertEqual(got["HOSTS"], "")

    def test_an_inherited_env_value_is_never_an_authorisation(self):
        # A PENTEST_ALLOWED_HOSTS already in the pod environment must not
        # survive into the scan: the file in the target repository is the only
        # source. Otherwise anything that can set an env var can pick targets.
        got = self.run_gate(hosts_file=None, inherited="victim.example.com")
        self.assertEqual(got["ACTIVE"], "0")
        self.assertEqual(got["HOSTS"], "")

    def test_an_authorised_run_uses_the_file_and_only_the_file(self):
        got = self.run_gate(hosts_file="dev.example.test\n",
                            inherited="victim.example.com")
        self.assertEqual(got["ACTIVE"], "1")
        self.assertEqual(got["HOSTS"], "dev.example.test")
        self.assertNotIn("victim", got["HOSTS"])

    def test_a_switched_off_run_does_not_even_ask_for_the_file(self):
        # Cheapest gate first: establishing "switched off" costs no API call.
        self.run_gate(switch=False, hosts_file="dev.example.test\n")
        log = os.path.join(self.home, "curl.log")
        body = open(log, encoding="utf-8").read() if os.path.exists(log) else ""
        self.assertNotIn("PENTEST_ALLOWED_HOSTS", body)


class SlotPriority(_Blocks):
    """The tester takes its model slot LAST, and at LOW priority.

    The gate defaults to `high`. A tester left at the default reintroduces
    exactly the starvation the gate exists to prevent: its run is the longest
    of the three, so winning a first-come race lets it sit on a slot for the
    best part of an hour while issues and pull requests queue behind it.
    """

    def setUp(self):
        super().setUp()
        self.slot = self.block(SLOT_START, SLOT_END, "slot.sh")

    def run_slot(self, busy):
        return self.sh(
            PREAMBLE
            + "MAX_AGENT_SLOTS=2\nAGENT_SLOT_RESERVED=1\nAGENT_SLOT_WAIT=0\n"
            + ". $PWD/bin/agent-slot.sh\n"
            + (HOLD_A_SLOT if busy else "")
            + f'source "$PWD/{self.slot}"\n'
            "echo OWNER_FILE=$(cat $AGENT_SLOT/owner)\n"
            "echo REACHED_THE_AGENT=yes\n"
            # Never `kill ${OWNER:-0}`: with OWNER unset that is `kill 0`,
            # which signals the whole process group — the test shell included.
            '[ -n "${OWNER:-}" ] && kill "$OWNER" 2>/dev/null; true\n')

    def test_it_yields_rather_than_starting_beside_the_solver(self):
        rc, out, err = self.run_slot(busy=True)
        self.assertIn("yielding", out, out + err)
        self.assertNotIn("REACHED_THE_AGENT", out,
                         "a tester at high priority is the starvation the "
                         "gate exists to prevent")
        # Yielding costs nothing: this HEAD is not recorded as tested, so the
        # next tick picks the same commit up again.
        self.assertIn("stays untested", out)

    def test_a_high_priority_caller_is_not_blocked_in_the_same_state(self):
        # The control: with one of two slots free, the solver and the reviewer
        # still start. The tester's yield comes from its PRIORITY, not from a
        # full gate.
        rc, out, err = self.sh(
            "MAX_AGENT_SLOTS=2\nAGENT_SLOT_RESERVED=1\nAGENT_SLOT_WAIT=0\n"
            ". $PWD/bin/agent-slot.sh\n"
            + HOLD_A_SLOT
            + "SLOT_NAME=reviewer\nacquire_agent_slot && echo GOT\n"
            "kill $OWNER 2>/dev/null || true\n")
        self.assertIn("GOT", out, out + err)

    def test_an_idle_pod_lets_the_tester_run(self):
        rc, out, err = self.run_slot(busy=False)
        self.assertIn("REACHED_THE_AGENT", out, out + err)
        self.assertIn("OWNER_FILE=tester r", out)


class DuplicateGuard(_Blocks):
    """Nothing is filed that an open issue already reports."""

    def setUp(self):
        super().setUp()
        self.dedup = self.block(DEDUP_START, DEDUP_END, "dedup.sh")
        self.drafts = os.path.join(self.home, "drafts")
        os.makedirs(self.drafts, exist_ok=True)

    def check(self, title, open_titles):
        self.fixture("repos_o_r_issues",
                     [{"title": t} for t in open_titles]
                     + [{"title": "a pull request", "pull_request": {}}])
        with open(os.path.join(self.drafts, "d.json"), "w",
                  encoding="utf-8", newline="\n") as f:
            json.dump({"title": title, "body": "b", "assigneeRole": "BOT"}, f)
        rc, out, err = self.sh(
            PREAMBLE
            + 'DRAFTS_DIR="$PWD/drafts"\n'
            f'source "$PWD/{self.dedup}"\n'
            'if draft_is_duplicate "$DRAFTS_DIR/d.json" >/dev/null; then\n'
            "  echo VERDICT=duplicate\n"
            "else\n"
            "  echo VERDICT=new\n"
            "fi\n")
        self.assertEqual(rc, 0, out + err)
        return self.values(out)["VERDICT"]

    def test_an_identical_open_issue_suppresses_the_draft(self):
        self.assertEqual(
            self.check("Login form submits nothing",
                       ["Login form submits nothing"]),
            "duplicate")

    def test_nothing_open_means_nothing_to_duplicate(self):
        self.assertEqual(self.check("Login form submits nothing", []), "new")

    def test_the_same_failure_on_a_later_commit_is_not_re_filed(self):
        # The commit SHA is in the title, so a plain string match re-filed the
        # identical CI failure on every single commit.
        self.assertEqual(
            self.check("CI failure: build on commit 9f2b71c",
                       ["CI failure: build on commit 4ac0d13"]),
            "duplicate")

    def test_a_reworded_report_of_the_same_thing_is_a_duplicate(self):
        # The passes describe the same weakness in their own words: the static
        # scan, the live scan and the code review each name it differently.
        self.assertEqual(
            self.check("Missing HSTS header on deployed responses",
                       ["Missing HSTS header on the deployed responses"]),
            "duplicate")

    def test_a_security_finding_matches_the_humans_issue_for_it(self):
        # An open issue a human filed suppresses ours just as much as one of
        # ours does, which is why the guard reads ALL open issues and not only
        # the tester-labelled ones.
        self.assertEqual(
            self.check("🔒 Security: missing HSTS header on responses",
                       ["Missing HSTS header on responses"]),
            "duplicate")

    def test_a_different_finding_is_still_filed(self):
        self.assertEqual(
            self.check("🔒 Security: IDOR on /api/orders/{id}",
                       ["Missing HSTS header on responses",
                        "CI failure: build on commit 4ac0d13"]),
            "new")

    def test_a_closed_issue_does_not_suppress_a_regression(self):
        # Only OPEN issues are fetched (state=open), so a defect that was
        # fixed, closed and came back is filed again — which is the report a
        # regression deserves.
        log = "state=open"
        self.check("Login form submits nothing", [])
        with open(os.path.join(self.home, "curl.log"), encoding="utf-8") as f:
            self.assertIn(log, f.read())


if __name__ == "__main__":
    unittest.main()
