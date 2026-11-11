"""Tests for shadow_politics.py: the shadow-only politics consensus experiment.
No network: every call is faked. It must never touch a ledger.

Run:  python -m unittest discover -s tests -v
"""
import io
import contextlib
import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
_argv = sys.argv
sys.argv = ["signals.py", "SPORTS"]
import signals                                 # noqa: E402
sys.argv = _argv
import shadow                                  # noqa: E402
import shadow_politics as sp                   # noqa: E402
import simcore                                 # noqa: E402


def iso(hours):
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()


class InTempDir(unittest.TestCase):
    def setUp(self):
        self._old = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory()
        os.chdir(self._tmp.name)
        self._tags = dict(signals.TAGS)
        signals.TAGS.clear()

    def tearDown(self):
        signals.TAGS.clear()
        signals.TAGS.update(self._tags)
        os.chdir(self._old)
        self._tmp.cleanup()


def trader(name, strict=True, n=40, win=0.65, roi=0.2):
    return {"name": name, "n": n, "win_rate": win, "roi": roi, "strict": strict}


def pos(asset, cond="c1", event="ev1", end_h=240, size=200, cur=0.6, entry=0.6, out="Yes",
        redeemable=False):
    return {"asset": asset, "conditionId": cond, "eventSlug": event, "endDate": iso(end_h),
            "size": size, "curPrice": cur, "avgPrice": entry, "outcome": out,
            "title": f"title {asset}", "redeemable": redeemable}


def book(ask):
    return {"asks": [{"price": str(ask), "size": "500"}], "bids": []}


class Warm(InTempDir):
    def test_deadline_stops_the_lookups_and_reports_it(self):
        calls = []
        old = signals.event_tags
        signals.event_tags = lambda s: calls.append(s) or signals.TAGS.setdefault(s, [])
        try:
            self.assertFalse(sp.warm_tags(["a", "b"], time.monotonic() - 1))
            self.assertEqual(calls, [])
            self.assertTrue(sp.warm_tags(["a", "b"], time.monotonic() + 60))
            self.assertEqual(sorted(calls), ["a", "b"])
        finally:
            signals.event_tags = old

    def test_a_failed_lookup_does_not_stall_the_run(self):
        old = signals.event_tags
        signals.event_tags = lambda s: []          # API failure: nothing cached
        try:
            self.assertTrue(sp.warm_tags(["a"], time.monotonic() + 60))
        finally:
            signals.event_tags = old
        self.assertNotIn("a", signals.TAGS)
        self.assertFalse(sp.is_politics("a"))      # unknown counts as not politics

    def test_known_slugs_are_not_looked_up_again(self):
        signals.TAGS["a"] = ["politics"]
        old = signals.event_tags
        signals.event_tags = lambda s: self.fail("looked up a known slug")
        try:
            self.assertTrue(sp.warm_tags(["a", None, ""], time.monotonic() + 60))
        finally:
            signals.event_tags = old

    def test_save_tags_merges_with_the_file_on_disk(self):
        signals.write_json(signals.TAGS_FILE, {"old": ["x"]})
        signals.TAGS["new"] = ["politics"]
        sp.save_tags()
        self.assertEqual(signals.read_json(signals.TAGS_FILE, {}),
                         {"old": ["x"], "new": ["politics"]})


