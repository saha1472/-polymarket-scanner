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


class SplitDisplay(unittest.TestCase):
    def test_weather_summary_counts_split_separately(self):
        led = {"cash": 50.0, "bets": [
            {"status": "won", "pnl": 0.2}, {"status": "lost", "pnl": -1.0},
            {"status": "split", "pnl": -0.38}]}
        text = weather.summary(led)
        self.assertIn("record 1-1 (+1 split)", text)

    def test_weather_summary_single_split_is_not_a_loss(self):
        text = weather.summary({"cash": 50.0, "bets": [{"status": "split", "pnl": -0.38}]})
        self.assertIn("record 0-0 (+1 split)", text)


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


# ---------- Audit trail and the read-only settlement checker ----------

import verify_settlements  # noqa: E402


class AuditTrail(unittest.TestCase):
    BOOK = book(bids=[(0.58, 10), (0.60, 5), (0.55, 1)],
                asks=[(0.66, 3), (0.62, 2), (0.70, 9), (0.64, 1), (0.68, 1), (0.75, 1)])

    def test_book_snapshot_best_first_and_capped(self):
        snap = simcore.book_snapshot(self.BOOK, levels=5)
        self.assertEqual([a[0] for a in snap["asks"]], [0.62, 0.64, 0.66, 0.68, 0.70])
        self.assertEqual([b[0] for b in snap["bids"]], [0.60, 0.58, 0.55])
        self.assertIsNone(simcore.book_snapshot(None))

    def _fresh_version(self, sha_env, git_out):
        old = (os.environ.get("GITHUB_SHA"), simcore._CODE_VERSION, simcore.subprocess.run)
        if sha_env is None:
            os.environ.pop("GITHUB_SHA", None)
        else:
            os.environ["GITHUB_SHA"] = sha_env

        class R:
            stdout = git_out

        def fake_run(*a, **k):
            if git_out is None:
                raise OSError("no git")
            return R()
        simcore.subprocess.run = fake_run
        simcore._CODE_VERSION = None
        return old

    def _restore(self, old):
        simcore._CODE_VERSION = old[1]
        simcore.subprocess.run = old[2]
        if old[0] is None:
            os.environ.pop("GITHUB_SHA", None)
        else:
            os.environ["GITHUB_SHA"] = old[0]

    def test_code_version_is_the_checked_out_commit(self):
        old = self._fresh_version("111111111111aaaa", "222222222222bbbb\n")
        try:
            self.assertEqual(simcore.code_version(), "222222222222")     # HEAD wins
            a = simcore.audit("X", {})
            self.assertEqual(a["code"], "222222222222")
            self.assertEqual(a["trigger_sha"], "111111111111")          # kept separately
        finally:
            self._restore(old)

    def test_trigger_sha_only_stored_when_different(self):
        old = self._fresh_version("222222222222bbbb", "222222222222bbbb\n")
        try:
            self.assertNotIn("trigger_sha", simcore.audit("X", {}))
        finally:
            self._restore(old)

    def test_falls_back_to_github_sha_without_git(self):
        old = self._fresh_version("333333333333cccc", None)
        try:
            self.assertEqual(simcore.code_version(), "333333333333")
        finally:
            self._restore(old)

    def test_copy_bet_records_audit(self):
        s = {"asset": "A2", "condition": "c9", "event": "e9", "stake": 1, "fee": 0.01,
             "price": 0.62, "shares": 1.6, "order": "market", "traders": ["x"],
             "holders": {"w": {"name": "x", "size": 100, "win_rate": 0.6, "roi": 0.2, "n": 40}},
             "hours": 5, "title": "t", "outcome": "o", "avg_entry": 0.6,
             "profit_if_win": 0.5, "breakeven": 0.63, "book": self.BOOK}
        led = {"cash": 50, "bets": []}
        signals.open_paper_bets(led, [s])
        a = led["bets"][0]["audit"]
        self.assertEqual(a["scan"], signals.SCAN)
        self.assertIn("code", a)
        self.assertEqual(a["settings"]["min_price"], signals.MIN_PRICE)
        self.assertEqual(a["book"]["asks"][0], [0.62, 2.0])
        self.assertEqual(led["bets"][0]["holders"]["w"]["win_rate"], 0.6)
        json.dumps(led, allow_nan=False)            # must stay serialisable

    def test_weather_bet_records_audit(self):
        old = weather.get
        weather.get = lambda u, p=None, timeout=25: self.BOOK
        try:
            bet = weather.make_bet("MORNING", {"slug": "ev"},
                                   {"yes": "Y", "no": "N", "condition": "c", "question": "q",
                                    "label": "7°C"}, "YES", "test")
        finally:
            weather.get = old
        self.assertEqual(bet["audit"]["scan"], "WX-MORNING")
        self.assertEqual(bet["audit"]["settings"]["hours"], [8, 12])
        self.assertEqual(bet["audit"]["book"]["asks"][0], [0.62, 2.0])

    def test_resting_fill_records_evidence(self):
        led = {"cash": 49.0, "bets": [{"status": "resting", "asset": "A", "price": 0.80,
                                       "stake": 1.0, "expires": iso(60), "bucket": "x",
                                       "title": "t"}]}
        weather.check_resting(led, {"A": 0.79}, {"A"})
        ev = led["bets"][0]["fill_evidence"]
        self.assertEqual(ev["seen_ask"], 0.79)
        self.assertIn("proxy", ev["note"])


