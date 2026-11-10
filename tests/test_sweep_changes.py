"""Tests for the 10 Nov sweep changes: the watchlist, the shadow-only new
trader bar, and the cheap-bucket weather shadow. No network: every call is faked.

Run:  python -m unittest discover -s tests -v
"""
import contextlib
import io
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
import shadow                                  # noqa: E402


def iso(minutes):
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()


class Patch:
    """Set module attributes for one test and put them back afterwards."""

    def __init__(self, mod, **values):
        self.mod, self.values, self.saved = mod, values, {}

    def __enter__(self):
        for k, v in self.values.items():
            self.saved[k] = getattr(self.mod, k)
            setattr(self.mod, k, v)

    def __exit__(self, *exc):
        for k, v in self.saved.items():
            setattr(self.mod, k, v)


class InTempDir(unittest.TestCase):
    def setUp(self):
        self._old = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory()
        os.chdir(self._tmp.name)

    def tearDown(self):
        os.chdir(self._old)
        self._tmp.cleanup()


def score(win=0.6, roi=0.2, n=40, name="T", **extra):
    return dict({"name": name, "win_rate": win, "roi": roi, "n": n}, **extra)


class Watchlist(unittest.TestCase):
    def _leaders(self, rows):
        return lambda path, params, max_pages=10, stop=None: rows

    def test_watchlisted_traders_join_the_leaderboard_pool(self):
        rows = [{"user_id": "0xaaa", "user_name": "Leader", "pnl": 50, "volume": 100}]
        with Patch(signals, SCAN="ESPORTS", data_v2=self._leaders(rows),
                   WATCHLIST={"ESPORTS": {"0xBBB": "Watched"}, "WEATHER": {"0xccc": "W"}}):
            pool = signals.top_traders()
        self.assertEqual(pool, {"0xaaa": "Leader", "0xBBB": "Watched"})   # other scans' list ignored

    def test_no_duplicate_when_already_on_the_leaderboard(self):
        rows = [{"user_id": "0xbbb", "user_name": "Leader", "pnl": 50, "volume": 100}]
        with Patch(signals, SCAN="ESPORTS", data_v2=self._leaders(rows),
                   WATCHLIST={"ESPORTS": {"0xBBB": "Watched"}}):
            self.assertEqual(signals.top_traders(), {"0xbbb": "Leader"})

    def test_watchlist_skips_the_market_maker_filter_only_for_listed_wallets(self):
        rows = [{"user_id": "0xmm", "user_name": "MM", "pnl": 1, "volume": 1000}]   # 0.1% of volume
        with Patch(signals, SCAN="ESPORTS", data_v2=self._leaders(rows),
                   WATCHLIST={"ESPORTS": {"0xddd": "Watched"}}):
            self.assertEqual(signals.top_traders(), {"0xddd": "Watched"})

    def test_the_real_list(self):
        for scan, wallets in signals.WATCHLIST.items():
            self.assertIn(scan, signals.CATEGORIES)
            for w in wallets:
                self.assertRegex(w, r"^0x[0-9a-f]{40}$")
        self.assertIn("0x3231d2d67053e5dc3715f6429124741e52866e75", signals.WATCHLIST["ESPORTS"])