class Scoring(InTempDir):
    def test_scores_only_politics_bets_and_needs_15_of_them(self):
        signals.TAGS.update({"pol": ["politics"], "nfl": ["sports"]})
        hist = {"W1": [("pol", 1.0, 1.0)] * 10 + [("pol", -1.0, 1.0)] * 5 + [("nfl", -1.0, 1.0)] * 40,
                "W2": [("pol", 1.0, 1.0)] * 14,          # only 14 politics bets
                "W3": None}                               # history failed
        old = signals.trader_history
        signals.trader_history = lambda w: hist[w]
        try:
            scores = sp.score_politics({"W1": "one", "W2": "two", "W3": "three"},
                                       time.monotonic() + 60)
        finally:
            signals.trader_history = old
        self.assertEqual(list(scores), ["W1"])
        s = scores["W1"]
        self.assertEqual((s["n"], round(s["win_rate"], 3), round(s["roi"], 3)),
                         (15, 0.667, 0.333))

    def test_returns_none_when_tags_are_not_ready_in_time(self):
        old = signals.trader_history
        signals.trader_history = lambda w: [("pol", 1.0, 1.0)]
        try:
            self.assertIsNone(sp.score_politics({"W": "w"}, time.monotonic() - 1))
        finally:
            signals.trader_history = old

    def test_pool_keeps_profitable_traders_and_flags_the_strict_ones(self):
        signals.write_json(sp.TRADERS_FILE, {
            "scored_at": datetime.now(timezone.utc).isoformat(),
            "scores": {"A": {"name": "a", "n": 40, "win_rate": 0.7, "roi": 0.3},     # strict
                       "B": {"name": "b", "n": 40, "win_rate": 0.3, "roi": 0.5},     # profitable
                       "C": {"name": "c", "n": 40, "win_rate": 0.7, "roi": -0.1},    # losing
                       "D": {"name": "d", "n": 10, "win_rate": 0.9, "roi": 0.9}}})   # too few
        pool = sp.tracked_pool(time.monotonic() + 60)
        self.assertEqual({w: s["strict"] for w, s in pool.items()}, {"A": True, "B": False})

    def test_stale_scores_are_rebuilt_and_saved(self):
        signals.write_json(sp.TRADERS_FILE, {"scored_at": iso(-12), "scores": {}})
        olds = (sp.leaderboard_wallets, sp.score_politics)
        sp.leaderboard_wallets = lambda: {"W": "w"}
        sp.score_politics = lambda wallets, deadline: {
            "W": {"name": "w", "n": 20, "win_rate": 0.6, "roi": 0.2}}
        try:
            pool = sp.tracked_pool(time.monotonic() + 60)
        finally:
            sp.leaderboard_wallets, sp.score_politics = olds
        self.assertEqual(list(pool), ["W"])
        self.assertIn("W", signals.read_json(sp.TRADERS_FILE, {})["scores"])

    def test_leaderboard_uses_politics_filters_market_makers_and_adds_watchlist(self):
        rows = [{"user_id": "MM", "user_name": "maker", "pnl": 1, "volume": 1000},
                {"user_id": "OK", "user_name": "ok", "pnl": 500, "volume": 1000}]
        seen = []
        old = signals.data_v2
        signals.data_v2 = lambda path, params, max_pages=10, stop=None: seen.append(params) or rows
        try:
            wallets = sp.leaderboard_wallets()
        finally:
            signals.data_v2 = old
        self.assertEqual({p["category"] for p in seen}, {"POLITICS"})
        self.assertNotIn("MM", wallets)
        self.assertEqual(wallets["OK"], "ok")
        self.assertEqual(wallets["0xc8b9a30184244d427169cf62485dde6041b2b836"], "SnowLover7")

    def test_watchlist_has_mrworldwide_and_every_address_is_well_formed(self):
        self.assertEqual(sp.WATCHLIST["0x98d04f9eb52d08ef9697f6eb8d7cfe8d46e5f14a"], "mrworldwide")
        self.assertEqual(len(sp.WATCHLIST), 6)
        for wallet in sp.WATCHLIST:
            # 0x + 40 hex characters, lower case (a typo here would quietly track nobody)
            self.assertRegex(wallet, r"^0x[0-9a-f]{40}$")

    def test_watchlist_wallet_reaches_the_pool_even_if_not_on_the_leaderboard(self):
        old = signals.data_v2
        signals.data_v2 = lambda path, params, max_pages=10, stop=None: []
        try:
            wallets = sp.leaderboard_wallets()
        finally:
            signals.data_v2 = old
        self.assertEqual(wallets["0x98d04f9eb52d08ef9697f6eb8d7cfe8d46e5f14a"], "mrworldwide")


