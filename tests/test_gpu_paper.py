"""Offline tests for the GPU price-market paper test (gpu_paper.py). The rules they pin are in GPU_PAPER.md."""
import contextlib
import io
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gpu_paper as gp  # noqa: E402
import simcore  # noqa: E402

DAY = 86400.0


def book(bids, asks):
    return {"tick_size": "0.01", "min_order_size": "5",
            "bids": [{"price": str(p), "size": str(s)} for p, s in bids],
            "asks": [{"price": str(p), "size": str(s)} for p, s in asks]}


def row(tx, wallet, side, outcome, price, size, ts=1000):
    return {"transaction_hash": tx, "proxy_wallet": wallet, "side": side, "outcome_index": outcome,
            "token_id": "yes" if outcome == 0 else "no", "price": price, "size": size, "timestamp": ts}


# One real sweep (shape and numbers from the public feed, wallets replaced): a taker sold YES (bought NO)
# through the bids at 67, 66 and 65 cents. The single taker row carries only the average price.
TAKER = row("0x1", "taker", "BUY", 1, 0.3464430913, 1290.84)
SWEEP = [TAKER,
         row("0x1", "m1", "BUY", 0, 0.65, 500.0), row("0x1", "m2", "BUY", 0, 0.65, 500.0),
         row("0x1", "m3", "BUY", 0, 0.66, 122.54), row("0x1", "m4", "SELL", 1, 0.33, 50.0),
         row("0x1", "m5", "BUY", 0, 0.67, 118.3)]

MARKET = {"condition": "c1", "group": "busy", "token": "yes1", "question": "q1", "event": "e1", "fee_rate": 0.0}
POOL = {"rate": 960.0, "v": 4.5, "min_size": 50.0}


class MakerFills(unittest.TestCase):
    def test_each_resting_order_keeps_its_own_price_and_the_taker_row_is_left_out(self):
        fills = gp.maker_fills(SWEEP, [TAKER])
        self.assertEqual(len(fills), 5)
        self.assertEqual({f[1] for f in fills}, {"bid"})
        self.assertEqual(sorted(round(f[2], 2) for f in fills), [0.65, 0.65, 0.66, 0.67, 0.67])
        self.assertAlmostEqual(sum(f[3] for f in fills), 1290.84)

    def test_a_taker_buying_yes_hits_the_asks(self):
        taker = row("0x2", "taker", "BUY", 0, 0.40, 60)
        every = [taker, row("0x2", "m1", "SELL", 0, 0.40, 10), row("0x2", "m2", "BUY", 1, 0.60, 50)]
        fills = gp.maker_fills(every, [taker])
        self.assertEqual([(f[1], round(f[2], 2), f[3]) for f in fills], [("ask", 0.40, 10), ("ask", 0.40, 50)])

    def test_a_transaction_without_its_taker_row_is_refused(self):
        with self.assertRaises(ValueError):
            gp.maker_fills(SWEEP[1:], [])

    def test_a_taker_row_missing_from_the_full_list_is_refused(self):
        with self.assertRaises(ValueError):
            gp.maker_fills(SWEEP[1:], [TAKER])

    def test_resting_orders_that_do_not_add_up_to_the_taker_row_are_refused(self):
        with self.assertRaises(ValueError):          # one resting order of the sweep is missing
            gp.maker_fills(SWEEP[:-1], [TAKER])


class FillCounts(unittest.TestCase):
    def count(self, bid, sure):
        return gp.fills_against({"bid": bid, "ask": 0.99, "size": 50.0}, gp.maker_fills(SWEEP, [TAKER]), sure)

    def test_sure_needs_a_trade_through_our_price(self):
        self.assertEqual(self.count(0.66, True), (50.0, 0.0, 1000))
        self.assertEqual(self.count(0.65, True), (0.0, 0.0, None))

    def test_possible_also_counts_trades_at_our_price(self):
        self.assertEqual(self.count(0.65, False), (50.0, 0.0, 1000))
        self.assertEqual(self.count(0.64, False), (0.0, 0.0, None))

    def test_a_small_trade_fills_only_its_size_and_the_first_time_is_kept(self):
        fills = [(20, "ask", 0.45, 7.0), (10, "ask", 0.44, 5.0)]
        self.assertEqual(gp.fills_against({"bid": 0.40, "ask": 0.43, "size": 50.0}, fills, True), (0.0, 12.0, 10))


