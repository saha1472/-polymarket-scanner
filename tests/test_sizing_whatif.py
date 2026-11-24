"""What bigger orders would have filled at, from the order book each bet stored (offline). Hand-worked numbers.

Run:  python -m unittest discover -s tests -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sizing_report  # noqa: E402

ASKS = [[0.50, 4.0], [0.60, 100.0]]


def bet(status="won", asks=ASKS, fee_rate=0.0, **extra):
    return dict({"status": status, "pnl": 1.0 if status == "won" else -1.0, "stake": 1.0, "fee": 0.0, "price": 0.5, "shares": 2.0,
                 "fee_rate": fee_rate, "opened": "2026-11-01T10:00:00+00:00", "audit": {"book": {"asks": asks, "bids": []}}}, **extra)


class WalkAsks(unittest.TestCase):
    def test_a_small_order_fills_at_the_best_ask(self):
        shares, spent, fee = sizing_report.walk_asks(ASKS, 1.0, 0.0)
        self.assertAlmostEqual((shares, spent, fee), (2.0, 1.0, 0.0), places=12)

    def test_a_bigger_order_walks_up_the_book(self):
        # $2 at 0.50 takes all 4 shares (cost 2.0): exactly the first level
        self.assertAlmostEqual(sizing_report.walk_asks(ASKS, 2.0, 0.0)[0], 4.0, places=12)
        # $3 = 4 shares at 0.50 ($2) + $1 at 0.60 (1/0.6 shares)
        shares, spent, _ = sizing_report.walk_asks(ASKS, 3.0, 0.0)
        self.assertAlmostEqual(shares, 4.0 + 1.0 / 0.6, places=12)
        self.assertAlmostEqual(spent, 3.0, places=12)

    def test_fee_is_charged_per_level(self):
        _, _, fee = sizing_report.walk_asks(ASKS, 3.0, 0.1)
        self.assertAlmostEqual(fee, 4.0 * 0.1 * 0.5 * 0.5 + (1.0 / 0.6) * 0.1 * 0.6 * 0.4, places=12)

    def test_an_order_the_stored_levels_cannot_absorb_is_not_filled(self):
        self.assertIsNone(sizing_report.walk_asks([[0.50, 1.0]], 5.0, 0.0))
        self.assertIsNone(sizing_report.walk_asks([], 1.0, 0.0))
        self.assertIsNone(sizing_report.walk_asks([[float("nan"), 5.0], [1.0, 5.0], [0.5, 0.0]], 1.0, 0.0))

    def test_broken_levels_are_skipped_not_traded_on(self):
        shares, spent, _ = sizing_report.walk_asks([[0.0, 10.0], [0.4, -3.0], [float("nan"), 2.0], [0.5, 4.0]], 1.0, 0.0)
        self.assertAlmostEqual((shares, spent), (2.0, 1.0), places=12)                       # only the 0.50 level counts

    def test_levels_are_taken_best_first_whatever_their_order(self):
        a = sizing_report.walk_asks([[0.60, 100.0], [0.50, 4.0]], 3.0, 0.0)
        self.assertAlmostEqual(a[0], 4.0 + 1.0 / 0.6, places=12)


class WhatIf(unittest.TestCase):
    def test_a_winner_pays_one_a_share_and_a_loser_nothing(self):
        w = sizing_report.whatif(bet("won"), 3.0)
        self.assertAlmostEqual(w["pnl"], (4.0 + 1.0 / 0.6) - 3.0, places=12)
        self.assertAlmostEqual(w["cost"], 3.0, places=12)
        self.assertAlmostEqual(w["slip"], 3.0 / (4.0 + 1.0 / 0.6) - 0.50, places=12)
        self.assertAlmostEqual(sizing_report.whatif(bet("lost"), 3.0)["pnl"], -3.0, places=12)

    def test_the_fee_is_part_of_the_cost(self):
        w = sizing_report.whatif(bet("lost", fee_rate=0.1), 1.0)
        fee = 2.0 * 0.1 * 0.5 * 0.5
        self.assertAlmostEqual(w["cost"], 1.0 + fee, places=12)
        self.assertAlmostEqual(w["pnl"], -(1.0 + fee), places=12)

    def test_slip_is_measured_from_the_best_usable_ask(self):
        self.assertEqual(sizing_report.whatif(bet(asks=[[0.0, 50.0], [0.5, 100.0]]), 1.0)["slip"], 0.0)
        self.assertGreater(sizing_report.whatif(bet(), 3.0)["slip"], 0.0)                    # walking past the first level costs something

    def test_odd_data_is_dropped_not_a_crash(self):
        text_levels = [[0.40, 10], ["0.5", "20"], [0.5], None, "x", [None, 3]]
        self.assertEqual(sizing_report.levels(text_levels), [(0.4, 10.0), (0.5, 20.0)])
        self.assertEqual(sizing_report.levels(None), [])
        self.assertEqual(sizing_report.levels("asks"), [])
        for audit in ("missing", None, {"book": "missing"}, {"book": {"asks": "x"}}, {"book": None}):
            self.assertIsNone(sizing_report.whatif(dict(bet(), audit=audit), 1.0))
        self.assertIsNotNone(sizing_report.whatif(bet(asks=[[0.40, 10], ["0.5", "20"]]), 1.0))

    def test_a_recorded_empty_book_is_a_book_that_fills_nothing(self):
        rows = sizing_report.whatif_rows({"bets": [bet(asks=[]), bet()]}, sizes=(1.0,))
        self.assertEqual((rows[0]["books"], rows[0]["filled"]), (2, 1))
        self.assertIsNone(sizing_report.whatif(bet(asks=[]), 1.0))

    def test_a_tiny_order_the_book_cannot_cover_is_not_filled(self):
        self.assertIsNone(sizing_report.walk_asks([[0.5, 2e-12]], 1e-9, 0.0))
        self.assertIsNotNone(sizing_report.walk_asks([[0.5, 2e-6]], 1e-9, 0.0))

    def test_no_book_or_not_a_plain_win_or_loss_gives_nothing(self):
        self.assertIsNone(sizing_report.whatif(dict(bet(), audit={}), 1.0))
        self.assertIsNone(sizing_report.whatif(bet("stopped"), 1.0))
        self.assertIsNone(sizing_report.whatif(bet(asks=[[0.5, 1.0]]), 5.0))


class Rows(unittest.TestCase):
    def test_rows_count_books_and_fills_and_pool_the_return(self):
        bets = [bet("won"), bet("lost"), bet("won"), dict(bet("won"), audit={})]
        rows = sizing_report.whatif_rows({"bets": bets}, sizes=(1.0, 5.0))
        self.assertEqual([(r["size"], r["books"], r["filled"]) for r in rows], [(1.0, 3, 3), (5.0, 3, 3)])
        self.assertAlmostEqual(rows[0]["roi"], (1.0 + -1.0 + 1.0) / 3.0, places=12)       # a win returns +100% at 0.50, a loss -100%
        self.assertGreater(rows[1]["slip"], rows[0]["slip"])                                 # a bigger order pays more above the best ask
        self.assertEqual(rows[0]["slip"], 0.0)

    def test_the_return_is_pooled_over_dollars_at_risk(self):
        # a win costing 1.00 (+1.00) and a loss costing 1.25 with fee (-1.25): pooled -0.25 / 2.25, not the mean of +100% and -100%
        rows = sizing_report.whatif_rows({"bets": [bet("won"), bet("lost", fee_rate=0.5)]}, sizes=(1.0,))
        self.assertAlmostEqual(rows[0]["roi"], -0.25 / 2.25, places=12)

    def test_sizes_are_compared_on_the_same_bets(self):
        # the winner fills at $1 and $100, the loser only at $1: the $100 row must not show +100% from the winner alone
        win, lose = bet("won", asks=[[0.50, 400.0]]), bet("lost", asks=[[0.50, 4.0]])
        rows = sizing_report.whatif_rows({"bets": [win, lose]}, sizes=(1.0, 100.0))
        self.assertEqual([(r["books"], r["filled"], r["same"]) for r in rows], [(2, 2, 1), (2, 1, 1)])
        self.assertAlmostEqual(rows[0]["roi"], rows[1]["roi"], places=12)                     # both rows use the winner only

    def test_an_order_too_big_for_the_stored_book_counts_as_not_filled(self):
        small = [[0.50, 6.0]]                                                                # room for $3 only
        rows = sizing_report.whatif_rows({"bets": [bet(asks=small)] * 2}, sizes=(1.0, 5.0))
        self.assertEqual([(r["books"], r["filled"]) for r in rows], [(2, 2), (2, 0)])
        self.assertIsNone(rows[1]["roi"])
        self.assertIsNone(rows[1]["slip"])

    def test_the_report_has_the_table_only_when_there_are_enough_books(self):
        few = sizing_report.render({"x": {"bets": [bet() for _ in range(3)]}}, paths=5, n_bets=5)
        self.assertIn("No ledger has 10+ settled bets with a stored order book yet.", few)
        many = {"bets": [dict(bet("won" if i % 2 else "lost"), opened=f"2026-11-{1 + i % 25:02d}T10:00:00+00:00", event=f"e{i}") for i in range(30)]}
        text = sizing_report.render({"x": many}, paths=5, n_bets=5)
        self.assertIn("Estimated fills of bigger orders", text)
        self.assertIn("$2.5", text)
        self.assertNotIn("No ledger has 10+", text)


if __name__ == "__main__":
    unittest.main()