class SettlementChecker(unittest.TestCase):
    def _bet(self, **kw):
        b = {"status": "won", "asset": "A", "shares": 1.25, "stake": 1.0, "fee": 0.01,
             "price": 0.8, "pnl": 0.24, "title": "t"}
        b.update(kw)
        return b

    def _check(self, bets, payouts, cash=None):
        if cash is None:
            cash = 50 + sum(b.get("pnl", 0) for b in bets
                            if b["status"] in verify_settlements.SETTLED)
        led = {"cash": cash, "bets": bets}
        return verify_settlements.check_ledger("L", led, payouts)

    def test_clean_ledger_passes(self):
        rows, problems, gap = self._check([self._bet()], {"A": 1.0})
        self.assertEqual(problems, [])

    def test_catches_wrong_outcome(self):
        _, problems, _ = self._check([self._bet()], {"A": 0.0})
        self.assertTrue(any("ledger says won" in p for p in problems))

    def test_catches_wrong_pnl(self):
        _, problems, _ = self._check([self._bet(pnl=0.5)], {"A": 1.0})
        self.assertTrue(any("pnl" in p for p in problems))

    def test_checks_hold_for_partial_stops(self):
        b = self._bet(shares=0.75, recovered=0.14475, pnl=-0.11525, orig_shares=1.25,
                      hold_pnl=0.24)
        self.assertEqual(self._check([b], {"A": 1.0})[1], [])
        b["hold_pnl"] = -0.11525                     # the old bug: hold == actual
        self.assertTrue(any("hold_pnl" in p for p in self._check([b], {"A": 1.0})[1]))

    def test_missing_hold_on_partial_stop_fails(self):
        b = self._bet(shares=0.75, recovered=0.14475, pnl=-0.11525, orig_shares=1.25)
        _, problems, _ = self._check([b], {"A": 1.0})
        self.assertTrue(any("no hold_pnl" in p for p in problems))

    def test_missing_hold_on_full_stop_fails(self):
        b = self._bet(status="stopped", shares=0.0, recovered=0.4, pnl=-0.61, orig_shares=1.25)
        _, problems, _ = self._check([b], {"A": 1.0})
        self.assertTrue(any("no hold_pnl" in p for p in problems))

    def test_missing_or_non_finite_numbers_fail(self):
        nan, inf = float("nan"), float("inf")
        cases = [{"pnl": nan}, {"pnl": inf}, {"stake": nan}, {"fee": -inf},
                 {"shares": nan}, {"pnl": "0.24"}, {"pnl": None}]
        for change in cases:
            _, problems, _ = self._check([self._bet(**change)], {"A": 1.0}, cash=50.24)
            self.assertTrue(problems, msg=repr(change))
        for key in ("pnl", "stake", "fee", "shares"):
            b = self._bet()
            b.pop(key)
            _, problems, _ = self._check([b], {"A": 1.0}, cash=50.24)
            self.assertTrue(any("missing" in p for p in problems), msg=key)
        b = self._bet(shares=0.75, recovered=nan, pnl=-0.11525, orig_shares=1.25, hold_pnl=0.24)
        self.assertTrue(self._check([b], {"A": 1.0}, cash=50)[1])
        b = self._bet(shares=0.75, recovered=0.14475, pnl=-0.11525, orig_shares=1.25,
                      hold_pnl=inf)
        self.assertTrue(self._check([b], {"A": 1.0}, cash=50)[1])

    # --- GPT's fourth-round cases: required fields by status, no zero defaults ---
    def _stopped(self, **kw):
        b = {"status": "stopped", "asset": "A", "shares": 0.0, "stake": 1.0, "fee": 0.01,
             "price": 0.6, "title": "t", "recovered": 0.4, "orig_shares": 1.25,
             "hold_pnl": 0.24, "pnl": -0.61}
        b.update(kw)
        return b

    def test_modern_stop_correct_passes(self):
        self.assertEqual(self._check([self._stopped()], {"A": 1.0})[1], [])

    def test_stopped_missing_pnl_fails(self):
        b = self._stopped()
        b.pop("pnl")
        _, problems, _ = self._check([b], {"A": 1.0}, cash=50.0)
        self.assertTrue(any("pnl" in p for p in problems))

    def test_stopped_wrong_pnl_with_matching_cash_fails(self):
        b = self._stopped(pnl=0.5)                       # truth: 0.4 - 1.01 = -0.61
        _, problems, _ = self._check([b], {"A": 1.0}, cash=50.5)
        self.assertTrue(any("recovered - stake - fee" in p for p in problems))

    def test_open_missing_stake_with_matching_cash_fails(self):
        b = {"status": "open", "asset": "B", "shares": 1.2, "fee": 0.01, "price": 0.8,
             "title": "t"}
        _, problems, _ = self._check([b], {}, cash=49.99)
        self.assertTrue(any("stake" in p for p in problems))
        self.assertTrue(any("not reconciled" in p for p in problems))

    def test_open_and_resting_required_fields(self):
        for status in ("open", "resting"):
            for key in ("stake", "fee", "shares", "price"):
                b = {"status": status, "asset": "B", "shares": 1.2, "stake": 1.0,
                     "fee": 0.0, "price": 0.8, "title": "t"}
                b.pop(key)
                self.assertTrue(self._check([b], {}, cash=49)[1], msg=(status, key))

    def test_legacy_stop_is_range_checked_not_rederived(self):
        legacy = {"status": "stopped", "asset": "A", "shares": 1.49, "stake": 1.0,
                  "fee": 0.0165, "price": 0.67, "title": "t", "pnl": -0.90,
                  "hold_pnl": -1.0165}
        rows, problems, _ = self._check([legacy], {"A": 0.0})
        self.assertEqual(problems, [])
        self.assertEqual(rows[0][3], "legacy")
        impossible = dict(legacy, pnl=-5.0)              # lost more than was staked
        self.assertTrue(any("outside the possible range" in p
                            for p in self._check([impossible], {"A": 0.0})[1]))

    def test_unknown_status_fails(self):
        _, problems, _ = self._check([self._bet(status="weird")], {"A": 1.0}, cash=50)
        self.assertTrue(any("unknown status" in p for p in problems))

    def test_non_finite_cash_fails(self):
        _, problems, _ = self._check([self._bet()], {"A": 1.0}, cash=float("nan"))
        self.assertTrue(any("cash" in p for p in problems))

    def test_catches_cash_gap_and_unresolved(self):
        _, problems, gap = self._check([self._bet()], {"A": 1.0}, cash=10.0)
        self.assertTrue(any("reconcile" in p for p in problems))
        _, problems, _ = self._check([self._bet()], {})
        self.assertTrue(any("no resolved payout" in p for p in problems))


