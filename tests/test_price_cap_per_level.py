"""The weather desk's upper price limits apply to the WORST order-book level an order has to reach, not to the average
price: an average under the limit used to hide a dear level. No network: every call is faked."""
import os
import sys
import tempfile
import unittest
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import shadow   # noqa: E402
import weather  # noqa: E402

BUCKET = {"label": "5°C", "yes": "Y", "no": "N", "condition": "c", "question": "q", "yes_bid": 0.80, "yes_ask": 0.90}
EVENT = {"slug": "ev", "tz": "UTC", "date": date(2026, 11, 26)}


def book(*levels):
    return {"asks": [{"price": str(p), "size": str(s)} for p, s in levels], "bids": []}


class Fill(unittest.TestCase):
    def setUp(self):
        self.old = weather.get
        self.book = book((0.90, 4), (0.99, 100))
        weather.get = lambda u, p=None, timeout=25: self.book

    def tearDown(self):
        weather.get = self.old

    def test_one_level_is_both_the_average_and_the_worst(self):
        avg, shares, fee, _, worst = weather.fill("T", 2.0)
        self.assertAlmostEqual(avg, 0.90)
        self.assertEqual(worst, 0.90)

    def test_an_order_that_reaches_a_dearer_level_reports_it(self):
        avg, shares, fee, _, worst = weather.fill("T", 5.0)        # $3.60 at 0.90, then the rest at 0.99
        self.assertEqual(worst, 0.99)
        self.assertLess(avg, 0.99)
        self.assertAlmostEqual(avg, 5.0 / (4 + 1.4 / 0.99))

    def test_exactly_using_the_first_level_stays_on_it(self):
        self.assertEqual(weather.fill("T", 3.6)[4], 0.90)

    def test_levels_outside_zero_to_one_are_skipped_and_never_the_worst(self):
        self.book = book((0.90, 1), (1.0, 100), (0.95, 100))
        self.assertEqual(weather.fill("T", 3.0)[4], 0.95)

    def test_a_book_that_cannot_fill_it_gives_nothing(self):
        self.book = book((0.90, 1))
        self.assertIsNone(weather.fill("T", 5.0))

    def test_the_bet_stores_the_worst_price(self):
        bet = weather.make_bet("DEAD", EVENT, BUCKET, "YES", "why")
        self.assertAlmostEqual(bet["price"], 0.90)
        self.assertEqual(bet["worst_price"], 0.90)


