"""Tests for the 3 Nov batch: shadow evidence, the automatic strategy switch,
the book watcher, the rewards tracker, the Data API v2 migration and the
arbitrage scanner's paging. No network: every call is faked.

Run:  python -m unittest discover -s tests -v
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
sys.argv = ["signals.py", "SPORTS"]
import signals                                 # noqa: E402
sys.argv = _argv
import rewards_tracker                         # noqa: E402
import scanner                                 # noqa: E402
import shadow                                  # noqa: E402
import simcore                                 # noqa: E402
import watch_books                             # noqa: E402
import weather                                 # noqa: E402


def iso(minutes):
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()


class InTempDir(unittest.TestCase):
    def setUp(self):
        self._old = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory()
        os.chdir(self._tmp.name)

    def tearDown(self):
        os.chdir(self._old)
        self._tmp.cleanup()


def payouts_get(pays):
    """Fake HTTP helper for simcore.final_payouts: /v2/tokens with these payouts."""
    def get(url, params=None):
        if url == simcore.V2_TOKENS:
            ids = params["token_id"].split(",")
            return {"data": [{"token_id": t, "resolved": True, "final_price": pays[t]}
                             for t in ids if t in pays]}
        return []
    return get


KW = dict(condition="c", title="t")


class ShadowBook(InTempDir):
    def test_one_record_per_strategy_kind_asset(self):
        b = shadow.Book("shadow_x.json")
        self.assertIsNotNone(b.add_taker("S", "cash", asset="A", price=0.5, fee_rate=0.05, **KW))
        self.assertIsNone(b.add_taker("S", "per_event", asset="A", price=0.6, fee_rate=0.05, **KW))
        self.assertIsNotNone(b.add_taker("T", "cash", asset="A", price=0.5, fee_rate=0.05, **KW))
        self.assertEqual(len(b.bets), 2)

    def test_taker_fee_and_shares(self):
        b = shadow.Book("shadow_x.json")
        r = b.add_taker("S", "cash", asset="A", price=0.8, fee_rate=0.05, **KW)
        self.assertAlmostEqual(r["shares"], 1.25)
        self.assertAlmostEqual(r["fee"], 1.25 * 0.05 * 0.8 * 0.2)

    def test_limit_rests_then_fills_on_a_cheap_ask(self):
        b = shadow.Book("shadow_x.json")
        r = b.add_limit("S", asset="A", limit_price=0.55, best_ask=0.60, fee_rate=0.05, **KW)
        self.assertEqual((r["status"], r["fee"]), ("resting", 0.0))
        self.assertEqual(b.check_limits({"A": 0.56}), 0)
        self.assertEqual(b.check_limits({"A": 0.55}), 1)
        self.assertEqual(r["status"], "open")
        self.assertIn("fill", r)

    def test_limit_that_crosses_at_once_pays_the_ask_and_fee(self):
        b = shadow.Book("shadow_x.json")
        r = b.add_limit("S", asset="A", limit_price=0.60, best_ask=0.58, fee_rate=0.05, **KW)
        self.assertEqual(r["status"], "open")
        self.assertAlmostEqual(r["price"], 0.58)
        self.assertGreater(r["fee"], 0)

    def test_limit_expires_before_it_can_fill(self):
        b = shadow.Book("shadow_x.json")
        r = b.add_limit("S", asset="A", limit_price=0.55, best_ask=0.60, fee_rate=0.05,
                        ends=iso(-1), **KW)
        b.check_limits({"A": 0.50})
        self.assertEqual(r["status"], "expired")

    def test_settles_on_official_payout(self):
        b = shadow.Book("shadow_x.json")
        b.add_taker("S", "cash", asset="A", price=0.5, fee_rate=0.0, **KW)
        b.add_taker("S", "cash", asset="B", price=0.5, fee_rate=0.0, **KW)
        b.add_limit("S", asset="C", limit_price=0.4, best_ask=0.5, fee_rate=0.0, **KW)
        self.assertEqual(b.settle(payouts_get({"A": 1.0, "B": 0.0, "C": 1.0})), 2)
        got = {x["asset"]: (x["status"], round(x.get("pnl", 0), 6)) for x in b.bets}
        self.assertEqual(got["A"], ("won", 1.0))
        self.assertEqual(got["B"], ("lost", -1.0))
        self.assertEqual(got["C"][0], "resting")      # never filled: never settles

    def test_round_trip_and_broken_file_is_set_aside(self):
        b = shadow.Book("shadow_x.json")
        b.add_taker("S", "cash", asset="A", price=0.5, fee_rate=0.05, **KW)
        b.save()
        self.assertEqual(len(shadow.Book("shadow_x.json").bets), 1)
        with open("shadow_x.json", "w") as fh:
            fh.write('{"bets": [{"kind": "nope"}]}')
        self.assertEqual(shadow.Book("shadow_x.json").bets, [])
        self.assertTrue(os.path.exists("shadow_x.json.bad"))

    def test_rejects_non_finite_numbers(self):
        b = shadow.Book("shadow_x.json")
        self.assertIsNone(b.add("S", "skipped", "x", asset="A", price=float("nan"),
                                shares=1, stake=1, fee=0, **KW))


def settled(n, pnl, opened="2026-11-01T00:00:00+00:00", stake=1.0):
    return [{"status": "won" if pnl > 0 else "lost", "pnl": pnl, "stake": stake, "fee": 0.0,
             "opened": opened, "closed": f"2026-11-02T00:{i % 60:02d}:00+00:00"}
            for i in range(n)]


def mixed(n_win, win_pnl, n_loss, loss_pnl, **kw):
    return settled(n_win, win_pnl, **kw) + settled(n_loss, loss_pnl, **kw)


class StrategySwitch(unittest.TestCase):
    def test_too_few_bets_never_pauses(self):
        led = {"cash": 50, "bets": settled(shadow.SWITCH_MIN_N - 1, -1.0)}
        self.assertIsNone(shadow.switch_update(led, [], "S"))
        self.assertFalse(shadow.is_paused(led))

    def test_confident_loser_is_paused(self):
        led = {"cash": 50, "bets": mixed(10, 0.2, 30, -1.0)}
        self.assertIn("Auto-paused", shadow.switch_update(led, [], "S"))
        self.assertTrue(shadow.is_paused(led))

    def test_noisy_small_loss_is_not_paused(self):
        # like AFTERNOON today: many wins, a few big losses, ROI slightly negative
        led = {"cash": 50, "bets": mixed(72, 0.15, 12, -1.0)}
        self.assertIsNone(shadow.switch_update(led, [], "S"))

    def test_bets_before_judge_from_are_ignored(self):
        led = {"cash": 50, "bets": mixed(10, 0.2, 30, -1.0, opened="2026-10-29T00:00:00+00:00")}
        self.assertIsNone(shadow.switch_update(led, [], "S", judge_from="2026-10-31T00:00:00"))

    def _paused(self):
        return {"cash": 50, "bets": [], "auto": {"paused": True, "since": "2026-11-01T00:00:00+00:00"}}

    def _sh(self, n_win, n_loss, created="2026-11-02T00:00:00+00:00", reason="auto_paused"):
        out = []
        for i in range(n_win + n_loss):
            out.append({"strategy": "S", "kind": "skipped", "reason": reason, "created": created,
                        "status": "won" if i < n_win else "lost", "stake": 1.0, "fee": 0.0,
                        "pnl": 0.3 if i < n_win else -1.0})
        return out

    def test_resumes_when_shadow_results_are_convincing(self):
        led = self._paused()
        msg = shadow.switch_update(led, self._sh(38, 2), "S")
        self.assertIn("Auto-resumed", msg)
        self.assertFalse(shadow.is_paused(led))
        self.assertEqual(len(led["auto"]["history"]), 1)

    def test_stays_paused_on_weak_or_old_or_other_evidence(self):
        for sh in (self._sh(28, 8),                                       # not convincing
                   self._sh(38, 2, created="2026-10-30T00:00:00+00:00"),  # from before the pause
                   self._sh(38, 2, reason="cash")):                       # not a paused signal
            led = self._paused()
            self.assertIsNone(shadow.switch_update(led, sh, "S"))
            self.assertTrue(shadow.is_paused(led))


def sig(n, traders=("A",), event=None):
    return {"asset": f"N{n}", "condition": f"cN{n}", "event": event or f"eN{n}", "stake": 1,
            "fee": 0.01, "price": 0.6, "shares": 1.6, "order": "market", "traders": list(traders),
            "holders": {}, "hours": 5, "title": "t", "outcome": "o", "avg_entry": 0.6,
            "profit_if_win": 0.5, "breakeven": 0.6}


class SignalsShadow(InTempDir):
    def test_skips_are_recorded_with_their_reason(self):
        led = {"cash": 50, "bets": [{"status": "open", "condition": f"c{i}", "event": f"e{i}",
                                     "asset": f"a{i}", "traders": ["A"]}
                                    for i in range(signals.MAX_OPEN_PER_TRADER)]}
        led["bets"].append({"status": "open", "condition": "cX", "event": "eBusy",
                            "asset": "aX", "traders": ["Z"]})
        b = shadow.Book("s.json")
        signals.open_paper_bets(led, [sig(1, ["A"]), sig(2, ["B"], event="eBusy"), sig(3, ["B"])], b)
        why = {x["asset"]: x["reason"] for x in b.bets}
        self.assertEqual(why, {"N1": "per_trader", "N2": "per_event"})
        self.assertEqual(led["bets"][-1]["asset"], "N3")           # the real bet still happens

    def test_cash_skip(self):
        led = {"cash": 0.5, "bets": []}
        b = shadow.Book("s.json")
        signals.open_paper_bets(led, [sig(1)], b)
        self.assertEqual([x["reason"] for x in b.bets], ["cash"])

    def test_auto_paused_bets_nothing_but_records_everything(self):
        led = {"cash": 50, "bets": [], "auto": {"paused": True, "since": iso(-60)}}
        b = shadow.Book("s.json")
        signals.open_paper_bets(led, [sig(1), sig(2)], b)
        self.assertEqual(led["bets"], [])
        self.assertEqual({x["reason"] for x in b.bets}, {"auto_paused"})
        self.assertEqual(len(b.bets), 2)


class CandidateShadows(InTempDir):
    """Regression (3 Nov, two failed runs): add_limit crashed with "got multiple
    values for keyword argument 'extra'" as soon as a real candidate existed."""

    def _cand(self, n, avg_entry, ask, reject=None):
        c = {"asset": f"A{n}", "condition": f"c{n}", "title": "t", "event": f"e{n}",
             "outcome": "Yes", "traders": ["X"], "avg_entry": avg_entry, "hours": 5,
             "ask_seen": ask}
        if reject:
            c["reject"] = reject
        return c

    def test_limit_and_chase_records_from_real_candidates(self):
        b = shadow.Book("s.json")
        cands = [self._cand(1, 0.60, 0.65),                    # rests at 0.60
                 self._cand(2, 0.60, 0.59),                    # crosses at once
                 self._cand(3, 0.50, 0.75, reject="chase"),    # chased: limit + taker
                 {"asset": "A4", "ask_seen": None}]            # no book: ignored
        filled = signals.record_shadow_candidates(b, cands, fetch=lambda t: {})
        got = sorted((x["asset"], x["kind"], x["status"]) for x in b.bets)
        self.assertEqual(got, [("A1", "limit", "resting"), ("A2", "limit", "open"),
                               ("A3", "limit", "resting"), ("A3", "skipped", "open")])
        self.assertEqual(filled, 0)
        lim = next(x for x in b.bets if x["asset"] == "A1")
        self.assertEqual((lim["limit"], lim["avg_entry"], lim["traders"]), (0.60, 0.60, ["X"]))

    def test_resting_limit_fills_from_a_later_run(self):
        b = shadow.Book("s.json")
        signals.record_shadow_candidates(b, [self._cand(1, 0.60, 0.65)], fetch=lambda t: {})
        book = {"A1": {"asks": [{"price": "0.60", "size": "50"}], "bids": []}}
        self.assertEqual(signals.record_shadow_candidates(b, [], fetch=lambda t: book), 1)
        self.assertEqual(b.bets[0]["status"], "open")

    def test_add_limit_keeps_caller_extra(self):
        b = shadow.Book("s.json")
        r = b.add_limit("S", asset="A", limit_price=0.5, best_ask=0.6, fee_rate=0.05,
                        extra={"note": "x"}, **KW)
        self.assertEqual((r["note"], r["limit"]), ("x", 0.5))


