"""Tests for sizing.py (stake rule for a small bankroll that must never reach zero) and sizing_report.py
(what rules would have done on past bets). Hand-worked numbers; nothing touches the network or a real ledger.

Run:  python -m unittest discover -s tests -v
"""
import itertools
import math
import os
import random
import sys
import tempfile
import json
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sizing  # noqa: E402
import sizing_report  # noqa: E402


class Pieces(unittest.TestCase):
    def test_cost_per_share_adds_the_taker_fee(self):
        self.assertAlmostEqual(sizing.cost_per_share(0.70, 0.01), 0.7021, places=12)      # 0.70 + 0.01 * 0.7 * 0.3
        self.assertEqual(sizing.cost_per_share(0.70, 0.0), 0.70)

    def test_kelly_fraction(self):
        self.assertAlmostEqual(sizing.kelly_fraction(0.8, 0.7), 0.1 / 0.3, places=12)
        self.assertEqual(sizing.kelly_fraction(0.7, 0.7), 0.0)                            # no edge, no stake
        self.assertEqual(sizing.kelly_fraction(0.6, 0.7), 0.0)                            # negative edge, no stake
        self.assertEqual(sizing.kelly_fraction(0.9, 1.0), 0.0)
        self.assertEqual(sizing.kelly_fraction(0.9, 0.0), 0.0)
        self.assertEqual(sizing.kelly_fraction(float("nan"), 0.5), 0.0)
        self.assertAlmostEqual(sizing.kelly_fraction(1.7, 0.5), 1.0, places=12)           # a chance above 1 is treated as 1

    def test_the_edge_we_believe_is_the_low_end_of_the_range(self):
        self.assertAlmostEqual(sizing.edge_lower_bound(0.10, 0.04), 0.10 - 1.645 * 0.04, places=12)
        self.assertEqual(sizing.edge_lower_bound(0.05, 0.04), 0.0)                         # the range reaches below zero
        self.assertEqual(sizing.edge_lower_bound(-0.2, 0.01), 0.0)
        self.assertEqual(sizing.edge_lower_bound(0.3, float("inf")), 0.0)                  # no error bar, no belief
        self.assertEqual(sizing.edge_lower_bound(float("nan"), 0.01), 0.0)
        self.assertEqual(sizing.edge_lower_bound(0.3, -0.1), 0.0)

    def test_smallest_order(self):
        self.assertAlmostEqual(sizing.min_order(0.70, 0.01), 5 * 0.7021, places=12)