# ---------- Consolidated review batch (f25af6c) ----------

import heartbeat  # noqa: E402


class PerTraderCap(unittest.TestCase):
    def _sig(self, traders, n):
        return {"asset": f"N{n}", "condition": f"cN{n}", "event": f"eN{n}", "stake": 1,
                "fee": 0.01, "price": 0.6, "shares": 1.6, "order": "market",
                "traders": traders, "holders": {}, "hours": 5, "title": "t", "outcome": "o",
                "avg_entry": 0.6, "profit_if_win": 0.5, "breakeven": 0.6}

    def _ledger_with(self, trader, count):
        return {"cash": 50, "bets": [{"status": "open", "condition": f"c{i}", "event": f"e{i}",
                                      "asset": f"a{i}", "traders": [trader]}
                                     for i in range(count)]}

    def test_capped_trader_blocks_even_with_uncapped_partner(self):
        led = self._ledger_with("A", signals.MAX_OPEN_PER_TRADER)
        signals.open_paper_bets(led, [self._sig(["A", "B"], 1), self._sig(["B", "A"], 2)])
        self.assertEqual(len(led["bets"]), signals.MAX_OPEN_PER_TRADER)

    def test_uncapped_group_allowed_and_cap_applies_within_one_run(self):
        led = self._ledger_with("A", signals.MAX_OPEN_PER_TRADER - 1)
        signals.open_paper_bets(led, [self._sig(["A", "B"], 1), self._sig(["A"], 2),
                                      self._sig(["B"], 3)])
        opened = [b["asset"] for b in led["bets"][signals.MAX_OPEN_PER_TRADER - 1:]]
        self.assertEqual(opened, ["N1", "N3"])    # N2 blocked: A reached the cap via N1