class WeatherShadow(InTempDir):
    def test_expired_resting_bid_becomes_unfilled_record(self):
        led = {"cash": 49.0, "bets": [{"strategy": "MORNING", "status": "resting", "asset": "A",
                                       "condition": "c", "event": "e", "title": "t",
                                       "outcome": "YES", "bucket": "5°C", "price": 0.8,
                                       "shares": 1.25, "stake": 1.0, "expires": iso(-1)}]}
        b = shadow.Book("s.json")
        weather.check_resting(led, {}, set(), b)
        self.assertEqual(led["bets"][0]["status"], "cancelled")
        self.assertEqual([(x["kind"], x["status"], x["price"]) for x in b.bets],
                         [("unfilled", "open", 0.8)])

    def test_auto_paused_weather_strategy_only_records(self):
        led = {"cash": 50, "bets": [], "auto": {"paused": True, "since": iso(-60)}}
        e = {"slug": "ev"}
        bk = {"label": "5°C", "yes": "Y", "no": "N", "condition": "c", "question": "q"}
        fake = {"strategy": "DEAD", "asset": "N", "condition": "c", "event": "ev", "title": "q",
                "price": 0.95, "shares": 1.05, "stake": 1.0, "fee": 0.002}
        old = weather.make_bet
        weather.make_bet = lambda *a: dict(fake)
        try:
            b = shadow.Book("s.json")
            weather.open_bets(led, [("DEAD", e, bk, "NO", "why")], b)
        finally:
            weather.make_bet = old
        self.assertEqual(led["bets"], [])
        self.assertEqual([(x["strategy"], x["reason"]) for x in b.bets], [("DEAD", "auto_paused")])