class Stake(unittest.TestCase):
    def test_no_proven_edge_means_no_stake(self):
        self.assertEqual(sizing.stake(48, 0.70, 0.0, 0.0), (0.0, "no proven edge"))
        self.assertEqual(sizing.stake(48, 0.70, 0.0, -0.1)[0], 0.0)

    def test_only_the_reserve_left_means_no_stake(self):
        self.assertEqual(sizing.stake(2.0, 0.70, 0.0, 0.5), (0.0, "only the reserve is left"))
        self.assertEqual(sizing.stake(1.0, 0.70, 0.0, 0.5)[0], 0.0)

    def test_a_fraction_of_kelly_when_it_is_above_the_smallest_order(self):
        # bankroll 48, price 0.70, believed edge 30%: p = 0.91, Kelly = 0.21 / 0.3 = 0.7, quarter = 0.175, spendable 46
        s, why = sizing.stake(48, 0.70, 0.0, 0.30, max_frac=0.5)
        self.assertAlmostEqual(s, 46 * 0.175, places=12)
        self.assertEqual(why, "fraction of Kelly")

    def test_the_cap_limits_a_big_edge(self):
        # price 0.5, edge 50%: p = 0.75, Kelly = 0.5, quarter = 0.125, capped at 5% of 198 = 9.90 (smallest order is 2.50)
        s, why = sizing.stake(200, 0.50, 0.0, 0.50)
        self.assertAlmostEqual(s, 198 * 0.05, places=12)
        self.assertEqual(why, "fraction of Kelly")

    def test_half_kelly_stakes_more_than_quarter(self):
        q = sizing.stake(200, 0.50, 0.0, 0.10, max_frac=1.0)[0]
        h = sizing.stake(200, 0.50, 0.0, 0.10, kelly_mult=0.5, max_frac=1.0)[0]
        self.assertAlmostEqual(h, 2 * q, places=12)

    def test_a_small_kelly_stake_becomes_the_smallest_order(self):
        # the same 30% edge at the default 5% cap is 2.30, below the 5-share minimum of 3.50
        s, why = sizing.stake(48, 0.70, 0.0, 0.30)
        self.assertAlmostEqual(s, 3.5, places=12)
        self.assertEqual(why, "minimum order (Kelly is below it)")

    def test_when_the_smallest_order_is_too_big_a_share_of_what_is_left_skip(self):
        s, why = sizing.stake(10, 0.70, 0.0, 0.30)                  # spendable 8, smallest order 3.50 is more than 25% of it
        self.assertEqual((s, why), (0.0, "smallest order is too big for what is left"))
        self.assertEqual(sizing.stake(10, 0.70, 0.0, 0.30, floor_frac=0.5)[0], 3.5)

    def test_bad_input_is_a_skip(self):
        for args in ((float("nan"), 0.7, 0.0, 0.3), (48, float("nan"), 0.0, 0.3), (48, 0.7, 0.0, float("inf"))):
            self.assertEqual(sizing.stake(*args), (0.0, "bad input"))
        self.assertEqual(sizing.stake(48, 1.0, 0.0, 0.3)[1], "price out of range")
        self.assertEqual(sizing.stake(48, 0.0, 0.0, 0.3)[1], "price out of range")

    def test_stakes_shrink_with_the_bankroll_and_never_touch_the_reserve(self):
        a = sizing.stake(200, 0.50, 0.0, 0.50)[0]
        b = sizing.stake(102, 0.50, 0.0, 0.50)[0]                    # half the spendable money
        self.assertAlmostEqual(b, a / 1.98, places=12)
        for bank, price, edge in itertools.product((2.5, 5, 12, 25, 50, 400), (0.05, 0.3, 0.7, 0.97), (0.0, 0.02, 0.1, 0.5, 3.0)):
            s = sizing.stake(bank, price, 0.01, edge)[0]
            self.assertGreaterEqual(s, 0.0)
            self.assertLessEqual(s, max(bank - sizing.RESERVE, 0.0) + 1e-12)
            self.assertGreaterEqual(bank - s, sizing.RESERVE - 1e-12 if bank >= sizing.RESERVE else 0.0)   # losing the whole stake leaves the reserve

    def test_a_bigger_believed_edge_never_stakes_less(self):
        for bank, price in itertools.product((20, 48, 150), (0.2, 0.5, 0.8)):
            last = 0.0
            for edge in (0.0, 0.01, 0.03, 0.06, 0.1, 0.2, 0.5, 1.0):
                s = sizing.stake(bank, price, 0.01, edge)[0]
                self.assertGreaterEqual(s + 1e-12, last)
                last = s


