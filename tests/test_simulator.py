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


# ---------- Finding 3 (+ follow-up #3): settlement ----------

import simcore  # noqa: E402


def v2(token, resolved, final_price):
    return {"token_id": token, "resolved": resolved, "final_price": final_price}


class Settlement(unittest.TestCase):
    def test_v2_payouts(self):
        f = simcore.payout_from_v2
        self.assertEqual(f(v2("A", True, 1.0)), 1.0)
        self.assertEqual(f(v2("A", True, 0.0)), 0.0)
        self.assertEqual(f(v2("A", True, 0.5)), 0.5)          # 50/50 resolution
        self.assertIsNone(f(v2("A", False, None)))              # not resolved yet
        self.assertIsNone(f(v2("A", True, None)))               # resolved flag but no payout
        self.assertIsNone(f(v2("A", True, 0.99)))               # not a valid payout: don't guess
        self.assertIsNone(f(v2("A", True, float("nan"))))
        self.assertIsNone(f(None))

    def test_gamma_fallback_is_conservative(self):
        f = simcore.payout_from_gamma
        self.assertEqual(f(market([1, 0]), "A"), 1.0)
        self.assertEqual(f(market([1, 0]), "B"), 0.0)
        self.assertEqual(f(market([0.5, 0.5]), "A"), 0.5)
        # the follow-up finding: no status field -> NOT final, however close to 1
        self.assertIsNone(f(market([0.99, 0.01], status=None), "A"))
        self.assertIsNone(f(market([1, 0], status=None), "A"))
        self.assertIsNone(f(market([0.99, 0.01]), "A"))         # resolved, but not exact 1/0
        self.assertIsNone(f(market([1, 0], status="proposed"), "A"))
        self.assertIsNone(f(market([1, 0], status="disputed"), "A"))
        self.assertIsNone(f(market([1, 0], closed=False), "A"))
        self.assertIsNone(f(market([1, 0]), "Z"))
        self.assertIsNone(f({"closed": True, "umaResolutionStatus": "resolved",
                             "clobTokenIds": "{bad", "outcomePrices": "[]"}, "A"))

    def _fake_get(self, v2_rows, gamma_row=None):
        def get(url, params=None):
            if url == simcore.V2_TOKENS:
                want = set((params or {}).get("token_id", "").split(","))
                return {"data": [r for r in v2_rows if r["token_id"] in want]}
            return [gamma_row] if gamma_row else []
        return get

    def test_final_payouts_prefers_v2_and_falls_back(self):
        bets = [{"asset": "A", "condition": "c1"}, {"asset": "B", "condition": "c2"}]
        get = self._fake_get([v2("A", True, 1.0)], market([0, 1], ids=("X", "B")))
        self.assertEqual(simcore.final_payouts(get, bets), {"A": 1.0, "B": 1.0})
        get = self._fake_get([], market([0.99, 0.01], ids=("B", "X"), status=None))
        self.assertEqual(simcore.final_payouts(get, bets), {})

    def _settle(self, mod, led, rows):
        old = mod.get
        mod.get = self._fake_get(rows)
        try:
            return mod.settle(led)
        finally:
            mod.get = old

    def test_no_double_payout_and_split(self):
        for mod in (signals, weather):
            led = {"cash": 49.0, "bets": [{"status": "open", "asset": "A", "condition": "c",
                                           "shares": 1.25, "stake": 1.0, "fee": 0.01,
                                           "price": 0.8, "title": "t", "outcome": "o",
                                           "bucket": "x"}]}
            self._settle(mod, led, [v2("A", True, 1.0)])
            self._settle(mod, led, [v2("A", True, 1.0)])
            self.assertAlmostEqual(led["cash"], 50.25)
            led["bets"][0].update(status="open")
            led["cash"] = 49.0
            led["bets"][0].pop("pnl")
            self._settle(mod, led, [v2("A", True, 0.5)])
            self.assertEqual(led["bets"][0]["status"], "split")
            self.assertAlmostEqual(led["cash"], 49.625)


# ---------- Follow-up #2: partial stops vs holding ----------