class DataApiV2(unittest.TestCase):
    """Polymarket retired Data API v1 on 3 Nov 2026 (it answers 410)."""

    def _fake(self, pages):
        calls = []

        def get(url, params=None, raw=False):
            calls.append((url, dict(params or {})))
            return pages[params.get("cursor")]
        return get, calls

    def test_follows_the_cursor_and_stops_early(self):
        pages = {None: {"data": [{"i": 1}, {"i": 2}], "pagination": {"next_cursor": "c2"}},
                 "c2": {"data": [{"i": 3}, {"i": 4}], "pagination": {"next_cursor": None}}}
        get, calls = self._fake(pages)
        old = signals.get
        signals.get = get
        try:
            self.assertEqual([r["i"] for r in signals.data_v2("positions", {})], [1, 2, 3, 4])
            self.assertEqual([r["i"] for r in signals.data_v2("positions", {},
                                                              stop=lambda r: r["i"] == 3)],
                             [1, 2])
            self.assertTrue(all("/v2/" in u and "offset" not in p for u, p in calls))
        finally:
            signals.get = old

    def test_api_failure_is_none_not_empty(self):
        old = signals.get
        signals.get = lambda *a, **k: None
        try:
            self.assertIsNone(signals.data_v2("positions", {}))
            self.assertIsNone(signals.trader_history("0xabc"))
        finally:
            signals.get = old

    def test_field_mapping(self):
        r = signals.v1_position({"token_id": "T", "condition_id": "C", "current_size": 5,
                                 "avg_price": 0.4, "current_price": 0.5, "event_slug": "ev",
                                 "end_date": "2026-11-05", "redeemable": False})
        self.assertEqual((r["asset"], r["conditionId"], r["size"], r["avgPrice"], r["curPrice"],
                          r["eventSlug"], r["endDate"]),
                         ("T", "C", 5, 0.4, 0.5, "ev", "2026-11-05"))
        self.assertEqual(signals.v1_leader({"user_id": "0x1", "user_name": "n", "pnl": 3,
                                            "volume": 9}),
                         {"proxyWallet": "0x1", "userName": "n", "pnl": 3, "vol": 9})

    def test_trader_history_counts_closed_and_unredeemed(self):
        now = datetime.now(timezone.utc)
        recent, old = int(now.timestamp()) - 86400, int((now - timedelta(days=60)).timestamp())
        closed = {"data": [{"last_event_at": recent, "realized_pnl": 5, "total_size": 10,
                            "avg_price": 0.5, "event_slug": "a"},
                           {"last_event_at": old, "realized_pnl": 9, "total_size": 10,
                            "avg_price": 0.5, "event_slug": "old"}], "pagination": {}}
        redeem = {"data": [{"end_date": now.date().isoformat(), "current_value": 0,
                            "total_cost_usdc": 4, "event_slug": "b"}], "pagination": {}}

        def get(url, params=None, raw=False):
            return closed if params.get("status") == "CLOSED" else redeem
        old_get = signals.get
        signals.get = get
        try:
            self.assertEqual(signals.trader_history("0x1"), [("a", 5.0, 5.0), ("b", -4.0, 4.0)])
        finally:
            signals.get = old_get