class CloseOut(unittest.TestCase):
    def test_a_long_position_is_sold_down_the_bids(self):
        cash = gp.close_out(book([(0.40, 20), (0.38, 100)], [(0.42, 100)]), 50, 0.0)
        self.assertAlmostEqual(cash, 20 * 0.40 + 30 * 0.38)

    def test_a_short_position_is_bought_back_up_the_asks_and_pays_the_fee(self):
        cash = gp.close_out(book([(0.40, 100)], [(0.50, 100)]), -50, 0.04)
        self.assertAlmostEqual(cash, -(50 * 0.50 + 50 * 0.04 * 0.5 * 0.5))

    def test_what_the_book_cannot_take_is_a_total_loss(self):
        self.assertAlmostEqual(gp.close_out(book([(0.40, 10)], []), 50, 0.0), 4.0)
        self.assertAlmostEqual(gp.close_out(book([], [(0.40, 10)]), -50, 0.0), -(4.0 + 40.0))

    def test_no_position_costs_nothing(self):
        self.assertEqual(gp.close_out(book([], []), 0, 0.07), 0.0)


class Quote(unittest.TestCase):
    def test_it_joins_the_best_qualifying_prices_with_50_shares(self):
        q = gp.quote_for(book([(0.47, 10), (0.46, 500)], [(0.48, 500)]), POOL)
        self.assertEqual((q["bid"], q["ask"], q["size"]), (0.46, 0.48, 50.0))
        self.assertGreater(q["usd_day"], 0)

    def test_no_quote_outside_the_midpoint_range(self):
        self.assertIsNone(gp.quote_for(book([(0.10, 500)], [(0.11, 500)]), POOL))

    def test_no_quote_when_the_pool_is_gone_or_needs_more_than_50(self):
        b = book([(0.46, 500)], [(0.47, 500)])
        self.assertIsNone(gp.quote_for(b, None))
        self.assertIsNone(gp.quote_for(b, dict(POOL, min_size=200.0)))

    def test_no_quote_when_a_side_would_be_outside_the_reward_band(self):
        self.assertIsNone(gp.quote_for(book([(0.40, 500)], [(0.55, 500)]), POOL))