class CheckerValidation(unittest.TestCase):
    """One suite for the checker: structure, identity, fields, payouts, stops."""

    def _bet(self, **kw):
        b = {"status": "won", "asset": "A", "shares": 1.25, "stake": 1.0, "fee": 0.01,
             "price": 0.8, "pnl": 0.24, "title": "t"}
        b.update(kw)
        return b

    def _problems(self, led, payouts=None, invalid=None):
        return verify_settlements.check_ledger("L", led, payouts or {}, invalid)[1]

    def test_ledger_structure(self):
        self.assertTrue(self._problems({"cash": 50}))                    # no bets field
        self.assertTrue(self._problems({"cash": 50, "bets": {}}))        # bets not a list
        self.assertTrue(self._problems({"bets": []}))                    # no cash
        self.assertTrue(self._problems({"cash": True, "bets": []}))      # boolean cash
        self.assertTrue(self._problems([]))                              # not an object
        self.assertEqual(self._problems({"cash": 50, "bets": []}), [])

    def test_bet_identity_and_types_no_crash(self):
        b = self._bet()
        b.pop("asset")
        self.assertTrue(any("asset" in p for p in self._problems({"cash": 50.24, "bets": [b]},
                                                                  {"A": 1.0})))
        for bad in ("not a bet", 7, None, self._bet(asset=""), self._bet(asset=5),
                    self._bet(stake=True), self._bet(pnl=False), self._bet(price="0.8")):
            self.assertTrue(self._problems({"cash": 50.24, "bets": [bad]}, {"A": 1.0}),
                            msg=repr(bad))

    def test_unresolved_stop_with_wrong_pnl_fails(self):
        stop = {"status": "stopped", "asset": "A", "shares": 0.0, "stake": 1.0, "fee": 0.01,
                "price": 0.6, "title": "t", "recovered": 0.4, "orig_shares": 1.25, "pnl": 0.5}
        self.assertTrue(any("recovered - stake - fee" in p
                            for p in self._problems({"cash": 50.5, "bets": [stop]}, {})))
        stop["pnl"] = -0.61                                              # correct: passes,
        self.assertEqual(self._problems({"cash": 49.39, "bets": [stop]}, {}), [])   # pending

    def test_invalid_official_payouts_are_rejected(self):
        good, invalid = verify_settlements.parse_payout_records([
            {"token_id": "A", "resolved": True, "final_price": float("nan")},
            {"token_id": "B", "resolved": True, "final_price": 0.99},
            {"token_id": "C", "resolved": True, "final_price": True},
            {"token_id": "D", "resolved": True, "final_price": "1"},
            {"token_id": "E", "resolved": True, "final_price": 0.5},
            {"token_id": "F", "resolved": False, "final_price": None},
            {"token_id": "G", "resolved": "true", "final_price": 1.0},
        ])
        self.assertEqual(good, {"E": 0.5})
        self.assertEqual(set(invalid), {"A", "B", "C", "D"})
        split = self._bet(status="split", pnl=123.0)
        probs = self._problems({"cash": 173.0, "bets": [split]}, {}, {"A": invalid["A"]})
        self.assertTrue(any("invalid payout" in p for p in probs))

    def test_main_with_no_ledgers_and_end_to_end(self):
        self.assertEqual(verify_settlements.main([]), 2)
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "ledger.json")
            with open(p, "w") as fh:
                json.dump({"cash": 50.24, "bets": [self._bet()]}, fh)

            class Resp:
                def raise_for_status(self):
                    pass

                def json(self):
                    return {"data": [{"token_id": "A", "resolved": True, "final_price": 1.0}]}

            class Http:
                def get(self, *a, **k):
                    return Resp()
            self.assertEqual(verify_settlements.main([p], http=Http()), 0)
            with open(p, "w") as fh:
                fh.write("{broken")
            self.assertEqual(verify_settlements.main([p], http=Http()), 1)