class FindPlays(InTempDir):
    def _find(self, rows_by_wallet, tracked=None):
        signals.TAGS.update({"ev1": ["politics"], "ev2": ["politics"], "nfl": ["sports"]})
        tracked = tracked or {w: trader(w) for w in rows_by_wallet}
        old = signals.positions
        signals.positions = lambda w, max_pages=6, **kw: rows_by_wallet[w]
        try:
            return sp.find_plays(tracked, time.monotonic() + 60)
        finally:
            signals.positions = old

    def test_groups_traders_by_side_and_counts_strict(self):
        plays = self._find({"A": [pos("Y1", entry=0.50)], "B": [pos("Y1", entry=0.60)],
                            "C": [pos("Y1", entry=0.70)]},
                           {"A": trader("A"), "B": trader("B", strict=False), "C": trader("C")})
        self.assertEqual(len(plays), 1)
        p = plays[0]
        self.assertEqual((p["agree"], p["strict"], round(p["avg_entry"], 2)), (3, 2, 0.60))
        self.assertEqual(p["traders"], ["A", "B", "C"])
        self.assertEqual([s["name"] for s in p["stats"]], ["A", "B", "C"])

    def test_opposite_sides_of_one_market_are_dropped(self):
        plays = self._find({"A": [pos("Y1")], "B": [pos("N1", out="No")]})
        self.assertEqual(plays, [])

    def test_only_politics_events_in_the_window_and_big_enough(self):
        plays = self._find({"A": [pos("sport", event="nfl"),
                                  pos("far", end_h=24 * 31),
                                  pos("over", end_h=0.2),
                                  pos("tiny", size=10, cur=0.5),
                                  pos("done", redeemable=True),
                                  pos("ok", cond="c9", event="ev2")]})
        self.assertEqual([p["asset"] for p in plays], ["ok"])

    def test_most_agreed_first(self):
        plays = self._find({"A": [pos("one", cond="c1"), pos("two", cond="c2", event="ev2")],
                            "B": [pos("two", cond="c2", event="ev2")]})
        self.assertEqual([(p["asset"], p["agree"]) for p in plays], [("two", 2), ("one", 1)])

    def test_none_while_the_tag_cache_is_still_warming(self):
        old = signals.positions
        signals.positions = lambda w, max_pages=6, **kw: [pos("Y1")]
        try:
            self.assertIsNone(sp.find_plays({"A": trader("A")}, time.monotonic() - 1))
        finally:
            signals.positions = old


def play(asset, agree, entry=0.60, event=None, strict=1):
    return {"asset": asset, "condition": f"c-{asset}", "title": f"t {asset}",
            "event": event or f"e-{asset}", "outcome": "Yes", "hours": 100.0,
            "traders": [f"T{i}" for i in range(agree)], "entries": [entry] * agree,
            "avg_entry": entry, "agree": agree, "strict": strict, "stats": []}


