"""Regression tests for the paper-trading simulator (from the Oct 30 review).

Run:  python -m unittest discover -s tests -v
No network: every API call is mocked.
"""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
_argv = sys.argv
sys.argv = ["signals.py", "SPORTS"]          # signals.py reads its scan from argv
import signals                                # noqa: E402
sys.argv = _argv
import weather                                # noqa: E402


def book(bids=(), asks=()):
    return {"bids": [{"price": str(p), "size": str(s)} for p, s in bids],
            "asks": [{"price": str(p), "size": str(s)} for p, s in asks]}


def market(prices, ids=("A", "B"), closed=True, status="resolved"):
    row = {"closed": closed, "clobTokenIds": json.dumps(list(ids)),
           "outcomePrices": json.dumps([str(p) for p in prices])}
    if status is not None:
        row["umaResolutionStatus"] = status
    return row


def iso(minutes):
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()


class InTempDir(unittest.TestCase):
    def setUp(self):
        self._cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory()
        os.chdir(self._tmp.name)

    def tearDown(self):
        os.chdir(self._cwd)
        self._tmp.cleanup()


# ---------- Finding 1: selling uses real depth ----------

class SellFill(unittest.TestCase):
    def test_insufficient_depth_sells_only_what_exists(self):
        sold, cash, avg = signals.sell_fill(book(bids=[(0.40, 0.01)]), 1.25)
        self.assertAlmostEqual(sold, 0.01)
        self.assertLess(cash, 0.01)          # nowhere near 1.25 x 0.40

    def test_walks_multiple_levels(self):
        sold, cash, avg = signals.sell_fill(book(bids=[(0.50, 1), (0.40, 1)]), 1.5)
        self.assertAlmostEqual(sold, 1.5)
        self.assertTrue(0.40 < avg < 0.50)

    def test_empty_book(self):
        self.assertEqual(signals.sell_fill(book(), 2)[0], 0.0)
        self.assertEqual(signals.sell_fill(None, 2)[0], 0.0)


class RealStop(unittest.TestCase):
    def _ledger(self):
        return {"cash": 49.0, "bets": [{
            "status": "open", "price": 0.60, "shares": 1.25, "stake": 1.0,
            "fee": 0.01, "asset": "A", "condition": "c1", "title": "t",
            "outcome": "o", "traders": ["x"]}]}

    def _run(self, led, bk):
        old_get, old_stop = signals.get, signals.REAL_STOP
        signals.get, signals.REAL_STOP = (lambda u, p=None: bk), 0.40
        try:
            return signals.check_stops(led)
        finally:
            signals.get, signals.REAL_STOP = old_get, old_stop

    def test_partial_stop_keeps_the_rest_open_and_conserves_cash(self):
        led = self._ledger()
        self._run(led, book(bids=[(0.30, 0.5)]))
        b = led["bets"][0]
        self.assertEqual(b["status"], "open")
        self.assertAlmostEqual(b["shares"], 0.75)
        self.assertAlmostEqual(led["cash"], 49.0 + b["recovered"])
        self.assertNotIn("0.25", b.get("stops", {}))   # shadow needs full depth

    def test_full_stop(self):
        led = self._ledger()
        self._run(led, book(bids=[(0.30, 5)]))
        b = led["bets"][0]
        self.assertEqual(b["status"], "stopped")
        self.assertAlmostEqual(b["pnl"], b["recovered"] - 1.01)
        self.assertAlmostEqual(b["orig_shares"], 1.25)

    def test_tiny_bid_cannot_fake_a_full_exit(self):
        led = self._ledger()
        self._run(led, book(bids=[(0.30, 0.01)]))
        self.assertEqual(led["bets"][0]["status"], "open")


# ---------- Finding 2: resting bids and expiry ----------

class Resting(unittest.TestCase):
    def _bet(self, exp_minutes):
        return {"status": "resting", "asset": "A", "price": 0.80, "stake": 1.0,
                "expires": iso(exp_minutes), "bucket": "x", "title": "t"}

    def test_fill_before_expiry(self):
        led = {"cash": 49.0, "bets": [self._bet(60)]}
        weather.check_resting(led, {"A": 0.79}, {"A"})
        self.assertEqual(led["bets"][0]["status"], "open")
        self.assertEqual(led["cash"], 49.0)

    def test_no_fill_after_expiry_even_if_cheap(self):
        led = {"cash": 49.0, "bets": [self._bet(-60)]}
        weather.check_resting(led, {"A": 0.79}, {"A"})
        self.assertEqual(led["bets"][0]["status"], "cancelled")
        self.assertEqual(led["cash"], 50.0)

    def test_cancel_releases_cash_once(self):
        led = {"cash": 49.0, "bets": [self._bet(-60)]}
        weather.check_resting(led, {}, set())
        weather.check_resting(led, {}, set())
        self.assertEqual(led["cash"], 50.0)

    def test_missing_from_scan_is_not_a_cancel(self):
        led = {"cash": 49.0, "bets": [self._bet(60)]}
        weather.check_resting(led, {}, set())      # API hiccup: market not seen
        self.assertEqual(led["bets"][0]["status"], "resting")