class DiscordDelivery(unittest.TestCase):
    class Resp:
        def __init__(self, code, body=None, headers=None):
            self.status_code, self._body, self.headers = code, body, headers or {}

        def json(self):
            if self._body is None:
                raise ValueError("no json")
            return self._body

    def _http(self, *responses):
        seq = list(responses)
        sent = []

        class H:
            def post(self, url, json=None, timeout=None):
                sent.append(json["content"])
                r = seq.pop(0)
                if isinstance(r, Exception):
                    raise r
                return r
        return H(), sent

    def test_success(self):
        h, sent = self._http(self.Resp(204))
        self.assertTrue(simcore.post_discord("u", "hi", http=h, sleep=lambda s: None))

    def test_rate_limit_is_retried_with_retry_after(self):
        waits = []
        h, _ = self._http(self.Resp(429, {"retry_after": 2.5}), self.Resp(204))
        self.assertTrue(simcore.post_discord("u", "hi", http=h, sleep=waits.append))
        self.assertEqual(waits, [2.5])

    def test_server_errors_eventually_fail(self):
        h, _ = self._http(*[self.Resp(500)] * simcore.DISCORD_ATTEMPTS)
        self.assertFalse(simcore.post_discord("u", "hi", http=h, sleep=lambda s: None))

    def test_client_error_fails_without_retry(self):
        h, sent = self._http(self.Resp(400), self.Resp(204))
        self.assertFalse(simcore.post_discord("u", "hi", http=h, sleep=lambda s: None))
        self.assertEqual(len(sent), 1)

    def test_network_error_then_success(self):
        h, _ = self._http(OSError("down"), self.Resp(200))
        self.assertTrue(simcore.post_discord("u", "hi", http=h, sleep=lambda s: None))

    def test_every_chunk_must_be_delivered(self):
        text = "x" * (simcore.DISCORD_CHUNK + 10)
        h, sent = self._http(self.Resp(204), self.Resp(403))
        self.assertFalse(simcore.post_discord("u", text, http=h, sleep=lambda s: None))
        self.assertEqual(len(sent), 2)


class HeartbeatDelivery(InTempDir):
    def _run(self, delivered):
        with open("ledger.json", "w") as fh:
            json.dump({"cash": 50, "bets": []}, fh)
        old = (simcore.post_discord, os.environ.get("DISCORD_WEBHOOK_URL"), heartbeat.SEND_AFTER_HOUR)
        heartbeat.simcore.post_discord = lambda url, text: delivered
        os.environ["DISCORD_WEBHOOK_URL"] = "https://example.invalid/hook"
        heartbeat.SEND_AFTER_HOUR = 0
        try:
            return heartbeat.main()
        finally:
            heartbeat.simcore.post_discord = old[0]
            heartbeat.SEND_AFTER_HOUR = old[2]
            if old[1] is None:
                os.environ.pop("DISCORD_WEBHOOK_URL", None)
            else:
                os.environ["DISCORD_WEBHOOK_URL"] = old[1]

    def test_failed_delivery_is_not_marked_sent(self):
        self.assertEqual(self._run(False), 1)
        self.assertFalse(os.path.exists(heartbeat.STATE_FILE))

    def test_successful_delivery_is_marked_sent(self):
        self.assertEqual(self._run(True), 0)
        with open(heartbeat.STATE_FILE) as fh:
            self.assertIn("last", json.load(fh))

    def test_broken_ledger_is_shown_not_hidden(self):
        with open("ledger_x.json", "w") as fh:
            fh.write('{"cash": NaN, "bets": []}')
        row = heartbeat.line("ledger_x.json", "2000")
        self.assertIn("can't be read or is invalid", row[1])