class NewBar(unittest.TestCase):
    T0 = 1_780_000_000

    def bets(self, pnls, cost=1.0, timed=True):
        return [("e", p, cost, self.T0 + i * 3600 if timed else None)
                for i, p in enumerate(pnls)]

    def test_steady_winner_passes(self):
        r = signals.alt_bar(self.bets([0.6, -0.2] * 20))            # +20% a bet, steady
        self.assertTrue(r["alt"])
        self.assertGreater(r["roi_lo"], 0)
        self.assertGreater(min(r["roi_h1"], r["roi_h2"]), 0)

    def test_one_big_win_is_not_enough(self):
        r = signals.alt_bar(self.bets([-0.3] * 39 + [30.0]))        # +45% overall, all from one bet
        self.assertFalse(r["alt"])
        self.assertLess(r["roi_lo"], 0)

    def test_tiny_steady_edge_is_not_enough(self):
        r = signals.alt_bar(self.bets([0.01] * 99 + [-0.5]))         # 99% wins, about +0.5%
        self.assertGreater(r["roi_lo"], -0.01)
        self.assertFalse(r["alt"])
        r = signals.alt_bar(self.bets([0.08] * 39 + [-0.5]))         # 97.5% wins, +6.5%
        self.assertTrue(r["alt"])

    def test_must_be_profitable_in_both_halves(self):
        r = signals.alt_bar(self.bets([-0.1] * 20 + [0.9] * 20))     # older half lost
        self.assertLess(r["roi_h1"], 0)
        self.assertFalse(r["alt"])

    def test_needs_enough_bets_and_times(self):
        self.assertFalse(signals.alt_bar(self.bets([0.6, -0.2] * 10))["alt"])         # 20 bets
        r = signals.alt_bar(self.bets([0.6, -0.2] * 20, timed=False))
        self.assertIsNone(r["roi_h1"])
        self.assertFalse(r["alt"])
        r = signals.alt_bar([("e", 1.0, 1.0)] * 40)                                  # old 3-tuples
        self.assertFalse(r["alt"])

    def test_empty_or_costless(self):
        self.assertFalse(signals.alt_bar([])["alt"])
        self.assertFalse(signals.alt_bar([("e", 1.0, 0.0, 1)] * 2)["alt"])

    def test_score_traders_adds_the_flags(self):
        hist = self.bets([0.6, -0.2] * 20)
        with Patch(signals, trader_history=lambda w, with_time=False: hist,
                   warm_tags=lambda slugs: None, in_scan=lambda slug: True):
            s = signals.score_traders({"0x1": "A"})["0x1"]
        self.assertEqual((s["n"], round(s["win_rate"], 2)), (40, 0.5))
        self.assertTrue(s["alt"])
        for k in ("roi_lo", "roi_h1", "roi_h2"):
            self.assertIn(k, s)

    def test_strict_bar_is_unchanged(self):
        self.assertTrue(signals.passes_bar(score(alt=False)))
        self.assertFalse(signals.passes_bar(score(win=0.5, alt=True)))

    def test_history_with_time(self):
        now = datetime.now(timezone.utc)
        ts = int(now.timestamp()) - 86400
        closed = {"data": [{"last_event_at": ts, "realized_pnl": 5, "total_size": 10,
                            "avg_price": 0.5, "event_slug": "a"}], "pagination": {}}
        redeem = {"data": [{"end_date": now.date().isoformat(), "current_value": 0,
                            "total_cost_usdc": 4, "event_slug": "b"}], "pagination": {}}

        def get(url, params=None, raw=False):
            return closed if params.get("status") == "CLOSED" else redeem
        with Patch(signals, get=get):
            plain = signals.trader_history("0x1")
            timed = signals.trader_history("0x1", with_time=True)
        self.assertEqual(plain, [("a", 5.0, 5.0), ("b", -4.0, 4.0)])
        self.assertEqual([x[:3] for x in timed], plain)
        self.assertEqual(timed[0][3], float(ts))
        self.assertIsNotNone(timed[1][3])

    def test_end_ts(self):
        self.assertEqual(signals.end_ts("2026-11-10"),
                         datetime(2026, 11, 10, tzinfo=timezone.utc).timestamp())
        self.assertEqual(signals.end_ts("2026-11-10T12:00:00Z"),
                         datetime(2026, 11, 10, 12, tzinfo=timezone.utc).timestamp())
        self.assertIsNone(signals.end_ts(None))
        self.assertIsNone(signals.end_ts("soon"))


