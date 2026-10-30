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