class Score(unittest.TestCase):
    start = book([(0.46, 500)], [(0.47, 500)])

    def open_quote(self):
        return {"t": 1000.0, "quote": gp.quote_for(self.start, POOL)}

    def test_a_quiet_window_earns_reward_for_its_whole_length(self):
        o = self.open_quote()
        rec = gp.score(o, 1900.0, self.start, POOL, MARKET, [])
        self.assertAlmostEqual(rec["sure"]["reward"], o["quote"]["usd_day"] * 900 / DAY)
        self.assertEqual(rec["sure"], rec["possible"])
        self.assertEqual((rec["sure"]["pnl"], rec["covered_s"]), (0.0, 900.0))

    def test_reward_takes_the_lower_of_the_two_runs(self):
        crowded = book([(0.46, 50000)], [(0.47, 50000)])
        rec = gp.score(self.open_quote(), 1900.0, crowded, POOL, MARKET, [])
        self.assertAlmostEqual(rec["sure"]["reward"], gp.reward_rate(crowded, self.open_quote()["quote"], POOL) * 900 / DAY)

    def test_reward_stops_at_the_first_fill_and_the_position_is_closed_at_the_new_book(self):
        o = self.open_quote()
        later = book([(0.43, 500)], [(0.44, 500)])
        rec = gp.score(o, 1900.0, later, POOL, MARKET, [(1300, "bid", 0.45, 80.0)])
        self.assertAlmostEqual(rec["sure"]["pnl"], 50 * (0.43 - 0.46))
        self.assertEqual((rec["sure"]["bought"], rec["sure"]["sold"]), (50.0, 0.0))
        lower = min(o["quote"]["usd_day"], gp.reward_rate(later, o["quote"], POOL))
        self.assertGreater(lower, 0)
        self.assertAlmostEqual(rec["sure"]["reward"], lower * 300 / DAY)

    def test_both_sides_filled_keeps_the_spread(self):
        fills = [(1100, "bid", 0.45, 60.0), (1200, "ask", 0.48, 60.0)]
        rec = gp.score(self.open_quote(), 1900.0, self.start, POOL, MARKET, fills)
        self.assertAlmostEqual(rec["sure"]["pnl"], 50 * (0.47 - 0.46))

    def test_after_a_long_gap_there_is_no_reward_and_only_trades_before_expiry_count(self):
        fills = [(1000 + 1800, "bid", 0.45, 5.0), (1000 + 1801, "bid", 0.45, 40.0)]
        rec = gp.score(self.open_quote(), 1000.0 + 3600, self.start, POOL, MARKET, fills)
        self.assertEqual((rec["sure"]["reward"], rec["covered_s"], rec["sure"]["bought"]), (0.0, 0.0, 5.0))

    def test_trades_from_before_the_quote_or_after_this_run_are_ignored(self):
        fills = [(1000, "bid", 0.45, 5.0), (1901, "bid", 0.45, 5.0)]
        rec = gp.score(self.open_quote(), 1900.0, self.start, POOL, MARKET, fills)
        self.assertEqual(rec["sure"]["bought"], 0.0)

    def test_no_quote_earns_nothing_but_the_time_still_counts_as_covered(self):
        rec = gp.score({"t": 1000.0, "quote": None}, 1900.0, self.start, POOL, MARKET, [])
        self.assertEqual((rec["sure"]["reward"], rec["possible"]["pnl"], rec["covered_s"]), (0.0, 0.0, 900.0))


class Choose(unittest.TestCase):
    NOW = datetime(2026, 11, 27, tzinfo=timezone.utc)

    def cand(self, cond, rate, event, end="2027-02-01", bid=0.46, ask=0.47, min_size=50.0):
        return {"condition": cond, "rate": rate, "event": event, "end": end, "v": 4.5, "min_size": min_size,
                "token": "t" + cond, "question": cond, "book": book([(bid, 500)], [(ask, 500)])}

    def pick(self, cands, fee=lambda c: 0.0):
        return [(m["group"], m["condition"]) for m in gp.choose(cands, fee, now=self.NOW)]

    def test_groups_order_and_one_market_per_event(self):
        cands = [self.cand("far", 960, "e1", bid=0.26, ask=0.27), self.cand("mid", 960, "e1"),
                 self.cand("late", 960, "e2", end="2027-03-01"), self.cand("small", 40, "e1"),
                 self.cand("mid-rate", 360, "e3")]
        self.assertEqual(self.pick(cands), [("busy", "mid"), ("busy", "late"), ("busy", "mid-rate"), ("quiet", "small")])

    def test_at_most_five_per_group(self):
        self.assertEqual(len(self.pick([self.cand(str(i), 960, f"e{i}") for i in range(9)])), 5)

    def test_unsuitable_markets_are_left_out(self):
        cands = [self.cand("soon", 960, "a", end="2026-12-20"), self.cand("big-min", 960, "b", min_size=200.0),
                 self.cand("edge", 960, "c", bid=0.05, ask=0.06), self.cand("wide", 960, "d", bid=0.40, ask=0.50),
                 self.cand("nopool", 0, "e"), self.cand("nofee", 960, "f")]
        self.assertEqual(self.pick(cands, lambda c: None if c == "nofee" else 0.0), [])


def rec(day, condition, reward, pnl=0.0, sure_fill=False, group="busy", covered=DAY, possible_pnl=None):
    t = START + day * DAY + 600
    part = {"reward": reward, "pnl": pnl, "bought": 50.0 if sure_fill else 0.0, "sold": 0.0, "until": t}
    return {"t": t, "placed": t - 600, "condition": condition, "group": group, "covered_s": covered, "sure": part,
            "possible": dict(part, pnl=pnl if possible_pnl is None else possible_pnl)}