class Candidates(unittest.TestCase):
    def _rows(self, price, value):
        return [{"redeemable": False, "curPrice": price, "size": value / price,
                 "endDate": iso(300), "eventSlug": "e", "asset": "A", "conditionId": "c",
                 "title": "t", "outcome": "Yes", "avgPrice": price}]

    def _find(self, rows, good, **kw):
        with Patch(signals, positions=lambda w, **k: rows, warm_tags=lambda s: None,
                   in_scan=lambda s: True):
            return signals.find_candidates(good, **kw)

    def test_default_band_and_size_unchanged(self):
        self.assertEqual(self._find(self._rows(0.22, 10), {"w": score()}), [])     # cheap, small
        self.assertEqual(len(self._find(self._rows(0.6, 60), {"w": score()})), 1)

    def test_cheap_band_and_small_positions_when_asked(self):
        got = self._find(self._rows(0.22, 10), {"w": score()},
                         min_price=0.15, max_price=0.395, min_usd=5)
        self.assertEqual(len(got), 1)
        self.assertEqual(self._find(self._rows(0.22, 3), {"w": score()},
                                    min_price=0.15, max_price=0.395, min_usd=5), [])

    def test_holders_carry_the_new_bar_flag_when_scored(self):
        got = self._find(self._rows(0.6, 60), {"w": score(alt=True)})
        self.assertIs(got[0]["holders"]["w"]["alt"], True)
        got = self._find(self._rows(0.6, 60), {"w": score()})                     # old cache
        self.assertNotIn("alt", got[0]["holders"]["w"])

    def test_size_bet_honours_a_cheap_band(self):
        book = {"asks": [{"price": "0.25", "size": "100"}], "min_order_size": "5"}
        s = {"asset": "A", "avg_entry": 0.24}
        with Patch(signals, get=lambda url, params=None, raw=False: book):
            self.assertIsNone(signals.size_bet(dict(s)))                           # normal band
            got = signals.size_bet(dict(s), min_price=0.15, max_price=0.395)
        self.assertEqual((got["price"], got["stake"]), (0.25, 1.0))
        self.assertAlmostEqual(got["shares"], 4.0)


class CheapShadow(InTempDir):
    WATCH = "0xf8dc8cd0fd55eccb5409ed99ed90a86bc7f0f31f"

    def _cand(self, n, event=None, cond=None):
        h = {"w": {"name": "T", "size": 40, "entry": 0.22, "win_rate": 0.41, "roi": 0.3,
                   "n": 500, "alt": True}}
        return {"asset": f"A{n}", "condition": cond or f"c{n}", "title": "t",
                "event": event or f"e{n}", "outcome": "Yes", "traders": ["T"],
                "holders": h, "avg_entry": 0.22, "price": 0.22, "hours": 5}

    @staticmethod
    def _size(c, min_price=None, max_price=None):
        assert (min_price, max_price) == (signals.CHEAP_MIN_PRICE, signals.CHEAP_MAX_PRICE)
        return dict(c, shares=4.5, stake=1.0, fee=0.04)

    def _run(self, book, cands, scores=None, strict=(), ledger=None, scan="WEATHER", seen=None):
        def find(pool, **kw):
            if seen is not None:
                seen.append((dict(pool), kw))
            return list(cands)
        scores = scores if scores is not None else {"w": score()}
        with Patch(signals, SCAN=scan):
            return signals.record_cheap(book, scores, list(strict), ledger or {"bets": []},
                                        find=find, size=self._size)

    def test_records_cheap_buys_as_shadow_only(self):
        b = shadow.Book("s.json")
        seen = []
        self.assertEqual(self._run(b, [self._cand(1)], seen=seen), 1)
        r = b.bets[0]
        self.assertEqual((r["kind"], r["reason"], r["strategy"]), ("skipped", "cheap_band", "WEATHER"))
        self.assertEqual(r["cheap"][0]["alt"], True)
        self.assertEqual(seen[0][1], {"min_price": signals.CHEAP_MIN_PRICE,
                                      "max_price": signals.CHEAP_MAX_PRICE,
                                      "min_usd": signals.CHEAP_MIN_USD})

    def test_only_in_the_weather_scan(self):
        b = shadow.Book("s.json")
        self.assertEqual(self._run(b, [self._cand(1)], scan="SPORTS"), 0)
        self.assertEqual(b.bets, [])

    def test_pool_is_strict_traders_plus_the_watchlist(self):
        scores = {"strict": score(), "weak": score(win=0.41, roi=0.3),
                  self.WATCH: score(win=0.41, roi=0.3, name="dingdingdangdang")}
        with Patch(signals, SCAN="WEATHER"):
            pool = signals.cheap_pool(scores)
        self.assertEqual(set(pool), {"strict", self.WATCH})

    def test_no_pool_means_no_work(self):
        b = shadow.Book("s.json")

        def never(pool, **kw):
            raise AssertionError("must not look")
        with Patch(signals, SCAN="WEATHER"):
            out = signals.record_cheap(b, {"weak": score(win=0.4)}, [], {"bets": []}, find=never)
        self.assertEqual(out, 0)

    def test_skips_held_markets_and_keeps_one_per_event(self):
        b = shadow.Book("s.json")
        ledger = {"bets": [{"asset": "X", "condition": "c2"}]}
        cands = [self._cand(1, event="m"), self._cand(2), self._cand(3, event="m"), self._cand(4)]
        self.assertEqual(self._run(b, cands, ledger=ledger), 2)
        self.assertEqual(sorted(x["asset"] for x in b.bets), ["A1", "A4"])
        self.assertEqual(self._run(b, [self._cand(5, event="m")]), 0)              # across runs

    def test_per_run_cap(self):
        b = shadow.Book("s.json")
        n = signals.CHEAP_MAX_PER_RUN + 3
        self.assertEqual(self._run(b, [self._cand(i) for i in range(n)]),
                         signals.CHEAP_MAX_PER_RUN)

    def test_a_failure_never_stops_the_bot(self):
        b = shadow.Book("s.json")

        def boom(pool, **kw):
            raise RuntimeError("api down")
        with Patch(signals, SCAN="WEATHER"):
            out = signals.record_cheap(b, {"w": score()}, [], {"bets": []}, find=boom)
        self.assertEqual((out, b.bets), (0, []))

    def test_cheap_records_do_not_touch_the_strategy_switch(self):
        b = shadow.Book("s.json")
        self._run(b, [self._cand(i) for i in range(3)])
        led = {"bets": [], "cash": 50, "auto": {"paused": True, "since": iso(-60)}}
        self.assertIsNone(shadow.switch_update(led, b.bets, "WEATHER"))