class PartialStopSequences(unittest.TestCase):
    """1.25 shares, $1 stake, $0.01 fee; stop sells 0.5 @ 0.30 (sports fee 0.05)."""

    def _ledger(self):
        return {"cash": 48.99, "bets": [{
            "status": "open", "price": 0.80, "shares": 1.25, "stake": 1.0, "fee": 0.01,
            "asset": "A", "condition": "c1", "title": "t", "outcome": "o",
            "traders": ["x"], "event": "e1"}]}

    def _stop(self, led, bk):
        old_get, old_stop, old_fee = signals.get, signals.REAL_STOP, signals.FEE_RATE
        signals.get, signals.REAL_STOP, signals.FEE_RATE = (lambda u, p=None: bk), 0.40, 0.05
        try:
            signals.check_stops(led)
        finally:
            signals.get, signals.REAL_STOP, signals.FEE_RATE = old_get, old_stop, old_fee

    def _settle(self, led, pay):
        old = signals.get
        signals.get = lambda u, p=None: ({"data": [v2("A", True, pay)]}
                                         if u == simcore.V2_TOKENS else [])
        try:
            signals.settle(led)
        finally:
            signals.get = old

    def test_partial_then_win(self):
        led = self._ledger()
        self._stop(led, book(bids=[(0.30, 0.5)]))
        b = led["bets"][0]
        self.assertAlmostEqual(b["recovered"], 0.14475)
        self._settle(led, 1.0)
        self.assertEqual(b["status"], "won")
        self.assertAlmostEqual(b["pnl"], -0.11525)       # actual: 0.75 + 0.14475 - 1.01
        self.assertAlmostEqual(b["hold_pnl"], 0.24)      # holding all 1.25 shares
        self.assertAlmostEqual(signals.hold_baseline(b), 0.24)
        self.assertAlmostEqual(led["cash"], 48.99 + 0.14475 + 0.75)

    def test_partial_then_loss(self):
        led = self._ledger()
        self._stop(led, book(bids=[(0.30, 0.5)]))
        self._settle(led, 0.0)
        b = led["bets"][0]
        self.assertEqual(b["status"], "lost")
        self.assertAlmostEqual(b["pnl"], 0.14475 - 1.01)
        self.assertAlmostEqual(b["hold_pnl"], -1.01)

    def test_partial_then_full_stop_then_resolution(self):
        led = self._ledger()
        self._stop(led, book(bids=[(0.30, 0.5)]))
        self._stop(led, book(bids=[(0.25, 5)]))          # sells the remaining 0.75
        b = led["bets"][0]
        self.assertEqual(b["status"], "stopped")
        self.assertAlmostEqual(b["shares"], 0.0)
        self.assertNotIn("hold_pnl", b)                  # not known until resolution
        self._settle(led, 1.0)
        self.assertAlmostEqual(b["hold_pnl"], 0.24)
        self.assertAlmostEqual(led["cash"], 48.99 + b["recovered"])   # no payout on 0 shares

    def test_shadow_exit_uses_original_shares(self):
        led = self._ledger()
        self._stop(led, book(bids=[(0.30, 0.5)]))
        b = led["bets"][0]
        b["holders"] = {"w": {"name": "x", "size": 100, "exited": "t"}}
        old = signals.get
        signals.get = lambda u, p=None: book(bids=[(0.40, 10)])
        try:
            signals.check_exits(led)
        finally:
            signals.get = old
        sold_all = signals.sell_fill(book(bids=[(0.40, 10)]), 1.25)[1]
        self.assertAlmostEqual(b["shadow_exit"]["pnl"], sold_all - 1.01)


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

    def test_non_finite_cash_stops(self):
        for raw in ('{"cash": NaN, "bets": []}', '{"cash": Infinity, "bets": []}',
                    '{"cash": -Infinity, "bets": []}'):
            with open("w.json", "w") as fh:
                fh.write(raw)
            with self.assertRaises(SystemExit):
                weather.load_ledger("w.json")
            with open(signals.LEDGER_FILE, "w") as fh:
                fh.write(raw)
            with self.assertRaises(SystemExit):
                signals.load_ledger()

    def test_malformed_bets_stop(self):
        good = {"status": "open", "asset": "A", "stake": 1, "fee": 0.01,
                "shares": 1.2, "price": 0.8}
        bad_bets = ["not a bet", dict(good, status="??"), dict(good, asset=""),
                    {k: v for k, v in good.items() if k != "shares"},
                    dict(good, stake=float("nan")), dict(good, fee=float("inf")),
                    dict(good, shares=-1), dict(good, price="0.8"), dict(good, stake=True),
                    dict(good, pnl=float("-inf")), dict(good, recovered=float("nan"))]
        for b in bad_bets:
            with open("w.json", "w") as fh:
                json.dump({"cash": 50, "bets": [b]}, fh)
            with self.assertRaises(SystemExit, msg=repr(b)):
                weather.load_ledger("w.json")
        with open("w.json", "w") as fh:
            json.dump({"cash": 50, "bets": [good]}, fh)
        self.assertEqual(len(weather.load_ledger("w.json")["bets"]), 1)

    def test_never_writes_nan(self):
        with self.assertRaises(ValueError):
            signals.write_json("a.json", {"cash": float("nan"), "bets": []})

    def test_atomic_write_leaves_no_temp_files(self):
        signals.write_json("b.json", {"cash": 1, "bets": []})
        self.assertNotIn(True, [n.endswith(".tmp") for n in os.listdir(".")])


if __name__ == "__main__":
    unittest.main()
