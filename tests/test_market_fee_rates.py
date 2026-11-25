"""Each market's own taker fee is used by the weather desk (live temperature markets say 0.01, not the old 0.05 constant)
and by the copy scans' shadow records (they used the scan's constant while the real bets used the market's rate).
No network: every call is faked.

Run:  python -m unittest discover -s tests -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import signals  # noqa: E402
import weather  # noqa: E402

BOOK = {"asks": [{"price": "0.70", "size": "5"}, {"price": "0.80", "size": "100"}], "bids": [{"price": "0.60", "size": "50"}]}


def fee_by_hand(rate):
    """$2 walks 5 shares at 0.70 ($3.50 is more than $2, so only 2 / 0.70 shares are bought at the first level)."""
    shares = 2.0 / 0.70
    return shares * rate * 0.70 * 0.30


class WeatherFill(unittest.TestCase):
    def setUp(self):
        self.old = weather.get
        weather.get = lambda u, p=None, timeout=25: BOOK

    def tearDown(self):
        weather.get = self.old

    def test_the_markets_own_rate_is_charged(self):
        avg, shares, fee, _, _ = weather.fill("T", 2.0, 0.01)
        self.assertAlmostEqual(fee, fee_by_hand(0.01), places=12)
        self.assertAlmostEqual(shares, 2.0 / 0.70, places=12)

    def test_a_fee_free_market_pays_nothing_and_is_not_confused_with_unknown(self):
        self.assertEqual(weather.fill("T", 2.0, 0.0)[2], 0.0)

    def test_without_a_market_rate_the_desk_constant_is_the_fallback(self):
        self.assertAlmostEqual(weather.fill("T", 2.0)[2], fee_by_hand(weather.FEE_RATE), places=12)
        self.assertAlmostEqual(weather.fill("T", 2.0, None)[2], fee_by_hand(weather.FEE_RATE), places=12)


class WeatherBets(unittest.TestCase):
    def setUp(self):
        self.old = weather.get
        weather.get = lambda u, p=None, timeout=25: BOOK

    def tearDown(self):
        weather.get = self.old

    def bucket(self, **kw):
        return dict({"yes": "Y", "no": "N", "condition": "c", "question": "q", "label": "7\u00b0C"}, **kw)

    def test_a_bet_records_the_rate_it_was_charged_and_where_it_came_from(self):
        bet = weather.make_bet("DEAD", {"slug": "ev"}, self.bucket(fee=0.01), "YES", "t")
        self.assertEqual((bet["fee_rate"], bet["fee_src"]), (0.01, "market"))
        cheap = weather.make_bet("DEAD", {"slug": "ev"}, self.bucket(fee=0.01), "YES", "t")["fee"]
        dear = weather.make_bet("DEAD", {"slug": "ev"}, self.bucket(), "YES", "t")["fee"]
        self.assertAlmostEqual(dear / cheap, weather.FEE_RATE / 0.01, places=9)

    def test_a_market_that_does_not_say_uses_the_desk_constant_and_says_so(self):
        bet = weather.make_bet("DEAD", {"slug": "ev"}, self.bucket(), "YES", "t")
        self.assertEqual((bet["fee_rate"], bet["fee_src"]), (weather.FEE_RATE, "desk"))

    def test_a_fee_free_market_is_recorded_as_such(self):
        bet = weather.make_bet("DEAD", {"slug": "ev"}, self.bucket(fee=0.0), "YES", "t")
        self.assertEqual((bet["fee"], bet["fee_rate"], bet["fee_src"]), (0.0, 0.0, "market"))

    def test_the_bets_profit_if_it_wins_uses_the_charged_fee(self):
        bet = weather.make_bet("DEAD", {"slug": "ev"}, self.bucket(fee=0.01), "YES", "t")
        self.assertAlmostEqual(bet["profit_if_win"], bet["shares"] - bet["stake"] - bet["fee"], places=12)


class LoadEventsCarriesTheRate(unittest.TestCase):
    def test_every_bucket_gets_its_own_markets_rate(self):
        def market(label, **extra):
            return dict({"groupItemTitle": label, "clobTokenIds": '["y%s", "n%s"]' % (label, label), "conditionId": "c" + label,
                         "bestAsk": 0.5, "bestBid": 0.4, "question": "Will it be " + label + "?", "closed": False,
                         "description": "resolves on METAR (LTAC)"}, **extra)
        event = {"title": "Highest temperature in Ankara on November 24?", "slug": "ev", "endDate": "2026-11-25T00:00:00Z",
                 "markets": [market("13\u00b0C", feesEnabled=True, feeSchedule={"rate": 0.01, "exponent": 1, "takerOnly": True}),
                             market("14\u00b0C", feesEnabled=False),
                             market("15\u00b0C")]}
        pages = [[event], []]
        old = weather.get
        weather.get = lambda u, p=None, timeout=25: pages.pop(0) if pages else []
        try:
            events = weather.load_events()
        finally:
            weather.get = old
        self.assertEqual(len(events), 1)
        self.assertEqual([b["fee"] for b in events[0]["buckets"]], [0.01, 0.0, None])


class ShadowRecordsUseTheMarketsRate(unittest.TestCase):
    class FakeBook:
        def __init__(self):
            self.limits, self.takers = [], []

        def add_limit(self, scan, **kw):
            self.limits.append(kw)

        def add_taker(self, scan, reason, **kw):
            self.takers.append(kw)

        def resting(self):
            return []

        def check_limits(self, seen):
            return 0

    def test_limit_and_chase_records_carry_the_candidates_own_rate(self):
        cand = {"asset": "A", "condition": "c1", "title": "t", "outcome": "o", "ask_seen": 0.62, "avg_entry": 0.55, "traders": ["x"],
                "hours": 5, "event": "e", "reject": "chase"}
        old = signals.market_fee_rate
        signals.market_fee_rate = lambda s: (0.03, "market")
        try:
            book = self.FakeBook()
            signals.record_shadow_candidates(book, [cand], fetch=lambda ids: {})
        finally:
            signals.market_fee_rate = old
        self.assertEqual([k["fee_rate"] for k in book.limits], [0.03])
        self.assertEqual([k["fee_rate"] for k in book.takers], [0.03])

    def test_when_the_lookup_fails_the_scan_constant_still_applies(self):
        cand = {"asset": "A", "condition": None, "title": "t", "outcome": "o", "ask_seen": 0.62, "avg_entry": 0.55, "traders": ["x"],
                "hours": 5, "event": "e", "reject": "chase"}
        book = self.FakeBook()
        signals.record_shadow_candidates(book, [cand], fetch=lambda ids: {})
        self.assertEqual([k["fee_rate"] for k in book.limits], [signals.FEE_RATE])


if __name__ == "__main__":
    unittest.main()
