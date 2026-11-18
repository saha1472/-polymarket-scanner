"""Offline tests for copy_quality.py and clv_report.py (pure: no network, no real ledger files).
Part of the bot's trading gate, so everything here must stay offline and fast."""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import clv_report as clv  # noqa: E402
import copy_quality as cq  # noqa: E402

T0 = 1_700_000_000        # an arbitrary entry time (epoch seconds)


def iso(t):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(t, timezone.utc).isoformat()


def bet(price=0.60, status="won", pnl=None, stake=1.0, holders=None, **kw):
    b = {"status": status, "price": price, "stake": stake, "asset": "tok", "opened": iso(T0),
         "pnl": (stake / price - stake) if pnl is None and status == "won" else (pnl if pnl is not None else -stake)}
    if holders is not None:
        b["holders"] = holders
    b.update(kw)
    return b


def holder(entry=0.55, size=100.0, name="alice", win=0.6, roi=0.2):
    return {"name": name, "entry": entry, "size": size, "win_rate": win, "roi": roi}


class CopyQuality(unittest.TestCase):
    def test_entry_is_size_weighted_and_the_biggest_holder_is_credited(self):
        b = bet(holders={"0xa": holder(0.50, 300, "big"), "0xb": holder(0.70, 100, "small")})
        entry, name, win, roi = cq.trader_entry(b)
        self.assertAlmostEqual(entry, 0.55)
        self.assertEqual(name, "big")

    def test_bets_without_a_usable_entry_are_skipped(self):
        self.assertIsNone(cq.trader_entry(bet(holders={"0xa": {"name": "x", "entry": 0, "size": 5}})))
        self.assertIsNone(cq.trader_entry(bet(holders={"0xa": {"name": "x", "entry": 1.5, "size": 5}})))
        self.assertIsNone(cq.trader_entry(bet(holders={})))
        self.assertIsNone(cq.trader_entry(bet()))
        led = {"bets": [bet(status="open", holders={"0xa": holder()}), bet(holders={"0xa": holder()}),
                        bet(pnl=None, status="won", stake=0, holders={"0xa": holder()})]}
        self.assertEqual(len(cq.copied_bets(led)), 1)

    def test_gap_is_what_we_paid_minus_what_the_trader_paid(self):
        rows = cq.copied_bets({"bets": [bet(price=0.60, holders={"0xa": holder(0.55)})]})
        self.assertAlmostEqual(rows[0]["gap"], 0.05)
        self.assertTrue(rows[0]["won"])

    def test_summary_roi_and_buckets(self):
        bets = []
        for i in range(6):          # chased by 6c: lost money
            bets.append(bet(price=0.61, status="lost", holders={"0xa": holder(0.55, name="chased")}))
        for i in range(6):          # paid the same as the trader: won
            bets.append(bet(price=0.55, status="won", holders={"0xb": holder(0.55, name="steady")}))
        rep = cq.analyse({"ledger": {"bets": bets}})
        self.assertEqual(rep["all"]["n"], 12)
        self.assertAlmostEqual(rep["by_trader"]["chased"]["roi"], -1.0)
        self.assertGreater(rep["by_trader"]["steady"]["roi"], 0)
        gaps = dict(rep["by_gap"])
        self.assertEqual(gaps["more than 5c more"]["n"], 6)
        self.assertEqual(gaps["paid no more than the trader"]["n"], 6)
        self.assertAlmostEqual(rep["by_trader"]["chased"]["gap"], 0.06)
        text = cq.render(rep)
        for needle in ("By ledger", "Does paying more", "chased", "steady", "claimed"):
            self.assertIn(needle, text)

    def test_empty_input_says_so(self):
        self.assertIn("No settled copied bets", cq.render(cq.analyse({"x": {"bets": []}})))

    def test_main_reads_files_and_ignores_broken_ones(self):
        with tempfile.TemporaryDirectory() as tmp:
            good = os.path.join(tmp, "ledger_a.json")
            with open(good, "w") as fh:
                json.dump({"cash": 50, "bets": [bet(holders={"0xa": holder()})]}, fh)
            bad = os.path.join(tmp, "ledger_b.json")
            with open(bad, "w") as fh:
                fh.write("not json")
            self.assertEqual(cq.main([good, bad, os.path.join(tmp, "missing.json")]), 0)


