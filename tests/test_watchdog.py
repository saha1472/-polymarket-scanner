"""Offline tests for watchdog.py: when it speaks and when it stays quiet. No network."""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import watchdog  # noqa: E402

NOW = datetime(2026, 11, 25, 12, 0, tzinfo=timezone.utc)


def iso(minutes_ago):
    return (NOW - timedelta(minutes=minutes_ago)).isoformat().replace("+00:00", "Z")


def run(minutes_ago, conclusion="success", status="completed"):
    return {"status": status, "conclusion": conclusion, "created_at": iso(minutes_ago + 11), "updated_at": iso(minutes_ago)}


class Checking(unittest.TestCase):
    def test_a_recent_success_is_all_well(self):
        self.assertIsNone(watchdog.check([run(5), run(20)], NOW))

    def test_just_inside_the_hour_is_all_well(self):
        self.assertIsNone(watchdog.check([run(59)], NOW))

    def test_an_hour_of_silence_raises_the_alarm_with_the_age(self):
        msg = watchdog.check([run(61), run(80)], NOW)
        self.assertIn("1 h 01 min", msg)
        self.assertIn("10:59 UTC", msg)                            # the last good run finished 61 minutes before 12:00

    def test_exactly_the_limit_speaks(self):
        self.assertIsNotNone(watchdog.check([run(watchdog.STALE_MIN)], NOW))

    def test_failed_runs_do_not_count_as_alive(self):
        runs = [run(5, "failure"), run(20, "failure"), run(90)]
        self.assertIn("1 h 30 min", watchdog.check(runs, NOW))

    def test_the_second_hour_still_speaks_then_it_goes_quiet(self):
        self.assertIsNotNone(watchdog.check([run(119)], NOW))
        self.assertIsNone(watchdog.check([run(120)], NOW))
        self.assertIsNone(watchdog.check([run(300)], NOW))

    def test_reminders_after_six_and_twenty_four_hours_of_silence(self):
        for hours in (6, 24, 48, 72):
            self.assertIsNotNone(watchdog.check([run(hours * 60 + 10)], NOW), hours)
            self.assertIsNone(watchdog.check([run(hours * 60 - 10)], NOW), hours)
            self.assertIsNone(watchdog.check([run(hours * 60 + 70)], NOW), hours)

    def test_a_run_in_progress_counts_as_alive(self):
        running = {"status": "in_progress", "conclusion": None, "created_at": iso(10), "updated_at": iso(1)}
        self.assertIsNotNone(watchdog.check([run(90)], NOW))        # without it, 90 minutes of silence would speak
        self.assertIsNone(watchdog.check([running, run(90)], NOW))
        queued = dict(running, status="queued")
        self.assertIsNone(watchdog.check([queued, run(90)], NOW))

    def test_a_run_stuck_for_a_long_time_does_not(self):
        stuck = {"status": "in_progress", "conclusion": None, "created_at": iso(watchdog.RUNNING_GRACE_MIN + 5), "updated_at": iso(1)}
        self.assertIsNotNone(watchdog.check([stuck, run(90)], NOW))

    def test_no_success_among_the_runs_speaks(self):
        self.assertIn("no successful scan run", watchdog.check([run(5, "failure")], NOW))
        self.assertIn("no successful scan run", watchdog.check([], NOW))

    def test_odd_rows_are_ignored(self):
        runs = ["junk", None, {"conclusion": "success", "updated_at": "not a time"}, run(5)]
        self.assertIsNone(watchdog.check(runs, NOW))

    def test_a_time_without_a_zone_is_taken_as_utc(self):
        naive = {"status": "completed", "conclusion": "success", "created_at": "2026-11-25T11:40:00", "updated_at": "2026-11-25T11:55:00"}
        self.assertIsNone(watchdog.check([naive], NOW))


class Command(unittest.TestCase):
    def call(self, content):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "runs.json")
            if content is not None:
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(content)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = watchdog.main(["watchdog.py", path])
            return code, buf.getvalue()

    def test_a_healthy_list_prints_nothing_and_succeeds(self):
        fresh = [{"status": "completed", "conclusion": "success", "created_at": "2099-01-01T00:00:00Z", "updated_at": "2099-01-01T00:10:00Z"}]
        self.assertEqual(self.call(json.dumps(fresh)), (0, ""))

    def test_an_old_list_prints_the_alert_and_still_succeeds(self):
        old = [{"status": "completed", "conclusion": "success", "created_at": "2000-01-01T00:00:00Z", "updated_at": "2000-01-01T00:10:00Z"}]
        code, out = self.call(json.dumps(old))
        self.assertEqual(code, 0)
        self.assertEqual(out, "")          # a very old outage is past every alarm window: it does not spam

    def test_an_unreadable_file_says_so_and_does_not_crash(self):
        for content in (None, "not json", '{"a": 1}'):
            code, out = self.call(content)
            self.assertEqual(code, 0)
            self.assertTrue(out == "" or "could not read" in out or "no successful" in out, (content, out))

    def test_a_missing_argument_is_handled(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(watchdog.main(["watchdog.py"]), 0)
        self.assertIn("could not read", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