START = datetime(2026, 11, 28, tzinfo=timezone.utc).timestamp()


def before_the_period():
    """The test's first record, an hour before the midnight at which the judged period starts."""
    r = rec(0, "a", 5.0)
    r.update(t=START - 3600, placed=START - 4200, sure=dict(r["sure"], until=START - 3600),
             possible=dict(r["possible"], until=START - 3600))
    return r


def period(days=14, reward=3.0, pnl=0.0, fills_per_day=3, markets=("a", "b")):
    return [rec(d, m, reward, pnl, sure_fill=i < fills_per_day) for d in range(days) for i, m in enumerate(markets)
            ] + [rec(d, "a", 0.0, sure_fill=True, covered=0.0) for d in range(days) for _ in range(max(0, fills_per_day - 2))]


class Judge(unittest.TestCase):
    def judge(self, records, days=14, rule="sure"):
        return gp.judge(records, "busy", rule, START, days)

    def test_a_steady_profit_passes_every_line(self):
        j = self.judge(period())
        self.assertAlmostEqual(j["net"], 14 * 2 * 3.0)
        self.assertEqual((j["coverage"], j["fill_windows"], j["passes"]), (1.0, 42, True))

    def test_a_market_day_under_one_dollar_pays_nothing(self):
        j = self.judge([rec(0, "a", 0.6), rec(0, "a", 0.6), rec(0, "b", 0.99)])
        self.assertAlmostEqual(j["net"], 1.2)

    def straddle(self, until):
        """A window placed 5 minutes before midnight and scored 10 minutes after it, earning until `until`."""
        r = rec(1, "a", 0.9)
        r["placed"], r["t"] = START + DAY - 300, START + DAY + 600
        r["sure"] = dict(r["sure"], until=START + until)
        return r

    def test_reward_earned_across_midnight_is_split_between_the_two_days(self):
        j = self.judge([rec(0, "a", 0.8), self.straddle(DAY + 600)])
        self.assertAlmostEqual(j["net"], 0.8 + 0.3)          # day 1 reaches $1; day 2 gets 0.6 and is not paid

    def test_reward_that_stopped_before_midnight_belongs_to_the_earlier_day(self):
        j = self.judge([rec(0, "a", 0.8), self.straddle(DAY - 60)])
        self.assertAlmostEqual(j["net"], 1.7)

    def test_an_unscored_loss_belongs_to_the_day_the_quote_was_placed(self):
        r = rec(13, "a", 0.0, pnl=-26.5)
        r.update(unscored=True, t=START + 15 * DAY)
        self.assertAlmostEqual(self.judge(period() + [r])["net"], 84.0 - 26.5)

    def test_half_rewards_are_halved_before_the_one_dollar_rule(self):
        j = self.judge([rec(0, "a", 1.5), rec(0, "b", 4.0)])
        self.assertAlmostEqual(j["net_half_reward"], 2.0)

    def test_one_great_market_or_day_cannot_carry_the_result(self):
        records = period(reward=0.0, pnl=-0.1) + [rec(3, "a", 50.0, covered=0.0)]
        j = self.judge(records)
        self.assertGreater(j["net"], 0)
        self.assertLess(j["net_without_best_market"], 0)
        self.assertLess(j["net_without_best_day"], 0)
        self.assertFalse(j["passes"])

    def test_each_half_must_be_positive(self):
        records = period() + [rec(10, "a", 0.0, pnl=-100.0, covered=0.0)]
        j = self.judge(records)
        self.assertGreater(j["halves"][0], 0)
        self.assertLess(j["halves"][1], 0)
        self.assertFalse(j["passes"])

    def test_records_outside_the_period_do_not_count(self):
        records = period() + [rec(-1, "a", 99.0), rec(14, "a", 99.0)]
        self.assertAlmostEqual(self.judge(records)["net"], 84.0)

    def test_verdict_needs_enough_data_before_it_can_pass(self):
        few = period(fills_per_day=0)
        self.assertEqual(gp.verdict(few, "busy", START, 14), "NOT ENOUGH DATA")
        thin = [dict(r, covered_s=r["covered_s"] * 0.8) for r in period()]
        self.assertEqual(gp.verdict(thin, "busy", START, 14), "NOT ENOUGH DATA")

    def test_a_pass_has_to_hold_under_both_fill_counts(self):
        self.assertEqual(gp.verdict(period(), "busy", START, 14), "PASS")
        worse = [dict(r, possible=dict(r["possible"], pnl=-10.0)) for r in period()]
        self.assertEqual(gp.verdict(worse, "busy", START, 14), "FAIL")

    def test_the_report_starts_the_period_at_the_first_midnight_and_marks_interim(self):
        text = gp.report([before_the_period()] + period(days=3))
        self.assertIn("2026-11-28", text)
        self.assertIn("INTERIM", text)
        self.assertNotIn("PASS", text)

    def test_no_verdict_until_a_day_after_the_period_so_stuck_quotes_are_in(self):
        text = gp.report([before_the_period()] + period(days=15))
        self.assertIn("INTERIM", text)
        self.assertNotIn("PASS", text)

    def test_the_report_gives_the_verdict_from_the_first_14_days_only(self):
        records = [before_the_period()] + period(days=14) + [rec(15, "a", 0.0, pnl=-999.0)]
        self.assertIn("VERDICT (days 1-14): PASS", gp.report(records))

    def test_short_data_is_read_once_more_at_day_28_then_ends_as_not_proven(self):
        first = [before_the_period()]
        self.assertIn("read again at day 28", gp.report(first + period(days=16, fills_per_day=0)))
        self.assertIn("not proven", gp.report(first + period(days=30, fills_per_day=0)))

    def test_an_empty_log_says_so(self):
        self.assertIn("No records", gp.report([]))


