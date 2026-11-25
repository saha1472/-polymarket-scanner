"""Offline tests: a copy-scan signal is re-priced on a fresh order book just before it is booked."""
import inspect
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
_argv = sys.argv
sys.argv = ["signals.py", "ESPORTS"]           # signals.py reads its scan from argv
import signals                                  # noqa: E402
sys.argv = _argv
signals.MANUAL_PAUSE = {}

GAMMA = [{"feesEnabled": True, "feeSchedule": {"rate": 0.05, "exponent": 1}}]


def book(ask, size="100", min_order="5"):
    return {"asks": [{"price": ask, "size": size}], "bids": [{"price": "0.50", "size": "100"}],
            "min_order_size": min_order, "timestamp": "1700000000000"}


class Patched(unittest.TestCase):
    def setUp(self):
        self._old = (signals.get, signals.TRADER_PAUSE_LIVE, signals.SCAN, signals.REAL_STOP)
        signals.FEE_CACHE.clear()
        self.now = book("0.60")          # what the next order book fetch answers
        self.fetches = 0

        def get(url, params=None, raw=False):
            if url == signals.BOOK_URL:
                self.fetches += 1
                return self.now
            if url == f"{signals.GAMMA_API}/markets":
                return GAMMA
            return None
        signals.get = get

    def tearDown(self):
        signals.get, signals.TRADER_PAUSE_LIVE, signals.SCAN, signals.REAL_STOP = self._old
        signals.FEE_CACHE.clear()

    def sized(self, n=1, event=None, traders=("A",)):
        """A signal sized the way main() does it, on the book as it is now."""
        s = {"asset": f"T{n}", "condition": f"C{n}", "title": f"title {n}", "outcome": "Yes",
             "event": event or f"e{n}", "hours": 5.0, "price": 0.60, "avg_entry": 0.58,
             "traders": list(traders), "holders": {"0xa": {"name": traders[0], "size": 9.0}}}
        got = signals.size_bet(s)
        self.assertIsNotNone(got)
        return got


class RePricing(Patched):
    def test_without_refresh_the_sized_price_is_booked_as_before(self):
        s = self.sized()
        self.now = book("0.65")
        led = {"cash": 50.0, "bets": []}
        signals.open_paper_bets(led, [s])
        self.assertEqual(led["bets"][0]["price"], 0.60)

    def test_the_fresh_book_sets_price_size_fee_and_audit(self):
        s = self.sized()
        self.now = book("0.65")
        led = {"cash": 50.0, "bets": []}
        signals.open_paper_bets(led, [s], refresh=signals.size_bet)
        bet = led["bets"][0]
        self.assertEqual(bet["price"], 0.65)
        self.assertAlmostEqual(bet["stake"], 1.0)                  # 5 shares would cost over $1: a $1 market order
        self.assertAlmostEqual(bet["shares"], 1.0 / 0.65)
        self.assertAlmostEqual(bet["fee"], signals.fees.fee_amount(1.0 / 0.65, 0.65, 0.05))
        self.assertAlmostEqual(led["cash"], 50.0 - bet["stake"] - bet["fee"])

    def test_the_audit_keeps_the_book_the_bet_was_booked_on(self):
        s = self.sized()
        self.now = dict(book("0.65"), timestamp="1700000099000")
        led = {"cash": 50.0, "bets": []}
        signals.open_paper_bets(led, [s], refresh=signals.size_bet)
        self.assertEqual(led["bets"][0]["audit"]["book"]["timestamp"], "1700000099000")

    def test_a_cheaper_fresh_price_is_taken(self):
        s = self.sized()
        self.now = book("0.55")
        led = {"cash": 50.0, "bets": []}
        signals.open_paper_bets(led, [s], refresh=signals.size_bet)
        self.assertEqual(led["bets"][0]["price"], 0.55)

    def test_the_other_signal_fields_survive(self):
        s = self.sized(traders=("A", "B"))
        s["flow"] = {"flags": ["x"]}
        led = {"cash": 50.0, "bets": []}
        signals.open_paper_bets(led, [s], refresh=signals.size_bet)
        bet = led["bets"][0]
        self.assertEqual((bet["traders"], bet["event"], bet["flow"]), (["A", "B"], "e1", {"flags": ["x"]}))
        self.assertIn("0xa", bet["holders"])


