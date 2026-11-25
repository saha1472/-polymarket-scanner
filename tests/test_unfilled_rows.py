"""Offline tests: "unfilled" shadow records (resting bids that expired without a fill) are never counted as results."""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import shadow         # noqa: E402
import shadow_report  # noqa: E402


def row(kind, status="won", pnl=0.3, i=0, reason="expired", strategy="S"):
    return {"strategy": strategy, "kind": kind, "reason": reason, "asset": f"A{i}", "condition": f"C{i}",
            "title": "t", "event": f"e{i}", "outcome": "Yes", "price": 0.8, "shares": 1.25, "stake": 1.0,
            "fee": 0.0, "status": status, "pnl": pnl, "created": f"2026-11-{1 + i % 12:02d}T00:00:00+00:00"}


def unfilled(n, **kw):
    return [row("unfilled", i=i, **kw) for i in range(n)]


class Statistics(unittest.TestCase):
    def test_what_counts_as_a_result(self):
        self.assertFalse(shadow.is_result({"kind": "unfilled"}))
        for kind in ("skipped", "limit", None):
            self.assertTrue(shadow.is_result({"kind": kind}))
        self.assertTrue(shadow.is_result({"status": "won"}))              # a real ledger bet has no kind at all

    def test_roi_stats_leaves_them_out(self):
        real = [row("skipped", pnl=-1.0, status="lost", i=i, reason="cash") for i in range(4)]
        n, mean, se = shadow.roi_stats(real + unfilled(40))
        self.assertEqual((n, mean), (4, -1.0))
        self.assertEqual(shadow.roi_stats(unfilled(40))[0], 0)

    def test_summarize_leaves_them_out_everywhere(self):
        real = [row("skipped", pnl=-1.0, status="lost", i=i, reason="cash") for i in range(4)]
        pending = unfilled(5, status="open", pnl=None) + unfilled(3, status="resting", pnl=None)
        s = shadow.summarize(real + unfilled(40) + pending)
        self.assertEqual((s["settled"], s["won"], s["open"], s["resting"], s["expired"]), (4, 0, 0, 0, 0))
        self.assertAlmostEqual(s["pnl"], -4.0)
        empty = shadow.summarize(unfilled(40))
        self.assertEqual((empty["settled"], empty["won"], empty["pnl"], empty["open"]), (0, 0, 0, 0))

    def test_other_kinds_still_count(self):
        bets = [row("skipped", i=1, reason="cash"), row("limit", i=2, reason="trader_entry")]
        self.assertEqual(shadow.summarize(bets)["settled"], 2)
        self.assertEqual(shadow.roi_stats(bets)[0], 2)

    def test_the_switch_statistics_leave_them_out(self):
        real = [row("skipped", pnl=0.3, i=i, reason="auto_paused") for i in range(10)]
        n, mean, se, days = shadow.switch_stats(real + unfilled(60))
        self.assertEqual(n, 10)
        self.assertEqual(shadow.switch_stats(unfilled(60))[0], 0)

    def test_a_flood_of_them_cannot_resume_a_paused_strategy(self):
        # forty "would have won" rows spread over many days, mislabelled as paused-strategy shadow bets
        fake = [dict(row("unfilled", i=i, reason="auto_paused"), kind="unfilled") for i in range(40)]
        led = {"cash": 50, "bets": [], "auto": {"paused": True, "since": "2026-10-01T00:00:00+00:00"}}
        self.assertIsNone(shadow.switch_update(led, fake, "S"))
        self.assertTrue(shadow.is_paused(led))
        self.assertEqual(shadow.switch_stats(fake)[0], 0)


class Report(unittest.TestCase):
    def setUp(self):
        self._cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory()
        os.chdir(self._tmp.name)

    def tearDown(self):
        os.chdir(self._cwd)
        self._tmp.cleanup()

    def run_report(self, bets, ledger=None):
        book = shadow.Book("shadow_wx.json")
        book.bets = bets
        book.save()
        with open("ledger_wx_morning.json", "w") as fh:
            json.dump(ledger or {"cash": 50.0, "bets": []}, fh)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            shadow_report.main()
        return buf.getvalue()

    def test_unfilled_bids_are_counted_not_scored(self):
        text = self.run_report(unfilled(3, strategy="MORNING") + [row("unfilled", status="lost", pnl=-1.0, i=9, strategy="MORNING")])
        line = next(ln for ln in text.splitlines() if "unfilled resting bids" in ln)
        self.assertIn("4 expired without a fill (3 of 4 settled would have won)", line)
        self.assertIn("Not a return", line)
        self.assertNotIn("ROI", line)
        self.assertNotIn("P&L", line)

    def test_the_other_groups_are_scored_as_before(self):
        bets = unfilled(3, strategy="MORNING") + [row("skipped", pnl=-1.0, status="lost", i=i, reason="cash", strategy="MORNING") for i in range(5, 8)]
        text = self.run_report(bets)
        cash = next(ln for ln in text.splitlines() if "skipped: cash" in ln)
        self.assertIn("ROI -100.0%", cash)

    def test_open_unfilled_rows_do_not_make_a_scored_line(self):
        text = self.run_report(unfilled(2, status="open", pnl=None, strategy="MORNING"))
        line = next(ln for ln in text.splitlines() if "unfilled resting bids" in ln)
        self.assertIn("2 expired without a fill (0 of 0 settled would have won)", line)

    def test_the_description_no_longer_promises_a_verdict_on_them(self):
        self.assertNotIn("would the unfilled weather bids have won", shadow_report.__doc__)


if __name__ == "__main__":
    unittest.main()
