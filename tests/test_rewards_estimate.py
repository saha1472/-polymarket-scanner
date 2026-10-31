"""Offline tests for rewards_estimate.py (no network)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import rewards_estimate as re_  # noqa: E402


def book(bids, asks, tick="0.01", min_order="5"):
    return {"bids": [{"price": str(p), "size": str(s)} for p, s in bids],
            "asks": [{"price": str(p), "size": str(s)} for p, s in asks],
            "tick_size": tick, "min_order_size": min_order}


class Scoring(unittest.TestCase):
    def test_order_score_formula(self):
        # 2c from mid with a 4c max spread: ((4-2)/4)^2 x 100 = 25
        self.assertAlmostEqual(re_.order_score(0.48, 100, 0.50, 4), 25.0)
        self.assertAlmostEqual(re_.order_score(0.50, 10, 0.50, 4), 10.0)   # at mid
        self.assertEqual(re_.order_score(0.45, 100, 0.50, 4), 0.0)         # outside v

    def test_side_total_ignores_levels_below_min_size(self):
        lv = [(0.49, 10), (0.48, 50)]
        self.assertAlmostEqual(re_.side_total(lv, 0.50, 4, 20),
                               re_.order_score(0.48, 50, 0.50, 4))

    def test_q_min_single_sided_rule(self):
        self.assertAlmostEqual(re_.q_min(30, 0, 0.5), 10.0)    # one side / 3
        self.assertAlmostEqual(re_.q_min(30, 20, 0.5), 20.0)   # two-sided min wins
        self.assertEqual(re_.q_min(30, 0, 0.95), 0.0)          # outside 0.10-0.90

    def test_share_bounds(self):
        lo, hi = re_.share_range(10, 30, 10)        # others between 10 and 20
        self.assertAlmostEqual(lo, 10 / 30)
        self.assertAlmostEqual(hi, 10 / 20)
        self.assertEqual(re_.share_range(0, 5, 5), (0.0, 0.0))

    def test_adjusted_mid_skips_small_levels(self):
        bids, asks = [(0.49, 1), (0.45, 50)], [(0.51, 1), (0.55, 50)]
        self.assertAlmostEqual(re_.adjusted_mid(bids, asks, 20), 0.50)
        self.assertAlmostEqual(re_.adjusted_mid([(0.40, 5)], [(0.60, 5)], 20), 0.50)
        self.assertIsNone(re_.adjusted_mid([], asks, 20))

    def test_placement_join_and_back(self):
        bids, asks = [(0.48, 50)], [(0.52, 50)]
        self.assertEqual(re_.placement(bids, asks, 20, 0, 0.01), (0.48, 0.52))
        self.assertEqual(re_.placement(bids, asks, 20, 1, 0.01), (0.47, 0.53))
        self.assertEqual(re_.placement([(0.384, 60)], [(0.389, 50)], 50, 1, 0.001),
                         (0.374, 0.399))

    def test_levels_drops_bad_rows(self):
        b = {"bids": [{"price": "nan", "size": "5"}, {"price": "0.4", "size": "0"},
                      {"price": "0.3", "size": "5"}, {"price": "x"}]}
        self.assertEqual(re_.levels(b, "bids"), [(0.3, 5.0)])


class Evaluate(unittest.TestCase):
    def test_empty_market_gets_full_pool(self):
        e = re_.evaluate(book([(0.48, 1)], [(0.52, 1)]), 10, 4, 20)
        j = e["placements"]["join"]
        self.assertEqual(e["others_q"], 0)
        self.assertEqual(j["share"], (1.0, 1.0))
        self.assertAlmostEqual(j["usd_day"][1], 10.0)
        self.assertAlmostEqual(j["capital"], 20 * 0.48 + 20 * 0.48)

    def test_join_beats_back_and_numbers_match_by_hand(self):
        e = re_.evaluate(book([(0.48, 100)], [(0.52, 100)]), 10, 4, 20)
        j, b = e["placements"]["join"], e["placements"]["back"]
        # others: 2c from mid each side -> 25 per side, Q1 + Q2 = 50
        self.assertAlmostEqual(e["others_q"], 50)
        # join: 20 shares at 2c -> 5 per side, two-sided Q_min 5
        self.assertAlmostEqual(j["score"], 5.0)
        self.assertAlmostEqual(j["share"][0], 5 / (5 + 25))
        self.assertAlmostEqual(j["share"][1], 5 / (5 + 12.5))
        # back: 3c from mid -> ((4-3)/4)^2 x 20 = 1.25
        self.assertAlmostEqual(b["score"], 1.25)
        self.assertLess(b["usd_day"][1], j["usd_day"][0])
        self.assertEqual((j["ahead_bid"], j["ahead_ask"]), (100, 100))
        self.assertEqual((b["ahead_bid"], b["ahead_ask"]), (100, 100))

    def test_one_sided_book_returns_none(self):
        self.assertIsNone(re_.evaluate(book([(0.48, 100)], []), 10, 4, 20))

    def test_uses_market_min_order_when_larger(self):
        e = re_.evaluate(book([(0.48, 100)], [(0.52, 100)], min_order="30"), 10, 4, 20)
        self.assertEqual(e["size"], 30)


class Helpers(unittest.TestCase):
    def test_days_until(self):
        from datetime import datetime, timezone
        now = datetime(2026, 11, 1, tzinfo=timezone.utc)
        self.assertAlmostEqual(re_.days_until("2026-11-04T00:00:00Z", now), 3.0)
        self.assertLess(re_.days_until("2026-10-31T00:00:00Z", now), 0)
        self.assertIsNone(re_.days_until(None, now))
        self.assertIsNone(re_.days_until("soon", now))

    def test_average_of_snapshots(self):
        a = re_.evaluate(book([(0.48, 100)], [(0.52, 100)]), 10, 4, 20)
        b = re_.evaluate(book([(0.48, 1)], [(0.52, 1)]), 10, 4, 20)    # empty book
        avg = re_.average([a, None, b])
        j = avg["placements"]["join"]
        self.assertEqual(avg["samples"], 2)
        self.assertAlmostEqual(j["usd_day"][1],
                               (a["placements"]["join"]["usd_day"][1] + 10.0) / 2)
        self.assertEqual(j["usd_day_range"],
                         (a["placements"]["join"]["usd_day"][0], 10.0))
        self.assertIsNone(re_.average([None, None]))


if __name__ == "__main__":
    unittest.main()