class NotBooked(Patched):
    def check_not_booked(self, why):
        s = self.sized()
        before = dict(s)
        self.now = why
        led = {"cash": 50.0, "bets": []}
        signals.open_paper_bets(led, [s], refresh=signals.size_bet)
        self.assertEqual(led["bets"], [])
        self.assertEqual(led["cash"], 50.0)
        self.assertEqual(s, before)                       # the failed re-check left the signal alone

    def test_the_price_ran_out_of_the_band(self):
        self.check_not_booked(book("0.95"))

    def test_the_price_ran_past_the_traders_entry(self):
        self.check_not_booked(book("0.69"))               # entry 0.58, MAX_CHASE 0.10

    def test_the_liquidity_is_gone(self):
        self.check_not_booked(book("0.60", size="1"))         # a $1 order needs 1.67 shares

    def test_the_book_is_empty(self):
        self.check_not_booked({"asks": [], "bids": []})

    def test_the_book_fetch_failed(self):
        self.check_not_booked(None)

    def test_the_return_is_too_thin_after_the_fee(self):
        self.check_not_booked(book("0.99"))

    def test_the_next_signal_is_still_booked(self):
        a, b = self.sized(1), self.sized(2)
        answers = [book("0.95"), book("0.62")]           # the first one moved away, the second did not
        signals.get = lambda url, params=None, raw=False: (
            answers.pop(0) if url == signals.BOOK_URL else GAMMA)
        led = {"cash": 50.0, "bets": []}
        signals.open_paper_bets(led, [a, b], refresh=signals.size_bet)
        self.assertEqual([x["asset"] for x in led["bets"]], ["T2"])
        self.assertEqual(led["bets"][0]["price"], 0.62)


class OnlyWhatWouldBeBooked(Patched):
    def test_no_fetch_for_signals_that_fail_a_cap(self):
        a, b = self.sized(1, event="same"), self.sized(2, event="same")   # one bet per event
        led = {"cash": 50.0, "bets": []}
        self.fetches = 0
        signals.open_paper_bets(led, [a, b], refresh=signals.size_bet)
        self.assertEqual(len(led["bets"]), 1)
        self.assertEqual(self.fetches, 1)

    def test_no_fetch_while_the_scan_is_paused_by_hand(self):
        s = self.sized()
        self.fetches = 0
        old = signals.MANUAL_PAUSE
        signals.MANUAL_PAUSE = {signals.SCAN: "test"}
        try:
            led = {"cash": 50.0, "bets": []}
            signals.open_paper_bets(led, [s], refresh=signals.size_bet)
        finally:
            signals.MANUAL_PAUSE = old
        self.assertEqual((self.fetches, led["bets"]), (0, []))

    def test_a_held_market_is_not_fetched(self):
        s = self.sized()
        held = {"status": "open", "asset": s["asset"], "condition": s["condition"], "traders": ["Z"]}
        self.fetches = 0
        signals.open_paper_bets({"cash": 50.0, "bets": [held]}, [s], refresh=signals.size_bet)
        self.assertEqual(self.fetches, 0)


class CashUsesTheFreshCost(Patched):
    # The stake is a fixed $1 here, so only the fee moves: 0.0200 at 0.60, 0.0250 at 0.50.
    def test_a_fresh_cost_that_no_longer_fits_the_cash_is_skipped(self):
        s = self.sized()
        self.assertAlmostEqual(s["stake"] + s["fee"], 1.02)
        self.now = book("0.50")
        led = {"cash": 1.022, "bets": []}                 # enough for the old cost, not the new 1.025
        signals.open_paper_bets(led, [s], refresh=signals.size_bet)
        self.assertEqual(led["bets"], [])
        self.assertEqual(led["cash"], 1.022)

    def test_the_fresh_cost_is_what_is_debited(self):
        s = self.sized()
        self.now = book("0.50")
        led = {"cash": 10.0, "bets": []}
        signals.open_paper_bets(led, [s], refresh=signals.size_bet)
        bet = led["bets"][0]
        self.assertAlmostEqual(bet["fee"], 0.025)
        self.assertAlmostEqual(led["cash"], 10.0 - 1.025)


class Wiring(unittest.TestCase):
    def test_main_asks_for_the_fresh_price(self):
        src = inspect.getsource(signals.main)
        self.assertIn("open_paper_bets(ledger, signals, book, refresh=size_bet)", src)


if __name__ == "__main__":
    unittest.main()