# ---------- weather readings (Oct 31) ----------

import wx_ensemble                             # noqa: E402
import wx_ensemble_eval                        # noqa: E402
from datetime import date                      # noqa: E402
from zoneinfo import ZoneInfo                  # noqa: E402


def ts_local(tz, d, hour, minute=0):
    return int(datetime(d.year, d.month, d.day, hour, minute, tzinfo=ZoneInfo(tz)).timestamp())


class MetarReadings(unittest.TestCase):
    """The METAR API returns at most 400 rows per call (newest first). Asking
    for 40 stations x 36h used to cut every station down to ~6 hours, so the
    'low so far' at night missed the dawn low."""

    def test_capped_batch_is_split_until_complete(self):
        calls = []

        def fake_get(url, params=None, timeout=25):
            ids = params["ids"].split(",")
            calls.append(ids)
            rows = [{"icaoId": s, "n": i} for s in ids for i in range(72)]
            return rows[:weather.METAR_ROW_CAP]          # API truncation
        old = weather.get
        weather.get = fake_get
        try:
            rows = weather.metar_rows(["A", "B", "C", "D", "E", "F", "G", "H"])
        finally:
            weather.get = old
        for s in "ABCDEFGH":
            self.assertEqual(sum(1 for r in rows if r["icaoId"] == s), 72)
        self.assertGreater(len(calls), 1)

    def test_partial_day_gives_no_extreme(self):
        tz, d = "UTC", date(2026, 10, 31)
        ev = {"station": "X", "date": d, "kind": "lowest", "unit": "C"}
        late_only = {"X": [(ts_local(tz, d, h), 15.0) for h in range(15, 21)]}
        self.assertIsNone(weather.observed_extreme(ev, late_only, tz))
        whole = {"X": [(ts_local(tz, d, 0, 20), 9.0)] + late_only["X"]}
        self.assertEqual(weather.observed_extreme(ev, whole, tz), 9)


class StaleStation(unittest.TestCase):
    def _event(self, d):
        return {"station": "X", "date": d, "kind": "highest", "unit": "C", "slug": "s",
                "buckets": [{"label": "5°C", "lo": 5, "hi": 5, "yes_bid": 0.05, "yes_ask": 0.06,
                             "yes": "y", "no": "n", "condition": "c", "question": "q"}]}

    def _run(self, last_hour_ago):
        now = datetime.now(timezone.utc)
        d = now.date()
        start = int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp())
        last = int((now - timedelta(hours=last_hour_ago)).timestamp())
        if last < start + 60:
            self.skipTest("too close to UTC midnight for this test")
        obs = {"X": [(start + 60, 10.0), (last, 12.0)]}
        fc = {"X": {"tz": "UTC", "days": {}}}
        return weather.dead_signals([self._event(d)], obs, fc)

    def test_quiet_station_is_skipped(self):
        self.assertEqual(self._run(weather.MAX_OBS_AGE_H + 1), [])

    def test_fresh_station_still_trades(self):
        self.assertEqual(len(self._run(0.5)), 1)

    def test_age(self):
        now = datetime(2026, 10, 31, 12, tzinfo=timezone.utc)
        obs = {"X": [(int(now.timestamp()) - 7200, 1.0)]}
        self.assertAlmostEqual(weather.obs_age_hours("X", obs, now), 2.0)
        self.assertIsNone(weather.obs_age_hours("Y", obs, now))
        self.assertFalse(weather.station_fresh("Y", obs, now))