class Reply:
    def __init__(self, body, status=200):
        self.body, self.status_code = body, status

    def json(self):
        return self.body


class FakeSession:
    """Answers the four public endpoints the script reads."""

    def __init__(self):
        self.lag, self.trades_fail, self.takers, self.every, self.cursor = 3, False, [], [], None
        self.pools = [{"condition_id": "c1", "event_slug": "ornn-h200-average-rental-price-in-january-2027", "question": "q1",
                       "rewards_config": [{"rate_per_day": 960}], "rewards_max_spread": 4.5, "rewards_min_size": 50,
                       "end_date": "2027-02-01", "tokens": [{"outcome": "Yes", "token_id": "yes1"}, {"outcome": "No", "token_id": "no1"}]},
                      {"condition_id": "other", "event_slug": "something-else", "question": "x", "rewards_config": [{"rate_per_day": 9999}],
                       "rewards_max_spread": 4.5, "rewards_min_size": 50, "end_date": "2027-02-01",
                       "tokens": [{"outcome": "Yes", "token_id": "yes9"}]}]

    def get(self, url, params=None, timeout=None):
        if url.endswith("/rewards/markets/multi"):
            return Reply({"data": self.pools, "next_cursor": "LTE="})
        if url.endswith("/v2/status"):
            return Reply({"data": {"serving": {"lag_seconds": self.lag}}})
        if url.endswith("/v2/trades"):
            if self.trades_fail:
                return Reply({}, 400)
            return Reply({"data": self.takers if params["taker_only"] == "true" else self.every,
                          "pagination": {"next_cursor": self.cursor}})
        return Reply([{"feesEnabled": False}])