class Watcher(InTempDir):
    def test_budget_from_run_start(self):
        self.assertEqual(watch_books.budget_seconds(1000, {"RUN_STARTED": "1000",
                                                          "WATCH_UNTIL_MIN": "11"}), 660)
        self.assertEqual(watch_books.budget_seconds(5000, {"RUN_STARTED": "1000"}), 0)
        self.assertEqual(watch_books.budget_seconds(1000, {}), 0)

    def test_fills_real_and_shadow_bids_and_tracks_quote_extremes(self):
        led = {"cash": 49.0, "bets": [{"strategy": "MORNING", "status": "resting", "asset": "W",
                                       "condition": "c", "event": "e", "title": "t",
                                       "outcome": "YES", "bucket": "5°C", "price": 0.80,
                                       "shares": 1.25, "stake": 1.0, "fee": 0.0,
                                       "expires": iso(60)}]}
        with open("ledger_wx_morning.json", "w") as fh:
            json.dump(led, fh)
        b = shadow.Book("shadow_sports.json")
        b.add_limit("SPORTS", asset="L", limit_price=0.5, best_ask=0.6, fee_rate=0.05, **KW)
        b.save()
        with open("rewards_state.json", "w") as fh:
            json.dump({"quotes": {"R": {}}, "watch": {}}, fh)
        looks = iter([{"W": 0.85, "L": 0.55, "R": (0.40, 0.45)},
                      {"W": 0.80, "L": 0.50, "R": (0.38, 0.44)}])

        def fetch(tokens):
            snap = next(looks)
            out = {}
            for t in tokens:
                v = snap.get(t)
                if isinstance(v, tuple):
                    out[t] = {"asks": [{"price": str(v[1]), "size": "9"}],
                              "bids": [{"price": str(v[0]), "size": "9"}]}
                elif v is not None:
                    out[t] = {"asks": [{"price": str(v), "size": "9"}], "bids": []}
            return out
        old = weather.notify
        weather.notify = lambda lines: True
        try:
            watch_books.main(seconds=watch_books.POLL_SECONDS + 1, sleep=lambda s: None,
                             fetch=fetch)
        finally:
            weather.notify = old
        with open("ledger_wx_morning.json") as fh:
            self.assertEqual(json.load(fh)["bets"][0]["status"], "open")
        self.assertEqual(shadow.Book("shadow_sports.json").bets[0]["status"], "open")
        with open("rewards_state.json") as fh:
            w = json.load(fh)["watch"]["R"]
        self.assertEqual((w["min_ask"], w["max_bid"]), (0.44, 0.40))

    def test_no_time_left_does_nothing(self):
        self.assertEqual(watch_books.main(seconds=5), 0)