class EnsembleShadow(unittest.TestCase):
    BUCKETS = [{"label": "60°F or below", "lo": None, "hi": 60},
               {"label": "61-62°F", "lo": 61, "hi": 62},
               {"label": "63°F or higher", "lo": 63, "hi": None}]

    def test_members_combine_readings_so_far_with_remaining_hours(self):
        d = date(2026, 10, 31)
        times = [f"2026-10-31T{h:02d}:00" for h in range(24)] + ["2026-11-01T00:00"]
        member = [10.0] * 25
        member[14] = 20.0        # afternoon peak, still ahead
        member[8] = 30.0         # morning hour already past: ignored
        member[24] = 40.0        # tomorrow: ignored
        now = datetime(2026, 10, 31, 10, 30)
        self.assertEqual(wx_ensemble.member_extremes(times, [member], d, now, "highest", 18.0),
                         [20.0])
        self.assertEqual(wx_ensemble.member_extremes(times, [member], d, now, "highest", 25.0),
                         [25.0])
        self.assertEqual(wx_ensemble.member_extremes(times, [member], d, now, "lowest", 12.0),
                         [10.0])

    def test_bucket_probs_use_market_rounding(self):
        # 16.1C = 60.98F -> 61 ; 16.95C = 62.51F -> 63 (half up) ; 15C = 59F
        p = wx_ensemble.bucket_probs([16.1, 16.95, 15.0, 15.0], self.BUCKETS, "F")
        self.assertEqual(p, {"61-62°F": 0.25, "63°F or higher": 0.25, "60°F or below": 0.5})

    def test_values_outside_listed_buckets_are_counted_separately(self):
        p = wx_ensemble.bucket_probs([10.0, 5.0], [{"label": "10°C", "lo": 10, "hi": 10}], "C")
        self.assertEqual(p, {"10°C": 0.5, None: 0.5})

    def test_each_checkpoint_logged_once(self):
        now = datetime(2026, 10, 31, 1, 30, tzinfo=timezone.utc)      # 10:30 Tokyo
        ev = {"station": "RJTT", "date": date(2026, 10, 31), "kind": "highest", "slug": "e"}
        tzs = {"RJTT": "Asia/Tokyo"}
        self.assertEqual(len(wx_ensemble.due_checkpoints([ev], tzs, set(), now)), 1)
        self.assertEqual(wx_ensemble.due_checkpoints([ev], tzs, {("e", 10)}, now), [])
        other_day = dict(ev, date=date(2026, 11, 1))
        self.assertEqual(wx_ensemble.due_checkpoints([other_day], tzs, set(), now), [])
        self.assertEqual(wx_ensemble.due_checkpoints([ev], {}, set(), now), [])

    def test_split_members_by_model(self):
        hourly = {"time": ["t"], "temperature_2m_ecmwf_ifs025_ensemble": [1],
                  "temperature_2m_member01_ecmwf_ifs025_ensemble": [2],
                  "temperature_2m_ncep_gefs025": [3], "temperature_2m_member01_ncep_gefs025": [4]}
        got = wx_ensemble.split_members(hourly)
        self.assertEqual(sorted(map(tuple, got["ecmwf"])), [(1,), (2,)])
        self.assertEqual(sorted(map(tuple, got["gefs"])), [(3,), (4,)])


class EnsembleEval(unittest.TestCase):
    def test_brier_and_unlisted_winner(self):
        self.assertAlmostEqual(wx_ensemble_eval.brier([1.0, 0.0], 0), 0.0)
        self.assertAlmostEqual(wx_ensemble_eval.brier([0.5, 0.5], 0), 0.5)
        self.assertAlmostEqual(wx_ensemble_eval.brier([0.5, 0.3], None, 0.2), 0.25 + 0.09 + 0.64)

    def test_lone_offer_is_not_a_favourite(self):
        m = wx_ensemble_eval.market_probs([{"bid": None, "ask": 0.9}, {"bid": 0.8, "ask": 0.85}])
        self.assertLess(m[0], m[1])

    def test_hypothetical_pnl_includes_fee(self):
        rec = {"buckets": [{"p": 0.7, "ask": 0.5}, {"p": 0.3, "ask": 0.4}]}
        won = wx_ensemble_eval.hypo_bets(rec, 0, 0.08)
        self.assertEqual(len(won), 1)
        self.assertAlmostEqual(won[0][1], 2.0 - 1.0 - 2.0 * 0.05 * 0.25)
        lost = wx_ensemble_eval.hypo_bets(rec, 1, 0.08)
        self.assertAlmostEqual(lost[0][1], -1.0 - 0.025)

    def test_winner_index(self):
        rec = {"buckets": [{"yes": "a"}, {"yes": "b"}]}
        self.assertEqual(wx_ensemble_eval.winner_index(rec, {"a": 0.0, "b": 1.0}), 1)
        self.assertIsNone(wx_ensemble_eval.winner_index(rec, {"a": 0.0, "b": 0.0}))
        self.assertEqual(wx_ensemble_eval.winner_index(rec, {"a": 0.0}), "pending")