class BadSettingsAndInputs(unittest.TestCase):
    def test_settings_that_would_break_the_reserve_are_refused(self):
        self.assertEqual(sizing.stake(3, 0.5, 0.0, 0.01, floor_frac=3), (0.0, "bad settings"))        # would stake 2.50 with 1 spendable
        self.assertEqual(sizing.stake(3, 0.5, 0.0, 0.01, kelly_mult=-1, min_shares=-1)[0], 0.0)
        self.assertEqual(sizing.stake(48, 0.5, 0.0, 0.5, max_frac=1.5)[1], "bad settings")
        self.assertEqual(sizing.stake(48, 0.5, 0.0, 0.5, reserve=-1)[1], "bad settings")
        self.assertEqual(sizing.stake(48, 0.5, 0.0, 0.5, min_shares=0)[1], "bad settings")
        self.assertEqual(sizing.stake(48, 0.5, 0.0, 0.5, min_shares=float("nan"))[1], "bad input")
        self.assertEqual(sizing.stake(48, None, 0.0, 0.5)[1], "bad input")
        self.assertEqual(sizing.stake(48, True, 0.0, 0.5)[1], "bad input")

    def test_whatever_the_settings_a_stake_stays_inside_what_is_spendable(self):
        rnd = random.Random(11)
        for _ in range(5000):
            bank, reserve = rnd.choice([rnd.uniform(0, 300), 2.0, 3.0, 0.0]), rnd.uniform(-1, 6)
            s, _ = sizing.stake(bank, rnd.uniform(0.01, 0.99), rnd.choice([0.0, 0.01, 0.05]), rnd.uniform(-0.1, 2.0),
                                kelly_mult=rnd.uniform(-1, 3), max_frac=rnd.uniform(-1, 2), reserve=reserve,
                                min_shares=rnd.uniform(-2, 12), floor_frac=rnd.uniform(-1, 3))
            self.assertFalse(math.isnan(s))
            self.assertTrue(s == 0.0 or 0.0 < s <= bank - reserve + 1e-9, (bank, reserve, s))        # never past what is above the reserve

    def test_a_missing_error_bar_is_no_belief_not_a_crash(self):
        self.assertEqual(sizing.edge_lower_bound(0.1, None), 0.0)
        self.assertEqual(sizing.edge_lower_bound(None, 0.01), 0.0)
        self.assertEqual(sizing.edge_lower_bound(0.1, 0.01, z=None), 0.0)
        self.assertEqual(sizing.kelly_fraction(None, 0.5), 0.0)


class Tuition(unittest.TestCase):
    def test_learning_money_taken_from_the_bankroll_leaves_the_reserve(self):
        self.assertEqual(sizing.tuition_stake(0.0, 0.5, 0.0, bankroll=3.0), (0.0, "only the reserve is left"))   # 2.50 of 1 spendable
        self.assertEqual(sizing.tuition_stake(0.0, 0.5, 0.0, bankroll=4.5)[0], 2.5)
        self.assertEqual(sizing.tuition_stake(0.0, 0.5, 0.0, bankroll=4.4)[0], 0.0)

    def test_nan_settings_are_refused(self):
        self.assertEqual(sizing.tuition_stake(0.0, 0.5, 0.0, min_shares=float("nan"))[1], "bad input")
        self.assertEqual(sizing.tuition_stake(0.0, 0.5, 0.0, budget=float("nan"))[1], "bad input")
        self.assertEqual(sizing.tuition_stake(0.0, 0.5, 0.0, min_shares=-1)[1], "bad settings")
        self.assertEqual(sizing.tuition_stake(0.0, 0.5, 0.0, bankroll=float("nan"))[1], "bad input")

    def test_the_learning_bucket_buys_minimum_orders_until_it_is_used_up(self):
        self.assertEqual(sizing.tuition_stake(0.0, 0.70, 0.0), (3.5, "learning bet at the minimum order"))
        self.assertEqual(sizing.tuition_stake(6.4, 0.70, 0.0)[0], 3.5)                      # 9.9 of 10
        self.assertEqual(sizing.tuition_stake(6.6, 0.70, 0.0), (0.0, "learning budget used up"))

    def test_bad_input(self):
        self.assertEqual(sizing.tuition_stake(float("nan"), 0.7, 0.0)[1], "bad input")
        self.assertEqual(sizing.tuition_stake(0.0, 1.0, 0.0)[1], "price out of range")


