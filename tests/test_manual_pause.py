"""Tests for the by-hand pause of a weather strategy (weather.MANUAL_PAUSE).

A manually paused strategy places no new paper bets, keeps recording shadow bets
(reason "manual_pause"), still settles what is already open, and is labelled in
shadow_report.py. No network: every call is faked.

Run:  python -m unittest discover -s tests -v
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import shadow                                  # noqa: E402
import shadow_report                           # noqa: E402
import weather                                 # noqa: E402


class InTempDir(unittest.TestCase):
    def setUp(self):
        self._old = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory()
        os.chdir(self._tmp.name)

    def tearDown(self):
        os.chdir(self._old)
        self._tmp.cleanup()


def fake_bet(strategy, asset="N"):
    return {"strategy": strategy, "asset": asset, "condition": "c", "event": "ev",
            "title": "q", "outcome": "NO", "bucket": "5°C", "price": 0.95, "shares": 1.05,
            "stake": 1.0, "fee": 0.002, "profit_if_win": 0.05, "order": "market",
            "audit": {}}


BUCKET = {"label": "5°C", "yes": "Y", "no": "N", "condition": "c", "question": "q"}


class ManualPause(InTempDir):
    def _open(self, strategy, led, book):
        old = weather.make_bet
        weather.make_bet = lambda s, *a: fake_bet(s)
        try:
            return weather.open_bets(led, [(strategy, {"slug": "ev"}, BUCKET, "NO", "why")], book)
        finally:
            weather.make_bet = old

    def test_late_is_on_the_list_and_every_name_is_a_real_strategy(self):
        self.assertIn("LATE", weather.MANUAL_PAUSE)
        self.assertTrue(set(weather.MANUAL_PAUSE) <= set(weather.STRATEGIES))

    def test_paused_strategy_places_no_bet_but_records_a_shadow_bet(self):
        led = {"cash": 50.0, "bets": []}
        book = shadow.Book("s.json")
        msgs = self._open("LATE", led, book)
        self.assertEqual((led["bets"], led["cash"], msgs), ([], 50.0, []))
        self.assertEqual([(b["strategy"], b["kind"], b["reason"]) for b in book.bets],
                         [("LATE", "skipped", "manual_pause")])

    def test_same_signal_on_every_run_is_still_one_shadow_record(self):
        led = {"cash": 50.0, "bets": []}
        book = shadow.Book("s.json")
        for _ in range(3):
            self._open("LATE", led, book)
        self.assertEqual(len(book.bets), 1)

    def test_other_strategies_are_not_affected(self):
        led = {"cash": 50.0, "bets": []}
        book = shadow.Book("s.json")
        self._open("DEAD", led, book)
        self.assertEqual(len(led["bets"]), 1)
        self.assertEqual(book.bets, [])

    def test_manual_pause_does_not_feed_the_automatic_resume(self):
        # the auto switch only resumes on reason "auto_paused"; a by-hand pause
        # must never be undone by it
        led = {"cash": 50.0, "bets": [], "auto": {"paused": True, "since": "2000-01-01"}}
        book = shadow.Book("s.json")
        self._open("LATE", led, book)
        self.assertEqual([b["reason"] for b in book.bets], ["manual_pause"])
        self.assertIsNone(shadow.switch_update(led, book.bets, "LATE"))

    def test_open_bets_of_a_paused_strategy_still_settle(self):
        bet = dict(fake_bet("LATE", "A"), status="open", opened="2026-11-09T00:00:00+00:00")
        led = {"cash": 49.0, "bets": [bet]}
        old = weather.simcore.final_payouts
        weather.simcore.final_payouts = lambda get, todo: {"A": 1.0}
        try:
            weather.settle(led)
        finally:
            weather.simcore.final_payouts = old
        self.assertEqual(led["bets"][0]["status"], "won")
        self.assertGreater(led["cash"], 49.0)


class ReportLabel(InTempDir):
    def _report(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            shadow_report.main()
        return buf.getvalue()

    def _write(self, reason, ledger_extra=None):
        led = {"cash": 40.0, "bets": [], **(ledger_extra or {})}
        with open("ledger_wx_late.json", "w") as fh:
            json.dump(led, fh)
        book = shadow.Book("shadow_wx.json")
        book.add_taker("LATE", reason, asset="N", condition="c", title="t", price=0.9,
                       fee_rate=0.05)
        book.save()

    def test_report_labels_a_hand_paused_strategy(self):
        self._write("manual_pause")
        out = self._report()
        self.assertIn("LATE: PAUSED BY HAND", out)
        self.assertIn("skipped: manual_pause", out)

    def test_report_keeps_the_auto_pause_note_alongside(self):
        self._write("manual_pause", {"auto": {"paused": True, "since": "2026-11-09T00:00:00",
                                              "why": "x"}})
        self.assertIn("also AUTO-PAUSED", self._report())

    def test_report_unchanged_for_other_strategies(self):
        self._write("cash")
        self.assertIn("LATE: active", self._report())


if __name__ == "__main__":
    unittest.main()
