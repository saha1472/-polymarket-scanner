"""The weather desk checks cash before it prices a bet (stake + a fixed 5 cents for the fee). With a bigger stake or
fee the real cost can exceed that, and the debit would take cash below zero. It now checks the real cost too.
No network: every call is faked.

Run:  python -m unittest discover -s tests -v
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import shadow  # noqa: E402
import weather  # noqa: E402

BUCKET = {"label": "5\u00b0C", "yes": "Y", "no": "N", "condition": "c", "question": "q"}


def fake_bet(fee):
    return {"strategy": "DEAD", "asset": "N", "condition": "c", "event": "ev", "title": "q", "outcome": "NO", "bucket": "5\u00b0C",
            "price": 0.95, "worst_price": 0.95, "shares": 1.05, "stake": 1.0, "fee": fee, "profit_if_win": 0.05, "order": "market", "audit": {}}


class CashGuard(unittest.TestCase):
    def setUp(self):
        self._old = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory()
        os.chdir(self._tmp.name)

    def tearDown(self):
        os.chdir(self._old)
        self._tmp.cleanup()

    def open(self, cash, fee):
        led = {"cash": cash, "bets": []}
        book = shadow.Book("s.json")
        old = weather.make_bet
        weather.make_bet = lambda *a: fake_bet(fee)
        try:
            weather.open_bets(led, [("DEAD", {"slug": "ev"}, BUCKET, "NO", "why")], book)
        finally:
            weather.make_bet = old
        return led, book

    def test_a_bet_whose_real_cost_exceeds_cash_is_not_placed(self):
        led, book = self.open(cash=1.10, fee=0.20)             # passes the early check (1.10 >= 1.05) but costs 1.20
        self.assertEqual((led["bets"], led["cash"]), ([], 1.10))
        self.assertEqual([(b["kind"], b["reason"]) for b in book.bets], [("skipped", "cash")])

    def test_a_bet_it_can_afford_is_placed_and_cash_never_goes_negative(self):
        led, book = self.open(cash=1.30, fee=0.20)
        self.assertEqual(len(led["bets"]), 1)
        self.assertAlmostEqual(led["cash"], 0.10, places=12)
        self.assertEqual(book.bets, [])

    def test_exactly_enough_cash_is_enough(self):
        led, _ = self.open(cash=1.20, fee=0.20)
        self.assertEqual(len(led["bets"]), 1)
        self.assertAlmostEqual(led["cash"], 0.0, places=12)


if __name__ == "__main__":
    unittest.main()