class RewardsTracker(unittest.TestCase):
    Q = {"bid": 0.40, "ask": 0.44, "size": 20, "usd_day": [1.0, 2.0]}

    def test_fills(self):
        self.assertEqual(rewards_tracker.fills(self.Q, 0.45, 0.39, 0.42), (False, False, 0.0))
        hit = rewards_tracker.fills(self.Q, 0.40, 0.39, 0.35)        # sold to us at 0.40, now 0.35
        self.assertEqual(hit[:2], (True, False))
        self.assertAlmostEqual(hit[2], 20 * (0.35 - 0.40))
        both = rewards_tracker.fills(self.Q, 0.39, 0.45, 0.42)       # round trip earns the spread
        self.assertAlmostEqual(both[2], 20 * 0.04)

    def test_accrual_only_for_short_gaps(self):
        self.assertEqual(rewards_tracker.accrual(self.Q, 0.25), (1.0 / 96, 2.0 / 96))
        self.assertEqual(rewards_tracker.accrual(self.Q, 5), (0.0, 0.0))

    def test_report_totals(self):
        lines = [{"condition": "c", "question": "q", "hours": 0.25, "accrued": [0.01, 0.02],
                  "bid_hit": True, "ask_hit": False, "fill_pnl": -0.5, "capital": 19.0}] * 4
        txt = rewards_tracker.report(lines)
        self.assertIn("hits 4", txt)
        self.assertIn("fill P&L $-2.00", txt)