class Caps(unittest.TestCase):
    def setUp(self):
        self._old = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory()
        os.chdir(self._tmp.name)
        self._pause = weather.MANUAL_PAUSE
        weather.MANUAL_PAUSE = {}              # these tests check the price rules, not today's hand-pause list

    def tearDown(self):
        weather.MANUAL_PAUSE = self._pause
        os.chdir(self._old)
        self._tmp.cleanup()

    def run_open(self, strategy, price, worst, cash=50.0):
        bet = {"strategy": strategy, "asset": "Y", "condition": "c", "event": "ev", "title": "q", "outcome": "YES",
               "bucket": "5°C", "price": price, "worst_price": worst, "shares": 1.0 / price, "stake": 1.0,
               "fee": 0.0, "profit_if_win": 1.0 / price - 1.0, "order": "market", "audit": {}}
        led = {"cash": cash, "bets": []}
        bk = shadow.Book("s.json")
        old = weather.make_bet
        weather.make_bet = lambda *a: bet
        try:
            weather.open_bets(led, [(strategy, EVENT, BUCKET, "YES", "why")], bk)
        finally:
            weather.make_bet = old
        return led, bk

    def check(self, strategy, price, worst, placed, shadow_reason="price_filter"):
        led, bk = self.run_open(strategy, price, worst)
        self.assertEqual(len(led["bets"]), 1 if placed else 0, (strategy, price, worst))
        if not placed and shadow_reason:
            self.assertEqual([b["reason"] for b in bk.bets], [shadow_reason])
        if placed:
            self.assertEqual(bk.bets, [])

    # DEAD: at most 0.985
    def test_dead_an_average_under_the_limit_does_not_hide_a_dear_level(self):
        self.check("DEAD", 0.92, 0.99, placed=False)

    def test_dead_a_worst_level_exactly_at_the_limit_is_fine(self):
        self.check("DEAD", 0.92, weather.DEAD_MAX_PRICE, placed=True)

    def test_dead_everything_under_the_limit_is_placed(self):
        self.check("DEAD", 0.90, 0.95, placed=True)

    # LATE: band, upper limit on the worst level, lower limit on the average
    def test_late_a_dear_level_is_refused(self):
        self.check("LATE", 0.90, weather.LATE_MAX_PRICE + 0.01, placed=False)

    def test_late_the_limit_itself_is_allowed(self):
        self.check("LATE", 0.90, weather.LATE_MAX_PRICE, placed=True)

    def test_late_an_average_below_the_band_is_still_refused(self):
        self.check("LATE", weather.LATE_MIN_PRICE - 0.03, weather.LATE_MIN_PRICE, placed=False)

    def test_late_the_old_two_cent_margin_below_the_band_is_kept(self):
        self.check("LATE", weather.LATE_MIN_PRICE - 0.01, weather.LATE_MIN_PRICE, placed=True)

    # SHIFT: same, but nothing is recorded in shadow
    def test_shift_a_dear_level_is_refused_quietly(self):
        self.check("SHIFT", 0.50, weather.SHIFT_MAX_PRICE + 0.05, placed=False, shadow_reason=None)

    def test_shift_inside_the_band_is_placed(self):
        self.check("SHIFT", 0.50, weather.SHIFT_MAX_PRICE, placed=True)

    def test_shift_an_average_below_the_band_is_still_refused(self):
        self.check("SHIFT", weather.SHIFT_MIN_PRICE - 0.01, 0.30, placed=False, shadow_reason=None)

    # MORNING / AFTERNOON: at most FAV_MAX_FILL
    def test_favourites_a_dear_level_is_refused_and_recorded(self):
        for strat in ("MORNING", "AFTERNOON"):
            self.check(strat, 0.93, weather.FAV_MAX_FILL + 0.01, placed=False)

    def test_favourites_the_limit_itself_is_allowed(self):
        self.check("MORNING", 0.93, weather.FAV_MAX_FILL, placed=True)


class EndToEnd(unittest.TestCase):
    """A bigger stake against a real-looking book: the first level is cheap and thin, the next is dear."""

    def setUp(self):
        self._old = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory()
        os.chdir(self._tmp.name)
        self.old_get, self.old_size = weather.get, weather.BET_SIZE
        weather.get = lambda u, p=None, timeout=25: book((0.90, 4), (0.99, 100))
        weather.BET_SIZE = 5.0

    def tearDown(self):
        weather.get, weather.BET_SIZE = self.old_get, self.old_size
        os.chdir(self._old)
        self._tmp.cleanup()

    def test_dead_is_not_placed_when_the_order_has_to_reach_0_99(self):
        led = {"cash": 50.0, "bets": []}
        bk = shadow.Book("s.json")
        weather.open_bets(led, [("DEAD", EVENT, BUCKET, "YES", "why")], bk)
        self.assertEqual(led["bets"], [])
        rec = bk.bets[0]
        self.assertEqual(rec["reason"], "price_filter")
        self.assertLess(rec["price"], weather.DEAD_MAX_PRICE)          # the average alone would have passed

    def test_the_same_book_at_a_small_stake_is_placed(self):
        weather.BET_SIZE = 2.0
        led = {"cash": 50.0, "bets": []}
        weather.open_bets(led, [("DEAD", EVENT, BUCKET, "YES", "why")], shadow.Book("s.json"))
        self.assertEqual(len(led["bets"]), 1)
        self.assertEqual(led["bets"][0]["worst_price"], 0.90)


if __name__ == "__main__":
    unittest.main()