# ---------- Finding 3: settlement ----------

class Settlement(unittest.TestCase):
    def test_cases(self):
        for mod in (signals, weather):
            f = mod.resolved_win
            self.assertTrue(f(market([0.99, 0.01]), "A"))          # the boundary bug
            self.assertFalse(f(market([0.99, 0.01]), "B"))
            self.assertTrue(f(market([1, 0]), "A"))
            self.assertIsNone(f(market([0.6, 0.4]), "A"))           # closed, not resolved
            self.assertIsNone(f(market([1, 0], status="proposed"), "A"))
            self.assertIsNone(f(market([1, 0], closed=False), "A"))
            self.assertIsNone(f(market([1, 0]), "Z"))               # token not in market
            self.assertIsNone(f(market([0.5, 0.5]), "A"))           # split: undefined
            self.assertIsNone(f({"closed": True, "clobTokenIds": "{bad",
                                 "outcomePrices": "[]"}, "A"))
            self.assertIsNone(f(None, "A"))
            self.assertTrue(f(market([1, 0], status=None), "A"))    # older markets: no status

    def test_no_double_payout(self):
        led = {"cash": 49.0, "bets": [{"status": "open", "asset": "A", "condition": "c",
                                       "shares": 1.25, "stake": 1.0, "fee": 0.01,
                                       "title": "t", "outcome": "o"}]}
        old = signals.get
        signals.get = lambda u, p=None: [market([1, 0])]
        try:
            signals.settle(led)
            signals.settle(led)
        finally:
            signals.get = old
        self.assertAlmostEqual(led["cash"], 50.25)


# ---------- Finding 5: no re-entry after a stop ----------

class ReEntry(unittest.TestCase):
    def _signal(self, cond, event):
        return {"asset": "A2", "condition": cond, "event": event, "stake": 1,
                "fee": 0.01, "price": 0.6, "shares": 1.66, "order": "market",
                "traders": ["x"], "holders": {}, "hours": 5, "title": "t",
                "outcome": "o", "avg_entry": 0.6, "profit_if_win": 0.6,
                "breakeven": 0.6}

    def test_stopped_market_and_event_are_blocked(self):
        led = {"cash": 49, "bets": [{"status": "stopped", "condition": "c1",
                                     "event": "e1", "asset": "A", "traders": ["x"],
                                     "pnl": -0.5}]}
        signals.open_paper_bets(led, [self._signal("c1", "e9"),
                                      self._signal("c2", "e1")])
        self.assertEqual(len(led["bets"]), 1)

    def test_unrelated_market_still_allowed(self):
        led = {"cash": 49, "bets": [{"status": "stopped", "condition": "c1",
                                     "event": "e1", "asset": "A", "traders": ["x"],
                                     "pnl": -0.5}]}
        signals.open_paper_bets(led, [self._signal("c3", "e3")])
        self.assertEqual(len(led["bets"]), 2)


# ---------- Finding 6: broken ledgers stop the run ----------

class LedgerLoading(InTempDir):
    def test_missing_starts_fresh(self):
        self.assertEqual(signals.load_ledger()["cash"], signals.START_BANKROLL)
        self.assertEqual(weather.load_ledger("x.json")["bets"], [])

    def test_truncated_json_stops(self):
        with open(signals.LEDGER_FILE, "w") as fh:
            fh.write('{"cash": 41.2, "bets": [')
        with self.assertRaises(SystemExit):
            signals.load_ledger()
        with open("w.json", "w") as fh:
            fh.write("{")
        with self.assertRaises(SystemExit):
            weather.load_ledger("w.json")

    def test_wrong_shape_stops(self):
        with open(signals.LEDGER_FILE, "w") as fh:
            json.dump({"cash": "lots", "bets": {}}, fh)
        with self.assertRaises(SystemExit):
            signals.load_ledger()

    def test_atomic_write_leaves_no_temp_files(self):
        signals.write_json("a.json", {"cash": 1, "bets": []})
        self.assertEqual(sorted(os.listdir(".")), ["a.json"])


if __name__ == "__main__":
    unittest.main()