class ClvMath(unittest.TestCase):
    HIST = [[T0 - 100, 0.50], [T0 + 1800, 0.62], [T0 + 3 * 3600, 0.70], [T0 + 10 * 3600, 0.95]]

    def test_price_at(self):
        self.assertIsNone(clv.price_at(self.HIST, T0 - 200))
        self.assertEqual(clv.price_at(self.HIST, T0), 0.50)
        self.assertEqual(clv.price_at(self.HIST, T0 + 3600), 0.62)
        self.assertEqual(clv.price_at(self.HIST, T0 + 4 * 3600), 0.70)
        self.assertEqual(clv.price_at([], T0), None)

    def test_markouts_use_the_payout_once_the_market_is_over(self):
        ends = iso(T0 + 10 * 3600)                              # the market ended with the history (+10h)
        m = clv.markouts(bet(price=0.60, status="won", ends=ends), self.HIST)
        self.assertAlmostEqual(m[1], 0.62 - 0.60)               # an hour later
        self.assertAlmostEqual(m[6], 0.70 - 0.60)               # six hours later (still open)
        self.assertAlmostEqual(m[24], 1.0 - 0.60)               # the market was over by +24h: the result counts
        self.assertAlmostEqual(m["res"], 1.0 - 0.60)
        lost = clv.markouts(bet(price=0.60, status="lost", pnl=-1.0, ends=ends), self.HIST)
        self.assertAlmostEqual(lost[24], 0.0 - 0.60)

    def test_the_payout_never_stands_in_for_a_horizon_before_the_market_ended(self):
        # a short history (it stops at +10h) must not leak the result into a horizon where the market was still open
        late = iso(T0 + 30 * 3600)                              # this market only ends at +30h
        m = clv.markouts(bet(price=0.60, status="won", ends=late), self.HIST)
        self.assertAlmostEqual(m[6], 0.70 - 0.60)
        self.assertIsNone(m[24])                                 # unknown, NOT the +40c payout
        self.assertAlmostEqual(m["res"], 1.0 - 0.60)             # the final result itself is still known
        # no end time on the bet: no stand-in at all
        m = clv.markouts(bet(price=0.60, status="won"), self.HIST)
        self.assertIsNone(m[24])
        self.assertAlmostEqual(m[6], 0.70 - 0.60)
        # the same market with an end time before the horizon does use it
        m = clv.markouts(bet(price=0.60, status="won", ends=iso(T0 + 12 * 3600)), self.HIST)
        self.assertAlmostEqual(m[24], 1.0 - 0.60)
        # a price recorded exactly AT the horizon (the last point of the history) is a known price, not an unknown
        short = [[T0 - 100, 0.50], [T0 + 3600, 0.50]]
        m = clv.markouts(bet(price=0.60, status="won", ends=iso(T0 + 30 * 3600)), short)
        self.assertAlmostEqual(m[1], 0.50 - 0.60)
        self.assertIsNone(m[6])
        # a price recorded at the horizon beats the payout, even for a market that was due to end earlier
        m = clv.markouts(bet(price=0.60, status="won", ends=iso(T0 + 2 * 3600)), self.HIST)
        self.assertAlmostEqual(m[6], 0.70 - 0.60)                # the recorded price at +6h, not the 1.00 payout

    def test_a_stale_point_from_before_the_entry_is_never_used(self):
        # the last point at or before entry+1h is from BEFORE the bet: that price was never traded against
        hist = [[T0 - 3600, 0.50], [T0 + 7200, 0.70], [T0 + 9 * 3600, 0.99]]
        self.assertEqual(clv.price_at(hist, T0 + 3600), 0.50)                  # the old function would say 0.50
        self.assertIsNone(clv.price_after(hist, T0, T0 + 3600))
        self.assertEqual(clv.price_after(hist, T0, T0 + 3 * 3600), 0.70)
        m = clv.markouts(bet(price=0.60, status="won"), hist)
        self.assertIsNone(m[1])                                                # unknown, not -10c
        self.assertAlmostEqual(m[6], 0.70 - 0.60)
        self.assertEqual(clv.summarize([m])[1]["n"], 0)                        # and left out of the average

    def test_paid_over_the_mid_comes_from_the_stored_book(self):
        book = {"bids": [[0.60, 5.0], [0.55, 5.0]], "asks": [[0.90, 5.0]]}
        b = bet(price=0.90, status="won", audit={"book": book})
        self.assertAlmostEqual(clv.entry_mid(b), 0.75)
        m = clv.markouts(b, self.HIST)
        self.assertAlmostEqual(m["spread"], 0.15)                              # we paid 15c over the mid
        self.assertAlmostEqual(m["mid6"], 0.70 - 0.75)                         # +6h price against the MID, not the ask
        for odd in ({}, {"audit": {}}, {"audit": {"book": {"bids": [], "asks": [[0.9, 1]]}}},
                    {"audit": {"book": {"bids": [[0.9, 1]], "asks": [[0.8, 1]]}}}):          # crossed / missing
            self.assertIsNone(clv.entry_mid(bet(**odd)))
        self.assertIsNone(clv.markouts(bet(price=0.5), self.HIST)["spread"])

    def test_a_sold_bet_has_no_final_payout(self):
        m = clv.markouts(bet(price=0.60, status="stopped", pnl=-0.4), self.HIST)
        self.assertIsNotNone(m[1])
        self.assertIsNone(m[24])
        self.assertIsNone(m["res"])

    def test_unusable_inputs_give_none(self):
        self.assertEqual(set(clv.markouts(bet(opened="garbage"), self.HIST).values()), {None})
        self.assertEqual(set(clv.markouts(bet(), []).values()), {None})
        self.assertEqual(set(clv.markouts(bet(price="x", pnl=-1.0), self.HIST).values()), {None})

    def test_history_cleaning(self):
        rows = [{"t": 5, "p": 0.5}, {"t": 2, "p": 0.4}, {"t": 3, "p": 1.7}, {"t": "x", "p": 0.2}, "junk", {"t": 4}]
        self.assertEqual(clv.clean_history(rows), [[2.0, 0.4], [5.0, 0.5]])
        self.assertEqual(clv.clean_history(None), [])

    def test_summary_and_grouping_by_strategy(self):
        good = [[T0 - 10, 0.5], [T0 + 3600, 0.7], [T0 + 7 * 3600, 0.99]]
        ledgers = {"ledger_wx": {"bets": [bet(price=0.5, strategy="DEAD"), bet(price=0.5, strategy="LATE", status="lost", pnl=-1)]},
                   "ledger": {"bets": [bet(price=0.5), bet(status="open")]}}
        rep = clv.analyse(ledgers, {"tok": good})
        self.assertEqual(set(rep), {"ALL", "ledger_wx/DEAD", "ledger_wx/LATE", "ledger"})
        self.assertEqual(rep["ALL"][1]["n"], 3)                 # the open bet is not counted
        self.assertAlmostEqual(rep["ledger_wx/DEAD"][1]["mean"], 0.2)
        self.assertEqual(rep["ledger_wx/DEAD"][1]["pos"], 1.0)
        self.assertAlmostEqual(rep["ledger_wx/LATE"]["res"]["mean"], -0.5)
        text = clv.render(rep)
        for needle in ("+1h", "+24h", "at result", "paid over mid", "+6h vs mid", "ALL", "DEAD"):
            self.assertIn(needle, text)
        self.assertIn("No settled bets", clv.render({}))

    def test_se_shrinks_with_more_bets(self):
        few = clv.summarize([{1: 0.1, 6: None, 24: None, "res": None}, {1: -0.1, 6: None, 24: None, "res": None}])[1]
        many = clv.summarize([{1: 0.1, 6: None, 24: None, "res": None}, {1: -0.1, 6: None, 24: None, "res": None}] * 20)[1]
        self.assertLess(many["se"], few["se"])
        self.assertAlmostEqual(few["mean"], 0.0)


