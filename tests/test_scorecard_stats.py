"""Tests for the honest statistics in scorecard.py: a pooled return per dollar, error bars that allow for bets that
share an event, a day or a trader, and a bets-to-detect figure that targets a fixed true return. Hand-worked numbers.

Run:  python -m unittest discover -s tests -v
"""
import math
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import scorecard  # noqa: E402


def bet(pnl, cost=1.0, **extra):
    return dict({"pnl": pnl, "cost": cost}, **extra)


def ledger(rows):
    """rows: (pnl, extra dict); stake 1, fee 0."""
    return {"cash": 50, "bets": [dict({"status": "won" if p > 0 else "lost", "pnl": p, "stake": 1.0, "fee": 0.0}, **x)
                                 for p, x in rows]}


class ClusterStats(unittest.TestCase):
    def test_independent_bets_have_the_usual_error_bar(self):
        st = scorecard.cluster_stats([bet(1), bet(-1), bet(1), bet(-1)], {})
        self.assertEqual((st["n"], st["pnl"], st["cost"], st["roi"]), (4, 0.0, 4.0, 0.0))
        self.assertAlmostEqual(st["se_iid"], math.sqrt(4 / 3 * 4) / 4, places=12)          # 0.57735
        self.assertEqual(st["se"], st["se_iid"])

    def test_bets_on_the_same_event_widen_the_error_bar(self):
        bets = [bet(1, ev="A"), bet(1, ev="A"), bet(-1, ev="B"), bet(-1, ev="B")]
        st = scorecard.cluster_stats(bets, {"event": lambda b: b["ev"]})
        self.assertAlmostEqual(st["se_by"]["event"], 1.0, places=12)       # clusters +2 and -2: sqrt(2 * 8) / 4
        self.assertAlmostEqual(st["se_iid"], 0.57735, places=5)
        self.assertEqual(st["se"], st["se_by"]["event"])                   # the widest one is used

    def test_a_dollar_at_risk_counts_the_same_whoever_placed_it(self):
        st = scorecard.cluster_stats([bet(2, cost=10), bet(-1, cost=1)], {})
        self.assertAlmostEqual(st["roi"], 1 / 11, places=12)               # pooled: 1 / 11, not the mean of +20% and -100%
        self.assertAlmostEqual(st["se_iid"], 24 / 121, places=12)

    def test_a_missing_key_makes_the_bet_its_own_group(self):
        bets = [bet(1), bet(-1), bet(1), bet(-1)]
        st = scorecard.cluster_stats(bets, {"event": lambda b: None})
        self.assertAlmostEqual(st["se_by"]["event"], st["se_iid"], places=12)

    def test_one_group_gives_no_error_bar_and_an_empty_list_is_safe(self):
        st = scorecard.cluster_stats([bet(1, d="x"), bet(-1, d="x"), bet(1, d="x")], {"day": lambda b: b["d"]})
        self.assertEqual(st["se_by"]["day"], math.inf)
        self.assertEqual(st["se"], math.inf)
        empty = scorecard.cluster_stats([], {"day": lambda b: None})
        self.assertEqual((empty["n"], empty["se"], empty["se_by"]), (0, math.inf, {}))

    def test_a_single_bet_has_no_error_bar(self):
        self.assertEqual(scorecard.cluster_stats([bet(1)], {})["se_iid"], math.inf)


class BetsToDetect(unittest.TestCase):
    def test_the_formula(self):
        n = scorecard.bets_to_detect(1.0, 2.8, 0.03, 0.8416)
        self.assertEqual(n, math.ceil(((2.8 + 0.8416) / 0.03) ** 2))
        self.assertEqual(n, 14735)

    def test_a_smaller_spread_or_a_bigger_effect_needs_fewer_bets(self):
        base = scorecard.bets_to_detect(1.0, 2.8, 0.03, 0.8416)
        self.assertLess(scorecard.bets_to_detect(0.5, 2.8, 0.03, 0.8416), base)
        self.assertLess(scorecard.bets_to_detect(1.0, 2.8, 0.06, 0.8416), base)

    def test_nonsense_inputs_give_no_answer(self):
        for sd in (0.0, -1.0, math.inf, math.nan):
            self.assertIsNone(scorecard.bets_to_detect(sd, 2.8))
        self.assertIsNone(scorecard.bets_to_detect(1.0, 2.8, 0.0))

    def test_design_effect(self):
        self.assertAlmostEqual(scorecard.design_effect(0.3, 0.1), 9.0)
        self.assertEqual(scorecard.design_effect(0.1, 0.1), 1.0)
        self.assertEqual(scorecard.design_effect(0.05, 0.1), 1.0)           # never below 1
        self.assertEqual(scorecard.design_effect(0.1, 0.0), 1.0)
        self.assertEqual(scorecard.design_effect(math.inf, 0.1), 1.0)       # no usable error bar: the caller reports that instead