class ScannerPaging(unittest.TestCase):
    def test_keyset_cursor_and_open_ended_events_skipped(self):
        pages = [{"events": [{"id": 1}], "next_cursor": "x"}, {"events": [{"id": 2}]}]
        seen = []

        class R:
            status_code = 200

            def __init__(self, body):
                self.body = body

            def json(self):
                return self.body

        def fake_get(url, params=None, timeout=None):
            seen.append(dict(params))
            return R(pages[len(seen) - 1])
        old_get, old_sleep = scanner.requests.get, scanner.time.sleep
        scanner.requests.get, scanner.time.sleep = fake_get, lambda s: None
        try:
            self.assertEqual([e["id"] for e in scanner.fetch_events()], [1, 2])
        finally:
            scanner.requests.get, scanner.time.sleep = old_get, old_sleep
        self.assertNotIn("offset", seen[0])
        self.assertEqual(seen[1].get("after_cursor"), "x")
        mk = [{"active": True, "closed": False, "bestAsk": "0.3", "bestBid": "0.2"}] * 3
        old = scanner.fetch_events
        scanner.fetch_events = lambda: iter([
            {"negRisk": True, "negRiskAugmented": True, "markets": mk, "title": "open-ended"},
            {"negRisk": True, "markets": mk, "title": "closed set"}])
        try:
            self.assertEqual([h[1] for h in scanner.scan()], ["closed set"])
        finally:
            scanner.fetch_events = old