class Recording(InTempDir):
    def _rec(self, plays, asks, book_=None):
        b = book_ or shadow.Book("shadow_politics.json")
        added = sp.record_plays(b, plays, {a: book(x) for a, x in asks.items()})
        return b, added

    def test_three_agree_near_the_entry_is_consensus(self):
        b, added = self._rec([play("A", 3, entry=0.60)], {"A": 0.64})
        self.assertEqual(added["consensus"], 1)
        r = b.bets[0]
        self.assertEqual((r["strategy"], r["kind"], r["reason"], r["price"], r["stake"]),
                         ("POLITICS", "skipped", "consensus", 0.64, 1.0))
        self.assertEqual((r["agree"], r["strict"], r["avg_entry"]), (3, 1, 0.60))
        self.assertAlmostEqual(r["fee"], (1 / 0.64) * 0.07 * 0.64 * 0.36)

    def test_exactly_five_cents_still_counts_as_near(self):
        _, added = self._rec([play("A", 3, entry=0.60)], {"A": 0.65})
        self.assertEqual(added["consensus"], 1)

    def test_three_agree_but_price_ran_away_is_recorded_as_chased(self):
        b, added = self._rec([play("A", 4, entry=0.60)], {"A": 0.70})
        self.assertEqual((added["consensus"], added["consensus_chased"]), (0, 1))
        self.assertEqual((b.bets[0]["strategy"], b.bets[0]["reason"]), ("POLITICS", "consensus_chased"))

    def test_two_agree_is_the_control_group_and_only_when_near(self):
        b, added = self._rec([play("A", 2, entry=0.60), play("B", 2, entry=0.60)],
                             {"A": 0.62, "B": 0.75})
        self.assertEqual(added["two_agree"], 1)
        self.assertEqual([(r["strategy"], r["reason"], r["asset"]) for r in b.bets],
                         [("POLITICS_2", "two_agree", "A")])

    def test_one_trader_alone_is_never_recorded(self):
        b, _ = self._rec([play("A", 1)], {"A": 0.60})
        self.assertEqual(b.bets, [])

    def test_price_band_and_missing_books(self):
        b, _ = self._rec([play("cheap", 3, entry=0.30), play("dear", 3, entry=0.93),
                          play("nobook", 3)], {"cheap": 0.30, "dear": 0.93})
        self.assertEqual(b.bets, [])

    def test_one_record_per_signal_across_runs(self):
        b, _ = self._rec([play("A", 3)], {"A": 0.60})
        b, added = self._rec([play("A", 3)], {"A": 0.61}, b)
        self.assertEqual((len(b.bets), sum(added.values())), (1, 0))

    def test_one_record_per_event_per_group(self):
        b, added = self._rec([play("A", 3, event="same"), play("B", 3, event="same"),
                              play("C", 2, event="same")], {"A": 0.6, "B": 0.6, "C": 0.6})
        # the two 3+ plays share an event: one record; the control group is separate
        self.assertEqual(sorted((r["strategy"], r["asset"]) for r in b.bets),
                         [("POLITICS", "A"), ("POLITICS_2", "C")])

    def test_per_run_caps(self):
        plays = [play(f"P{i}", 3) for i in range(30)] + [play(f"Q{i}", 2) for i in range(30)]
        asks = {s["asset"]: 0.6 for s in plays}
        b, added = self._rec(plays, asks)
        self.assertEqual((added["consensus"], added["two_agree"]), (20, 10))

    def test_wanted_assets_skips_singles_and_already_recorded(self):
        b, _ = self._rec([play("done", 3)], {"done": 0.6})
        plays = [play("done", 3), play("new", 3), play("pair", 2), play("solo", 1)]
        self.assertEqual(sp.wanted_assets(plays, b), ["new", "pair"])

    def test_results_line_summarises_settled_records(self):
        b, _ = self._rec([play("A", 3), play("B", 3)], {"A": 0.5, "B": 0.5})
        b.bets[0].update(status="won", pnl=1.0)
        b.bets[1].update(status="lost", pnl=-1.0)
        line = sp.results_line(b)[0]
        self.assertIn("POLITICS/consensus: 2 records, 2 settled (1 won", line)