class Run(unittest.TestCase):
    def setUp(self):
        self.old = os.getcwd()
        self.tmp = tempfile.TemporaryDirectory()
        os.chdir(self.tmp.name)
        self.session = FakeSession()
        self.books = {"yes1": book([(0.46, 500)], [(0.47, 500)])}

    def tearDown(self):
        os.chdir(self.old)
        self.tmp.cleanup()

    def run_at(self, t, sleep=lambda s: None):
        with contextlib.redirect_stdout(io.StringIO()):
            return gp.main(session=self.session, fetch=lambda tokens: self.books, sleep=sleep, clock=lambda: t)

    def log(self):
        return simcore.read_log(gp.LOG_FILE)

    def state(self):
        return simcore.read_json(gp.STATE_FILE, {})

    def test_first_run_fixes_the_market_list_and_places_a_quote(self):
        self.assertEqual(self.run_at(START), 0)
        self.assertEqual([m["condition"] for m in self.state()["markets"]], ["c1"])
        self.assertEqual(self.state()["open"]["c1"]["quote"]["bid"], 0.46)
        self.assertEqual(self.log(), [])

    def test_the_next_run_scores_the_quote_from_the_trades_and_quotes_again(self):
        self.run_at(START)
        taker = row("0x9", "t", "SELL", 0, 0.45, 60, ts=int(START) + 100)
        self.session.takers = [taker]
        self.session.every = [taker, row("0x9", "m", "BUY", 0, 0.45, 60, ts=int(START) + 100)]
        self.run_at(START + 900)
        (r,) = self.log()
        self.assertEqual((r["condition"], r["sure"]["bought"], r["group"]), ("c1", 50.0, "busy"))
        self.assertEqual(self.state()["open"]["c1"]["t"], START + 900)

    def test_a_failed_trade_read_keeps_the_quote_for_the_next_run(self):
        self.run_at(START)
        self.session.trades_fail = True
        self.run_at(START + 900)
        self.assertEqual((self.log(), self.state()["open"]["c1"]["t"]), ([], START))

    def test_a_lagging_trade_feed_changes_nothing(self):
        self.run_at(START)
        self.session.lag = 600
        self.run_at(START + 900)
        self.assertEqual((self.log(), self.state()["open"]["c1"]["t"]), ([], START))

    def test_a_missing_book_waits_and_is_given_up_after_a_day(self):
        self.run_at(START)
        self.books = {}
        self.run_at(START + 900)
        self.assertEqual(self.state()["open"]["c1"]["t"], START)
        self.run_at(START + DAY + 1)
        (r,) = self.log()
        self.assertEqual((r["unscored"], r["covered_s"], r["placed"]), (True, 0.0, START))
        self.assertAlmostEqual(r["sure"]["pnl"], -50 * 0.53)     # bid 46, ask 47: the worse side is lost in full
        self.assertEqual(r["possible"], r["sure"])
        self.assertIsNone(self.state()["open"]["c1"]["quote"])

    def test_a_feed_that_falls_behind_during_the_pause_changes_nothing(self):
        self.run_at(START)
        self.run_at(START + 900, sleep=lambda s: setattr(self.session, "lag", 600))
        self.assertEqual((self.log(), self.state()["open"]["c1"]["t"]), ([], START))

    def test_a_missing_state_file_beside_an_old_log_stops_the_test(self):
        self.run_at(START)
        self.run_at(START + 900)
        os.remove(gp.STATE_FILE)
        self.assertEqual(self.run_at(START + 1800), 1)
        self.assertFalse(os.path.exists(gp.STATE_FILE))

    def test_an_unusable_trade_row_keeps_the_quote_instead_of_crashing(self):
        self.run_at(START)
        self.session.every = [{"transaction_hash": "0x1"}]
        self.assertEqual(self.run_at(START + 900), 0)
        self.assertEqual((self.log(), self.state()["open"]["c1"]["t"]), ([], START))

    def test_an_empty_page_that_promises_more_is_not_a_complete_read(self):
        self.session.cursor = "more"
        self.assertIsNone(gp.fetch_trades(self.session, "c1", 0, 10, False))

    def test_the_test_stops_recording_once_its_longest_period_is_over(self):
        self.run_at(START)
        self.run_at(START + 900)
        self.assertEqual(self.run_at(START + 31 * DAY + 1), 0)
        self.assertEqual((len(self.log()), self.state()["open"]["c1"]["t"]), (1, START + 900))

    def test_the_main_bot_files_are_never_written(self):
        self.run_at(START)
        self.run_at(START + 900)
        self.assertTrue(all(n.startswith("gpu_paper_") for n in os.listdir(".")))


if __name__ == "__main__":
    unittest.main()