class Report(InTempDir):
    def test_report_shows_cheap_buckets_and_the_new_bar(self):
        import json
        import shadow_report
        b = shadow.Book("shadow_weather.json")
        r = b.add("WEATHER", "skipped", "cheap_band", asset="A1", condition="c", title="t",
                  price=0.22, shares=4.5, stake=1.0, fee=0.04)
        r.update(status="won", pnl=3.4)
        r = b.add("WEATHER", "skipped", "wide_low_win", asset="A2", condition="c", title="t",
                  price=0.6, shares=1.6, stake=1.0, fee=0.01,
                  extra={"wide": [{"name": "T", "alt": True}]})
        r.update(status="lost", pnl=-1.0)
        b.save()
        ledger = {"cash": 50, "bets": [
            {"asset": "R1", "condition": "c", "price": 0.6, "shares": 1.6, "stake": 1.0,
             "fee": 0.01, "status": "won", "pnl": 0.6, "holders": {"w": {"alt": True}}},
            {"asset": "R2", "condition": "c", "price": 0.6, "shares": 1.6, "stake": 1.0,
             "fee": 0.01, "status": "lost", "pnl": -1.0, "holders": {"w": {"alt": False}}},
            {"asset": "R3", "condition": "c", "price": 0.6, "shares": 1.6, "stake": 1.0,
             "fee": 0.01, "status": "won", "pnl": 0.6, "holders": {"w": {}}}]}
        with open("ledger_weather.json", "w") as fh:
            json.dump(ledger, fh)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            shadow_report.main()
        out = buf.getvalue()
        self.assertIn("cheap buckets 15-39c", out)
        yes = [ln for ln in out.splitlines() if "new bar: traders pass" in ln][0]
        no = [ln for ln in out.splitlines() if "new bar: real bets, traders fail" in ln][0]
        self.assertIn("1-1", yes)             # real R1 won + wide A2 lost
        self.assertIn("0-1", no)              # real R2 lost; R3 has no flag, left out

    def test_alt_flags(self):
        import shadow_report
        self.assertEqual(shadow_report.alt_flags({"holders": {"a": {"alt": True}, "b": {}}}), [True])
        self.assertEqual(shadow_report.alt_flags({"wide": [{"alt": False}, {"alt": None}]}), [False])
        self.assertEqual(shadow_report.alt_flags({}), [])


if __name__ == "__main__":
    unittest.main()