class EndToEnd(InTempDir):
    """main() with every network call faked."""

    def setUp(self):
        super().setUp()
        self._watch = dict(sp.WATCHLIST)
        sp.WATCHLIST.clear()          # only the three fake traders A, B, C

    def tearDown(self):
        sp.WATCHLIST.update(self._watch)
        super().tearDown()

    def _run(self, positions, asks, settle_pays=None):
        signals.TAGS.update({"ev1": ["politics"]})
        saved = (signals.data_v2, signals.trader_history, signals.positions, signals.get,
                 simcore.fetch_books)
        signals.data_v2 = lambda *a, **k: [
            {"user_id": w, "user_name": w, "pnl": 100, "volume": 500} for w in "ABC"]
        signals.trader_history = lambda w: [("ev1", 1.0, 1.0)] * 20
        signals.positions = lambda w, max_pages=6, **kw: positions
        signals.get = lambda url, params=None, raw=False: (
            {"data": [{"token_id": t, "resolved": True, "final_price": p}
                      for t, p in (settle_pays or {}).items()]}
            if url == simcore.V2_TOKENS else None)
        simcore.fetch_books = lambda tokens, **k: {t: book(asks[t]) for t in tokens if t in asks}
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                rc = sp.main()
        finally:
            (signals.data_v2, signals.trader_history, signals.positions, signals.get,
             simcore.fetch_books) = saved
        return rc, out.getvalue()

    def test_full_run_records_settles_and_never_writes_a_ledger(self):
        rc, out = self._run([pos("Y1", entry=0.60)], {"Y1": 0.63})
        self.assertEqual(rc, 0)
        recs = shadow.Book("shadow_politics.json").bets
        self.assertEqual([(r["strategy"], r["reason"], r["agree"]) for r in recs],
                         [("POLITICS", "consensus", 3)])
        self.assertTrue(os.path.exists("traders_politics.json"))
        self.assertTrue(os.path.exists("tags.json"))
        self.assertEqual([f for f in os.listdir(".") if f.startswith("ledger")], [])
        self.assertIn("New records", out)
        # a second run adds nothing, then the market resolves and the record settles
        self._run([pos("Y1", entry=0.60)], {"Y1": 0.63})
        self.assertEqual(len(shadow.Book("shadow_politics.json").bets), 1)
        self._run([pos("Y1", entry=0.60)], {"Y1": 0.63}, settle_pays={"Y1": 1.0})
        rec = shadow.Book("shadow_politics.json").bets[0]
        self.assertEqual(rec["status"], "won")
        self.assertGreater(rec["pnl"], 0)

    def test_nothing_qualifies_is_a_quiet_clean_run(self):
        rc, out = self._run([], {})
        self.assertEqual(rc, 0)
        self.assertEqual(shadow.Book("shadow_politics.json").bets, [])
        self.assertNotIn("Still warming", out)

    def test_an_exception_still_saves_what_was_settled(self):
        signals.TAGS.update({"ev1": ["politics"]})
        b = shadow.Book("shadow_politics.json")
        b.add_taker("POLITICS", "consensus", asset="Y1", price=0.5, fee_rate=0.07,
                    condition="c", title="t")
        b.save()
        saved = (signals.get, sp.tracked_pool)
        signals.get = lambda url, params=None, raw=False: (
            {"data": [{"token_id": "Y1", "resolved": True, "final_price": 1.0}]}
            if url == simcore.V2_TOKENS else None)

        def boom(deadline):
            raise RuntimeError("API down")
        sp.tracked_pool = boom
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(RuntimeError):
                    sp.main()
        finally:
            signals.get, sp.tracked_pool = saved
        self.assertEqual(shadow.Book("shadow_politics.json").bets[0]["status"], "won")


class Wiring(unittest.TestCase):
    def test_workflow_saves_the_files_this_script_writes(self):
        with open(os.path.join(ROOT, ".github", "workflows", "scan.yml")) as fh:
            yml = fh.read()
        self.assertIn("python shadow_politics.py", yml)
        self.assertIn("shadow_*.json", yml)       # shadow_politics.json
        self.assertIn("traders_*.json", yml)      # traders_politics.json
        step = yml.split("python shadow_politics.py")[0].rsplit("- name:", 1)[1]
        self.assertIn("continue-on-error: true", step)

    def test_names_match_what_the_report_and_workflow_expect(self):
        self.assertEqual(sp.SHADOW_FILE, "shadow_politics.json")
        self.assertTrue(sp.TRADERS_FILE.startswith("traders_"))


if __name__ == "__main__":
    unittest.main()