class WidePool(InTempDir):
    """Shadow-only test of a looser trader bar (17 Nov-style experiment)."""

    def _score(self, win=0.6, roi=0.2, n=40, name="T"):
        return {"name": name, "win_rate": win, "roi": roi, "n": n}

    def test_bands_name_the_first_rule_missed(self):
        W = signals.wide_band
        self.assertIsNone(W(self._score()))                            # passes the strict bar
        self.assertIsNone(W(self._score(roi=-0.05)))                   # not profitable
        self.assertIsNone(W(self._score(roi=0.0)))
        self.assertIsNone(W(self._score(roi=0.2, n=10)))               # too little history
        self.assertEqual(W(self._score(roi=0.05)), "wide_low_roi")
        self.assertEqual(W(self._score(win=0.4, roi=0.05)), "wide_low_roi")   # ROI named first
        self.assertEqual(W(self._score(n=20)), "wide_few_bets")
        self.assertEqual(W(self._score(win=0.45)), "wide_low_win")

    def test_pool_holds_only_the_traders_the_strict_bar_leaves_out(self):
        scores = {"strict": self._score(), "loss": self._score(roi=-0.1),
                  "short": self._score(n=20), "low": self._score(roi=0.04)}
        pool = signals.wide_pool(scores)
        self.assertEqual({w: s["band"] for w, s in pool.items()},
                         {"short": "wide_few_bets", "low": "wide_low_roi"})
        self.assertNotIn("band", scores["short"])                      # input untouched
        self.assertEqual(list(signals.good_traders(scores)), ["strict"])

    def _cand(self, n, band="wide_low_roi", roi=0.05, event=None, cond=None):
        h = {"w": {"name": "T", "size": 100, "entry": 0.6, "win_rate": 0.5, "roi": roi,
                   "n": 20, "band": band}}
        return {"asset": f"A{n}", "condition": cond or f"c{n}", "title": "t",
                "event": event or f"e{n}", "outcome": "Yes", "traders": ["T"],
                "holders": h, "avg_entry": 0.6, "price": 0.6, "hours": 5}

    @staticmethod
    def _size(c):
        return dict(c, shares=1.6, stake=1.0, fee=0.01)

    def _run(self, book, cands, strict=(), ledger=None, scores=None):
        scores = scores or {"w": self._score(roi=0.05)}
        return signals.record_wide_pool(book, scores, list(strict), ledger or {"bets": []},
                                        find=lambda wide: list(cands), size=self._size)

    def test_records_a_shadow_bet_with_the_band_and_scores(self):
        b = shadow.Book("s.json")
        self.assertEqual(self._run(b, [self._cand(1, band="wide_few_bets")]), 1)
        r = b.bets[0]
        self.assertEqual((r["kind"], r["reason"], r["status"], r["strategy"]),
                         ("skipped", "wide_few_bets", "open", signals.SCAN))
        self.assertEqual(r["wide"][0]["band"], "wide_few_bets")
        self.assertEqual((r["price"], r["stake"]), (0.6, 1.0))

    def test_only_what_the_looser_bar_adds(self):
        b = shadow.Book("s.json")
        strict = [{"asset": "A1", "condition": "c1"}]                  # strict pool has it
        ledger = {"bets": [{"asset": "OTHER", "condition": "c2"}]}     # we hold that market
        cands = [self._cand(1), self._cand(2), self._cand(3), self._cand(4, cond="c1")]
        self.assertEqual(self._run(b, cands, strict, ledger), 1)
        self.assertEqual([x["asset"] for x in b.bets], ["A3"])

    def test_one_record_per_event_even_across_runs(self):
        b = shadow.Book("s.json")
        self.assertEqual(self._run(b, [self._cand(1, event="m"), self._cand(2, event="m")]), 1)
        self.assertEqual(self._run(b, [self._cand(3, event="m"), self._cand(4)]), 1)
        self.assertEqual(sorted(x["asset"] for x in b.bets), ["A1", "A4"])

    def test_same_signal_every_run_is_one_record(self):
        b = shadow.Book("s.json")
        self._run(b, [self._cand(1)])
        self.assertEqual(self._run(b, [self._cand(1)]), 0)
        self.assertEqual(len(b.bets), 1)

    def test_per_run_cap(self):
        b = shadow.Book("s.json")
        n = signals.WIDE_MAX_PER_RUN + 5
        self.assertEqual(self._run(b, [self._cand(i) for i in range(n)]),
                         signals.WIDE_MAX_PER_RUN)

    def test_unsizable_signals_are_dropped(self):
        b = shadow.Book("s.json")
        out = signals.record_wide_pool(b, {"w": self._score(roi=0.05)}, [], {"bets": []},
                                       find=lambda w: [self._cand(1)], size=lambda c: None)
        self.assertEqual((out, b.bets), (0, []))

    def test_a_failure_never_stops_the_bot(self):
        b = shadow.Book("s.json")

        def boom(wide):
            raise RuntimeError("api down")
        out = signals.record_wide_pool(b, {"w": self._score(roi=0.05)}, [], {"bets": []},
                                       find=boom, size=self._size)
        self.assertEqual((out, b.bets), (0, []))

    def test_no_wide_traders_means_no_work(self):
        b = shadow.Book("s.json")

        def never(wide):
            raise AssertionError("must not look")
        self.assertEqual(signals.record_wide_pool(b, {"s": self._score()}, [], {"bets": []},
                                                  find=never), 0)

    def test_wide_records_do_not_touch_the_strategy_switch(self):
        b = shadow.Book("s.json")
        self._run(b, [self._cand(i) for i in range(3)])
        led = {"bets": [], "cash": 50, "auto": {"paused": True, "since": iso(-60)}}
        self.assertIsNone(shadow.switch_update(led, b.bets, signals.SCAN))

    def test_holders_carry_the_band_only_for_wide_traders(self):
        rows = [{"redeemable": False, "curPrice": 0.6, "size": 200, "endDate": iso(300),
                 "eventSlug": "e", "asset": "A", "conditionId": "c", "title": "t",
                 "outcome": "Yes", "avgPrice": 0.58}]
        old = (signals.positions, signals.warm_tags, signals.in_scan)
        signals.positions = lambda w, **kw: rows
        signals.warm_tags = lambda slugs: None
        signals.in_scan = lambda slug: True
        try:
            wide = signals.find_candidates({"w": dict(self._score(roi=0.05), band="wide_low_roi")})
            strict = signals.find_candidates({"w": self._score()})
        finally:
            signals.positions, signals.warm_tags, signals.in_scan = old
        self.assertEqual(wide[0]["holders"]["w"]["band"], "wide_low_roi")
        self.assertNotIn("band", strict[0]["holders"]["w"])            # real ledger format unchanged

    def test_report_shows_the_wide_pool_next_to_the_real_ledger(self):
        import io
        import contextlib
        import shadow_report
        b = shadow.Book("shadow_sports.json")
        for i, (band, st, pnl) in enumerate([("wide_low_roi", "won", 0.5),
                                              ("wide_low_roi", "lost", -1.0),
                                              ("wide_low_win", "won", 0.4)]):
            r = b.add("SPORTS", "skipped", band, asset=f"A{i}", price=0.6, shares=1.6,
                      stake=1.0, fee=0.0, **KW)
            r.update(status=st, pnl=pnl)
        b.save()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            shadow_report.main()
        out = buf.getvalue()
        self.assertIn("wide pool: ROI under 10%", out)
        self.assertIn("wide pool: win rate under 58%", out)
        self.assertIn("wide pool: all bands", out)


if __name__ == "__main__":
    unittest.main()