class ScoreRows(unittest.TestCase):
    def test_bets_that_share_an_event_make_the_range_wider_and_the_need_larger(self):
        independent = ledger([((1.0 if i % 2 else -1.0), {"event": f"e{i}", "opened": f"2026-11-{1 + i % 25:02d}T10:00:00+00:00"})
                              for i in range(40)])
        together = ledger([((1.0 if (i // 4) % 2 else -1.0), {"event": f"e{i // 4}", "opened": f"2026-11-{1 + i % 25:02d}T10:00:00+00:00"})
                           for i in range(40)])                              # same results, but four bets per event all alike
        a, b = scorecard.score("a", independent, 2.8), scorecard.score("b", together, 2.8)
        self.assertGreater(b["se"], a["se"])
        self.assertGreater(b["need"], a["need"])
        self.assertGreater(b["se_by"]["event"], b["se_iid"])

    def test_everything_on_one_day_cannot_be_judged(self):
        row = scorecard.score("x", ledger([((1.0 if i % 2 else -1.0), {"opened": "2026-10-28T09:00:00+00:00"}) for i in range(48)]), 2.8)
        self.assertIn("too clustered", row["verdict"])
        self.assertIsNone(row["lo"])

    def test_the_event_key_falls_back_to_the_market_and_the_trader_is_the_first_one_listed(self):
        self.assertEqual(scorecard.KEYS["event"]({"event": "e", "condition": "c"}), "e")
        self.assertEqual(scorecard.KEYS["event"]({"condition": "c"}), "c")
        self.assertIsNone(scorecard.KEYS["event"]({}))
        self.assertEqual(scorecard.KEYS["day"]({"opened": "2026-11-05T01:02:03+00:00"}), "2026-11-05")
        self.assertIsNone(scorecard.KEYS["day"]({}))
        self.assertEqual(scorecard.KEYS["trader"]({"traders": ["quqqq", "x"]}), "quqqq")
        self.assertIsNone(scorecard.KEYS["trader"]({"traders": []}))
        self.assertIsNone(scorecard.KEYS["trader"]({}))

    def test_one_trader_driving_most_bets_widens_the_range(self):
        rows = [((1.0 if (i // 5) % 2 else -1.0), {"traders": [f"t{i // 5}"]}) for i in range(40)]      # eight traders, each streaky
        row = scorecard.score("x", ledger(rows), 2.8)
        self.assertGreater(row["se_by"]["trader"], row["se_iid"])

    def test_the_return_is_per_dollar_at_risk_including_the_fee(self):
        led = {"cash": 50, "bets": [{"status": "won", "pnl": 0.9, "stake": 1.0, "fee": 0.1} for _ in range(10)]
               + [{"status": "lost", "pnl": -1.1, "stake": 1.0, "fee": 0.1} for _ in range(10)]}
        row = scorecard.score("x", led, 2.8)
        self.assertAlmostEqual(row["mean"], (9 - 11) / (22.0), places=12)   # -2.0 over 22 dollars at risk

    def test_the_text_explains_the_error_bars_and_the_target(self):
        built = scorecard.build({"a": ledger([(-1.0, {})] * 45 + [(0.3, {})] * 5), "b": ledger([])})
        text = scorecard.render(*built)
        self.assertIn("Bets to detect +3%", text)
        self.assertIn("grouped by event", text)
        self.assertIn("1 of 2 strategies have 20+ settled bets", text)
        self.assertIn("never called proven here", text)
        self.assertIn("Bets to detect", scorecard.render(*built, markdown=True))


class FalseWinnersAreRare(unittest.TestCase):
    def test_ten_fair_strategies_almost_never_look_proven(self):
        import random
        rnd = random.Random(7)
        proven = 0
        for _ in range(60):
            ledgers = {f"s{i}": ledger([((1.0 if rnd.random() < 0.5 else -1.0), {"event": f"e{i}-{j // 2}"}) for j in range(60)])
                       for i in range(10)}
            rows, z, _ = scorecard.build(ledgers)
            proven += sum(1 for r in rows if r["verdict"].startswith("PROVEN"))
        self.assertLessEqual(proven, 3)                                      # 600 fair strategies: about 600 * 0.05 / 10 = 3 expected at most


if __name__ == "__main__":
    unittest.main()