def mkbet(ret, day, event, price=0.5, stake=1.0, fee=0.0, **extra):
    return dict({"status": "won" if ret > 0 else "lost", "pnl": ret * (stake + fee), "stake": stake, "fee": fee, "price": price,
                 "shares": stake / price, "opened": f"2026-11-{day:02d}T10:00:00+00:00", "event": event}, **extra)


class Report(unittest.TestCase):
    def test_history_is_in_opening_order_with_the_return_per_dollar(self):
        led = {"bets": [mkbet(0.5, 9, "b"), mkbet(-1.0, 2, "a"), dict(mkbet(1.0, 3, "c"), status="open")]}
        h = sizing_report.history(led)
        self.assertEqual([round(r, 12) for _, r, _ in h], [-1.0, 0.5])
        self.assertTrue(h[0][0] < h[1][0])

    def test_the_fee_rate_is_the_recorded_one_or_worked_back_from_the_fee(self):
        self.assertEqual(sizing_report.bet_fee_rate({"fee_rate": 0.03}), 0.03)
        self.assertAlmostEqual(sizing_report.bet_fee_rate({"price": 0.5, "shares": 2.0, "fee": 0.01}), 0.01 / (2 * 0.25), places=12)
        self.assertEqual(sizing_report.bet_fee_rate({}), 0.0)
        self.assertEqual(sizing_report.bet_fee_rate({"price": 1.0, "shares": 2.0, "fee": 0.1}), 0.0)

    def test_replay_follows_the_bankroll(self):
        hist = [("d1", 0.5, {}), ("d2", -1.0, {})]
        r = sizing_report.replay(hist, 50.0, sizing_report.flat(1.0))
        self.assertAlmostEqual(r["final"], 49.5, places=12)
        self.assertAlmostEqual(r["low"], 49.5, places=12)
        self.assertAlmostEqual(r["drawdown"], 1.0 / 50.5, places=12)
        self.assertEqual((r["placed"], r["of"]), (2, 2))

    def test_replay_skips_a_bet_it_cannot_afford_instead_of_cutting_it(self):
        hist = [("d1", -1.0, {})] * 3
        r = sizing_report.replay(hist, 3.0, sizing_report.flat(5.0))                       # only 1 above the reserve: a $5 bet is skipped
        self.assertEqual((r["final"], r["placed"]), (3.0, 0))
        r = sizing_report.replay(hist, 3.0, sizing_report.flat(1.0))
        self.assertAlmostEqual(r["final"], sizing.RESERVE, places=12)                      # staked the 1 that was spendable, lost it
        self.assertEqual(r["placed"], 1)                                                    # then nothing was left to stake

    def test_the_rule_only_counts_bets_that_had_settled_when_this_one_opened(self):
        late = [mkbet(0.5, 1 + i % 25, f"e{i}", closed="2027-01-01T00:00:00+00:00") for i in range(30)]
        hist = sizing_report.history({"bets": late})
        self.assertEqual(sizing_report.rule(50.0, 29, hist), 0.0)                           # none of the 29 earlier winners had paid out yet
        done = [mkbet(0.5, 1 + i % 25, f"e{i}", closed="2026-10-01T00:00:00+00:00") for i in range(30)]
        self.assertAlmostEqual(sizing_report.rule(50.0, 29, sizing_report.history({"bets": done})), 2.5, places=12)

    def test_history_skips_bets_without_a_usable_return(self):
        led = {"bets": [mkbet(0.5, 1, "a"), dict(mkbet(0.5, 2, "b"), pnl=float("nan")), dict(mkbet(0.5, 3, "c"), pnl=None)]}
        self.assertEqual(len(sizing_report.history(led)), 1)

    def test_centring_makes_the_floored_average_hit_the_target(self):
        # returns -1 and +3, target 0: shifting by -2 gives -1 (already at the floor, stays -1) and +1, average 0
        self.assertAlmostEqual(sizing_report.centring_shift([-1.0, 3.0], 0.0), -2.0, places=9)
        self.assertEqual(sizing_report.centring_shift([-1.0, 3.0], None), 0.0)
        rnd = random.Random(5)
        for _ in range(50):
            rets = [rnd.choice([-1.0, -1.0, rnd.uniform(-0.5, 5.0)]) for _ in range(rnd.randint(1, 30))]
            for target in (0.0, 0.03, 0.2):
                sh = sizing_report.centring_shift(rets, target)
                got = sum(max(r + sh, -1.0) for r in rets) / len(rets)
                self.assertAlmostEqual(got, target, places=9)

    def test_simulate_uses_the_floored_centring(self):
        # returns -1 and +3 resampled together, assumed true return 0%: a lost bet stays at -100%, the win drops to +100%
        s = sizing_report.simulate([[-1.0, 3.0]], 1.0, 50.0, 2, 5, random.Random(1), target=0.0)
        self.assertEqual((s["median"], s["above"]), (50.0, 0.0))

    def test_simulate_checks_after_the_last_bet_too(self):
        s = sizing_report.simulate([[-1.0]], 5.0, 7.0, 1, 10, random.Random(1))            # 7 - 5 = 2: just the reserve, next bet impossible
        self.assertEqual((s["stuck"], s["median"]), (1.0, 2.0))
        self.assertEqual(sizing_report.simulate([[1.0]], 5.0, 7.0, 1, 10, random.Random(1))["stuck"], 0.0)

    def test_simulate_median_of_an_even_count_is_the_middle_of_the_two(self):
        class Seq:
            def __init__(self, picks):
                self.it = iter(picks)

            def choice(self, blocks):
                return next(self.it)
        loss, win = [-1.0], [1.0]
        s = sizing_report.simulate([loss, win], 48.0, 50.0, 1, 2, Seq([loss, win]))
        self.assertEqual((s["median"], s["stuck"]), (50.0, 0.5))                           # finals 2 and 98

    def test_simulate_refuses_inputs_that_would_crash_or_hang(self):
        rng = random.Random(1)
        with self.assertRaises(ValueError):
            sizing_report.simulate([[0.1]], 1.0, 50.0, 10, 0, rng)
        with self.assertRaises(ValueError):
            sizing_report.simulate([], 1.0, 50.0, 10, 5, rng)
        with self.assertRaises(ValueError):
            sizing_report.simulate([[]], 1.0, 50.0, 10, 5, rng)
        s = sizing_report.simulate([[]], 1.0, 50.0, 0, 5, rng)                              # nothing to bet on, nothing to do
        self.assertEqual((s["stuck"], s["median"]), (0.0, 50.0))

    def test_the_rule_waits_for_history_then_stakes_only_on_a_proven_edge(self):
        winners = [mkbet(0.5, 1 + i % 25, f"e{i}") for i in range(30)]
        hist = sizing_report.history({"bets": winners})
        self.assertEqual(sizing_report.rule(50.0, 5, hist), 0.0)                            # only 5 bets before: no verdict yet
        self.assertAlmostEqual(sizing_report.rule(50.0, 29, hist), 2.5, places=12)          # 29 winners: believed edge 50%, smallest order
        losers = sizing_report.history({"bets": [mkbet(-1.0, 1 + i % 25, f"e{i}") for i in range(30)]})
        self.assertEqual(sizing_report.rule(50.0, 29, losers), 0.0)

    def test_days_stay_together_when_resampled(self):
        hist = [("2026-11-01T10", 0.1, {}), ("2026-11-01T11", 0.2, {}), ("2026-11-02T10", 0.3, {})]
        self.assertEqual(sorted(len(b) for b in sizing_report.day_blocks(hist)), [1, 2])

    def test_simulate_certain_ruin_and_certain_safety(self):
        rng = random.Random(1)
        s = sizing_report.simulate([[-1.0]], 5.0, 50.0, 300, 20, rng)
        self.assertEqual(s["stuck"], 1.0)
        self.assertAlmostEqual(s["median"], 5.0, places=12)                                 # 50 -> 45 ... -> 5, then 3 is not enough for 5
        self.assertEqual(s["above"], 0.0)
        safe = sizing_report.simulate([[-1.0]], 5.0, 50.0, 300, 20, rng, target=0.0)       # re-centred to a true return of exactly zero
        self.assertEqual((safe["stuck"], safe["median"], safe["above"]), (0.0, 50.0, 0.0))

    def test_simulate_reports_the_middle_and_the_bad_end(self):
        # one bet per path, a win in two thirds of the days: the middle path won (51), the 5th percentile lost (49)
        s = sizing_report.simulate([[1.0], [1.0], [-1.0]], 1.0, 50.0, 1, 600, random.Random(3))
        self.assertEqual((s["median"], s["p5"]), (51.0, 49.0))
        self.assertAlmostEqual(s["above"], 2 / 3, delta=0.08)
        self.assertEqual(s["stuck"], 0.0)

    def test_simulate_is_repeatable_for_a_seed(self):
        blocks = [[0.5, -1.0], [-1.0], [0.3, 0.3, -1.0]]
        a = sizing_report.simulate(blocks, 2.5, 50.0, 100, 200, random.Random(7))
        b = sizing_report.simulate(blocks, 2.5, 50.0, 100, 200, random.Random(7))
        self.assertEqual(a, b)

    def test_stake_table_shows_the_minimum_order_taking_over(self):
        rows = sizing_report.stake_table(48.0, 0.70, 0.01)
        self.assertEqual([r[0] for r in rows], [0.03, 0.10, 0.30, 0.60])
        self.assertEqual({r[1] for r in rows}, {rows[0][3]})                                 # at 48 the 5% cap is below the smallest order for every edge
        big = sizing_report.stake_table(200.0, 0.50, 0.0)
        self.assertGreater(big[-1][1], big[-1][3])                                           # at 200 and a big edge Kelly (capped at 5%) is above it
        for _, q, h, m in rows:
            if q:
                self.assertGreaterEqual(q + 1e-12, m)                                       # a stake is never below the smallest order
            self.assertGreaterEqual(h + 1e-12, q)
            self.assertAlmostEqual(m, 5 * 0.7021, places=12)

    def test_the_report_and_loading(self):
        with tempfile.TemporaryDirectory() as d:
            led = {"cash": 50, "bets": [mkbet(0.5 if i % 3 else -1.0, 1 + i % 20, f"e{i}") for i in range(40)]}
            for name in ("ledger.json", "ledger_weather.json"):
                with open(os.path.join(d, name), "w", encoding="utf-8") as fh:
                    json.dump(led, fh)
            with open(os.path.join(d, "ledger_empty.json"), "w", encoding="utf-8") as fh:
                json.dump({"cash": 50, "bets": []}, fh)
            loaded = sizing_report.load(d)
            self.assertEqual(sorted(loaded), ["copy", "empty", "weather"])
        text = sizing_report.render(loaded, paths=50, n_bets=40)
        self.assertIn("copy: 40 settled bets", text)
        self.assertIn("weather: 40 settled bets", text)
        self.assertNotIn("empty:", text)
        self.assertIn("rule (sizing.py)", text)
        self.assertIn("true return +3%", text)
        self.assertIn("smallest order", text)
        one_day = {"cash": 50, "bets": [mkbet(0.5 if i % 2 else -1.0, 3, f"e{i}") for i in range(30)]}
        text = sizing_report.render({"day": one_day}, paths=10, n_bets=10)
        self.assertIn("only 1 day(s) of history, too few to resample", text)
        self.assertNotIn("true return 0%, $1", text)
        none = sizing_report.render({"x": {"bets": []}}, paths=10, n_bets=10)
        self.assertIn("No ledger has 20+ settled bets yet.", none)


if __name__ == "__main__":
    unittest.main()