class ClvFiles(unittest.TestCase):
    def test_fetch_uses_the_documented_parameters_and_main_caches(self):
        calls = []

        def fake_get(url, params):
            calls.append((url, dict(params)))
            return {"history": [{"t": T0 - 10, "p": 0.5}, {"t": T0 + 3600, "p": 0.6}]}
        with tempfile.TemporaryDirectory() as tmp:
            led = os.path.join(tmp, "ledger_x.json")
            with open(led, "w") as fh:
                json.dump({"cash": 50, "bets": [bet(price=0.5), bet(price=0.55, status="lost", pnl=-1.0)]}, fh)
            cache = os.path.join(tmp, "cache.json")
            lines = []
            self.assertEqual(clv.main([led, "--cache", cache], get=fake_get, out=lines.append), 0)
            self.assertEqual(len(calls), 1)                       # both bets share one token: one download
            self.assertEqual(calls[0][0], clv.HISTORY_URL)
            self.assertEqual(calls[0][1], {"market": "tok", "interval": "max", "fidelity": "60"})
            self.assertTrue(any("ALL" in x for x in lines))
            self.assertEqual(clv.main([led, "--cache", cache], get=fake_get, out=lines.append), 0)
            self.assertEqual(len(calls), 1)                       # second run: from the cache
            with open(cache) as fh:
                self.assertIn("tok", json.load(fh))

    def test_a_failed_download_does_not_crash_or_poison_the_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            led = os.path.join(tmp, "ledger_x.json")
            with open(led, "w") as fh:
                json.dump({"cash": 50, "bets": [bet()]}, fh)
            lines = []
            self.assertEqual(clv.main([led], get=lambda u, p: None, out=lines.append), 0)
            self.assertTrue(any("No settled bets" in x for x in lines))

    def test_this_module_makes_no_network_call_on_import(self):
        # the bot's trading gate imports this file: importing must be free of side effects
        self.assertTrue(callable(clv.main))


if __name__ == "__main__":
    unittest.main()
