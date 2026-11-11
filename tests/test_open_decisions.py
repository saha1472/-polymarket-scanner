"""Tests for the 11 Nov follow-ups: the rewards line in the daily Discord summary
(heartbeat.py) and the "if event cap were 2/3" lines in shadow_report.py. No
network: everything is built in a temporary folder.

Run:  python -m unittest discover -s tests -v
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import heartbeat                               # noqa: E402
import shadow                                  # noqa: E402
import shadow_report                           # noqa: E402


def ago(hours):
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


class InTempDir(unittest.TestCase):
    def setUp(self):
        self._old = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory()
        os.chdir(self._tmp.name)

    def tearDown(self):
        os.chdir(self._old)
        self._tmp.cleanup()


def log_line(cond, hours_ago, lo=1.0, hi=2.0, fill=0.0, bid_hit=False, ask_hit=False,
             hours=0.25, capital=20.0):
    return {"time": ago(hours_ago), "condition": cond, "question": "q", "hours": hours,
            "accrued": [lo, hi], "bid_hit": bid_hit, "ask_hit": ask_hit, "fill_pnl": fill,
            "capital": capital}


def write_log(rows, raw_extra=()):
    with open(heartbeat.REWARDS_LOG, "w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
        for x in raw_extra:
            fh.write(x + "\n")


class RewardsLine(InTempDir):
    SINCE = None

    def since(self):
        return ago(24)

    def test_last_24h_and_whole_record_are_summed_separately(self):
        write_log([
            log_line("a", 1, lo=1.0, hi=2.0, fill=-0.5, bid_hit=True),
            log_line("b", 2, lo=3.0, hi=5.0, fill=-1.0, ask_hit=True, bid_hit=True),
            log_line("a", 30, lo=10.0, hi=20.0, fill=-4.0),          # older than 24h
        ])
        text = heartbeat.rewards_line(self.since())
        self.assertIn("last 24h reward $4.00-$7.00", text)
        self.assertIn("fills -$1.50 (3 hits)", text)
        self.assertIn("net +$2.50 to +$5.50", text)                   # 4-1.5 and 7-1.5
        self.assertIn("net +$8.50 to +$21.50", text.split("whole record")[1])   # 14-5.5, 27-5.5

    def test_says_it_is_hypothetical(self):
        write_log([log_line("a", 1)])
        text = heartbeat.rewards_line(self.since())
        self.assertIn("hypothetical", text)
        self.assertIn("places no orders", text)
        self.assertIn("estimate", text)

    def test_a_losing_day_shows_minus_signs(self):
        write_log([log_line("a", 1, lo=1.0, hi=2.0, fill=-6.0, bid_hit=True)])
        text = heartbeat.rewards_line(self.since())
        self.assertIn("net -$5.00 to -$4.00", text)

    def test_capital_is_the_largest_quote_per_market_added_up(self):
        write_log([log_line("a", 1, capital=19.8), log_line("a", 2, capital=20.0),
                   log_line("b", 3, capital=10.0)])
        self.assertIn("on about $30 of capital", heartbeat.rewards_line(self.since()))

    def test_days_per_market_from_hours_tracked(self):
        rows = [log_line("a", 1, hours=12), log_line("b", 2, hours=12)]    # 12h each, 2 markets
        write_log(rows)
        self.assertIn("~0.5 days per market", heartbeat.rewards_line(self.since()))

    def test_nothing_in_the_last_day_is_said_plainly(self):
        write_log([log_line("a", 40, lo=3.0, hi=4.0, fill=-1.0)])
        text = heartbeat.rewards_line(self.since())
        self.assertIn("last 24h: no quotes logged", text)
        self.assertIn("net +$2.00 to +$3.00", text)

    def test_no_log_or_an_empty_log_gives_no_line(self):
        self.assertIsNone(heartbeat.rewards_line(self.since()))
        write_log([])
        self.assertIsNone(heartbeat.rewards_line(self.since()))

    def test_damaged_lines_are_skipped_not_fatal(self):
        write_log([log_line("a", 1, lo=1.0, hi=2.0)],
                  raw_extra=["not json", "[1, 2]", "null", '{"time": 5}', '{"time": "x"}',
                             '{"time": "2999-01-01T00:00:00+00:00", "condition": "z", '
                             '"accrued": ["x", null], "fill_pnl": "NaN", "capital": "inf"}'])
        text = heartbeat.rewards_line(self.since())
        self.assertIn("last 24h reward $1.00-$2.00", text)

    def test_odd_numbers_count_as_zero(self):
        write_log([log_line("a", 1, lo=1.0, hi=2.0),
                   {"time": ago(1), "condition": "b", "accrued": "oops", "fill_pnl": None,
                    "capital": float("nan"), "hours": "x"}])
        text = heartbeat.rewards_line(self.since())
        self.assertIsNotNone(text)
        self.assertNotIn("nan", text.lower())
        self.assertNotIn("inf", text.lower())

    def test_never_raises_even_on_an_unreadable_path(self):
        os.mkdir("rewards_dir")
        self.assertIsNone(heartbeat.rewards_line(self.since(), path="rewards_dir"))


class HeartbeatMessage(InTempDir):
    def _run(self):
        with open("ledger.json", "w") as fh:
            json.dump({"cash": 50, "bets": []}, fh)
        sent = []
        old = (heartbeat.simcore.post_discord, os.environ.get("DISCORD_WEBHOOK_URL"),
               heartbeat.SEND_AFTER_HOUR)
        heartbeat.simcore.post_discord = lambda url, text: sent.append(text) or True
        os.environ["DISCORD_WEBHOOK_URL"] = "https://example.invalid/hook"
        heartbeat.SEND_AFTER_HOUR = 0
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                code = heartbeat.main()
        finally:
            heartbeat.simcore.post_discord = old[0]
            heartbeat.SEND_AFTER_HOUR = old[2]
            if old[1] is None:
                os.environ.pop("DISCORD_WEBHOOK_URL", None)
            else:
                os.environ["DISCORD_WEBHOOK_URL"] = old[1]
        return code, sent

    def test_the_daily_message_ends_with_the_rewards_line(self):
        write_log([log_line("a", 1, lo=1.0, hi=2.0, fill=-0.5)])
        code, sent = self._run()
        self.assertEqual(code, 0)
        lines = sent[0].splitlines()
        self.assertIn("Daily heartbeat", lines[0])
        self.assertIn("General copy", lines[1])                     # ledgers still come first
        self.assertTrue(lines[-1].startswith("💸 **Rewards tracker**"))

    def test_without_a_rewards_log_the_message_is_unchanged_and_still_sent(self):
        code, sent = self._run()
        self.assertEqual(code, 0)
        self.assertNotIn("Rewards tracker", sent[0])
        self.assertIn("General copy", sent[0])

    def test_a_damaged_rewards_log_never_blocks_the_heartbeat(self):
        with open(heartbeat.REWARDS_LOG, "wb") as fh:
            fh.write(b"\xff\xfe\x00 not text at all \x00\n")
        code, sent = self._run()
        self.assertEqual(code, 0)
        self.assertEqual(len(sent), 1)


class EventCapLines(InTempDir):
    def _rec(self, book, n, event, status, pnl, created):
        r = book.add("ESPORTS", "skipped", "per_event", asset=f"A{n}", condition=f"c{n}",
                     title=f"t{n}", event=event, price=0.5, shares=2, stake=1.0, fee=0.0)
        r.update(status=status, pnl=pnl, created=created)

    def _report(self, build):
        book = shadow.Book("shadow_esports.json")
        build(book)
        book.save()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            shadow_report.main()
        return buf.getvalue()

    def _line(self, out, label):
        return [ln for ln in out.splitlines() if label in ln][0]

    def test_cap_2_keeps_the_first_extra_per_event_and_cap_3_the_first_two(self):
        def build(b):
            # event E1: three blocked signals, found in the order 1, 2, 3; event E2: one
            self._rec(b, 3, "E1", "lost", -1.0, "2026-11-01T03:00")
            self._rec(b, 1, "E1", "won", 1.0, "2026-11-01T01:00")
            self._rec(b, 2, "E1", "lost", -1.0, "2026-11-01T02:00")
            self._rec(b, 4, "E2", "won", 1.0, "2026-11-01T04:00")
        out = self._report(build)
        two = self._line(out, "if event cap were 2")
        three = self._line(out, "if event cap were 3")
        self.assertIn("2-0", two)                    # E1's first (won) + E2's only (won)
        self.assertIn("+2.00", two)
        self.assertIn("2-1", three)                  # + E1's second (lost)
        self.assertIn("+1.00", three)
        # the plain "skipped" line still counts all four
        self.assertIn("2-2", self._line(out, "skipped: per_event"))

    def test_no_per_event_records_means_no_cap_lines(self):
        def build(b):
            b.add("ESPORTS", "skipped", "chase", asset="A9", condition="c9", title="t",
                  price=0.5, shares=2, stake=1.0, fee=0.0).update(status="won", pnl=1.0)
        out = self._report(build)
        self.assertNotIn("if event cap were", out)

    def test_records_without_an_event_each_count_on_their_own(self):
        def build(b):
            self._rec(b, 1, None, "won", 1.0, "2026-11-01T01:00")
            self._rec(b, 2, None, "lost", -1.0, "2026-11-01T02:00")
        out = self._report(build)
        self.assertIn("1-1", self._line(out, "if event cap were 2"))

    def test_open_only_records_do_not_invent_results(self):
        def build(b):
            r = b.add("ESPORTS", "skipped", "per_event", asset="A1", condition="c1", title="t",
                      event="E", price=0.5, shares=2, stake=1.0, fee=0.0)
            r["created"] = "2026-11-01T01:00"
        out = self._report(build)
        line = self._line(out, "if event cap were 2")
        self.assertIn("1 open", line)
        self.assertIn("0-0", line)                   # nothing settled: no made-up record
        self.assertIn("P&L $  +0.00", line)
        self.assertNotIn("90%", line)                # and no error range


if __name__ == "__main__":
    unittest.main()
