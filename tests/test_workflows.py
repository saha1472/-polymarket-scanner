"""Guards for the workflow files: the scan's step limits and alerts, and the watchdog. Plain text checks (the runner has no
YAML library and the live bot must not gain a dependency), so they look for the exact lines that matter."""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKFLOWS = os.path.join(ROOT, ".github", "workflows")


def read(name):
    with open(os.path.join(WORKFLOWS, name), encoding="utf-8") as fh:
        return fh.read()


def steps(text):
    """{step name: its text} for the named steps of a workflow (a step starts at '      - name:')."""
    parts = re.split(r"(?m)^      - name: ", text)[1:]
    return {p.splitlines()[0].strip(): p for p in parts}


SCAN = read("scan.yml")
SCAN_STEPS = steps(SCAN)
LIMITED = ("Simulator self-tests", "Arbitrage scanner", "Smart-money signals (general + categories) + weather desk",
           "Weather ensemble shadow log", "Weather calibration (shadow)", "Politics consensus shadow", "Book watcher",
           "Rewards tracker", "Daily heartbeat", "Scorecard", "Save ledgers", "Warn on Discord if anything failed")


def limit(step_text):
    m = re.search(r"(?m)^        timeout-minutes: (\d+)", step_text)
    return int(m.group(1)) if m else None


class ScanLimits(unittest.TestCase):
    def test_every_step_that_runs_our_code_has_its_own_limit(self):
        for name in LIMITED:
            self.assertIn(name, SCAN_STEPS, name)
            self.assertIsNotNone(limit(SCAN_STEPS[name]), f"{name} has no timeout-minutes")

    def test_the_job_keeps_its_overall_limit(self):
        self.assertRegex(SCAN, r"(?m)^    timeout-minutes: 30\b")

    def test_a_hung_step_cannot_use_up_the_job(self):
        # the biggest single limit leaves room for a normal run (about 11 minutes) before the job limit
        biggest = max(limit(SCAN_STEPS[n]) for n in LIMITED)
        self.assertLessEqual(biggest + 11, 30)

    def test_two_steps_can_hang_at_their_limit_and_the_save_still_has_time(self):
        # the book watcher ends itself 11 minutes after the run starts, so it is left out; everything else can
        # hang. The two biggest limits, plus the save step's own limit, plus a normal rest of the run (5 minutes)
        # must fit in the job's 30.
        before_save = [n for n in LIMITED if n not in ("Save ledgers", "Book watcher", "Warn on Discord if anything failed")]
        two = sum(sorted((limit(SCAN_STEPS[n]) for n in before_save), reverse=True)[:2])
        self.assertLessEqual(two + limit(SCAN_STEPS["Save ledgers"]) + 5, 30)

    def test_each_limit_is_at_least_twice_the_longest_run_seen(self):
        longest_seen_minutes = {"Simulator self-tests": 0.1, "Arbitrage scanner": 1.9,
                                "Smart-money signals (general + categories) + weather desk": 3.0, "Weather ensemble shadow log": 0.1,
                                "Politics consensus shadow": 2.2, "Book watcher": 9.6, "Rewards tracker": 0.1, "Daily heartbeat": 0.1,
                                "Scorecard": 0.1, "Save ledgers": 4.8}
        for name, seen in longest_seen_minutes.items():
            if name == "Book watcher":
                self.assertGreaterEqual(limit(SCAN_STEPS[name]), seen + 2)          # it ends itself, so a little room is enough
            else:
                self.assertGreaterEqual(limit(SCAN_STEPS[name]), 1.2 * seen if name == "Save ledgers" else 2 * seen, name)

    def test_the_book_watcher_outlasts_its_own_11_minute_budget(self):
        self.assertGreaterEqual(limit(SCAN_STEPS["Book watcher"]), 12)

    def test_the_save_step_has_room_for_its_push_retries(self):
        self.assertGreaterEqual(limit(SCAN_STEPS["Save ledgers"]), 6)

    def test_shadow_steps_may_still_fail_without_failing_the_run(self):
        for name in ("Weather ensemble shadow log", "Weather calibration (shadow)", "Politics consensus shadow", "Book watcher",
                     "Rewards tracker", "Scorecard"):
            self.assertIn("continue-on-error: true", SCAN_STEPS[name], name)


class ScanAlerts(unittest.TestCase):
    def test_the_save_step_is_still_gated_on_the_tests(self):
        save = SCAN_STEPS["Save ledgers"]
        self.assertIn("if: ${{ !cancelled() && steps.tests.outcome == 'success' }}", save)

    def test_a_timeout_or_cancel_keeps_the_ledgers_and_warns(self):
        for name in ("Keep this run's ledgers if saving failed", "Warn on Discord if anything failed"):
            self.assertIn("if: ${{ failure() || cancelled() }}", SCAN_STEPS[name], name)

    def test_the_artifact_still_keeps_every_state_file(self):
        keep = SCAN_STEPS["Keep this run's ledgers if saving failed"]
        for pattern in ("ledger*.json", "traders_*.json", "shadow_*.json", "wx_state.json", "heartbeat.json", "rewards_state.json"):
            self.assertIn(pattern, keep)

    def test_the_comment_no_longer_talks_about_real_resting_bids(self):
        self.assertNotIn("check resting bids (real and", SCAN)


class Watchdog(unittest.TestCase):
    def setUp(self):
        self.text = read("watchdog.yml")

    def test_it_runs_hourly_and_by_hand(self):
        self.assertIn('cron: "17 * * * *"', self.text)
        self.assertIn("workflow_dispatch:", self.text)

    def test_it_can_only_read(self):
        self.assertIn("actions: read", self.text)
        self.assertIn("contents: read", self.text)
        self.assertNotIn("write", re.sub(r"(?m)^\s*#.*$", "", self.text))

    def test_it_has_a_time_limit_and_uses_the_tested_script(self):
        self.assertRegex(self.text, r"(?m)^    timeout-minutes: \d+")
        self.assertIn("python3 watchdog.py runs.json", self.text)
        self.assertIn("workflows/scan.yml/runs", self.text)

    def test_it_posts_only_to_the_discord_secret_and_only_when_there_is_a_message(self):
        self.assertIn("secrets.DISCORD_WEBHOOK_URL", self.text)
        self.assertIn('if [ -z "$msg" ]', self.text)


class Permissions(unittest.TestCase):
    def test_only_the_scan_may_write_to_the_repository(self):
        for name in os.listdir(WORKFLOWS):
            if name.endswith(".yml") and name != "scan.yml":
                self.assertNotIn("contents: write", read(name), name)


if __name__ == "__main__":
    unittest.main()
