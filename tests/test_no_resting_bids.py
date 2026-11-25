"""The weather desk no longer posts resting (maker) bids: none of 273 ever filled under the snapshot fill proxy.
A resting bid left in a ledger from before is cancelled and its cash returned. No network: every call is faked."""
import inspect
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import shadow       # noqa: E402
import watch_books  # noqa: E402
import weather      # noqa: E402

BUCKET = {"label": "5°C", "yes": "Y", "no": "N", "condition": "c", "question": "q", "yes_bid": 0.80, "yes_ask": 0.97}
EVENT = {"slug": "ev", "tz": "UTC", "date": __import__("datetime").date(2026, 11, 26)}
RESTING = {"strategy": "MORNING", "status": "resting", "asset": "A", "condition": "cx", "event": "e", "title": "t",
           "outcome": "YES", "bucket": "5°C", "price": 0.8, "shares": 1.25, "stake": 1.0, "fee": 0.0,
           "expires": "2099-01-01T00:00:00+00:00"}


class InTempDir(unittest.TestCase):
    def setUp(self):
        self._old = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory()
        os.chdir(self._tmp.name)

    def tearDown(self):
        os.chdir(self._old)
        self._tmp.cleanup()


def expensive_bet():
    return {"strategy": "MORNING", "asset": "Y", "condition": "c", "event": "ev", "title": "q", "outcome": "YES",
            "bucket": "5°C", "price": 0.97, "shares": 1.03, "stake": 1.0, "fee": 0.0, "profit_if_win": 0.03,
            "order": "market", "audit": {}}


class NothingIsPosted(InTempDir):
    def run_open(self, bet, cash=50.0):
        led = {"cash": cash, "bets": []}
        book = shadow.Book("s.json")
        old = weather.make_bet
        weather.make_bet = lambda *a: bet
        try:
            msgs = weather.open_bets(led, [("MORNING", EVENT, BUCKET, "YES", "why")], book)
        finally:
            weather.make_bet = old
        return led, book, msgs

    def test_a_favourite_priced_above_the_fill_limit_is_skipped_not_bid_for(self):
        led, book, msgs = self.run_open(expensive_bet())
        self.assertEqual((led["bets"], led["cash"], msgs), ([], 50.0, []))
        self.assertEqual([(b["kind"], b["reason"], b["price"]) for b in book.bets], [("skipped", "price_filter", 0.97)])

    def test_no_book_at_all_places_nothing_and_records_nothing(self):
        led, book, msgs = self.run_open(None)
        self.assertEqual((led["bets"], led["cash"], msgs, book.bets), ([], 50.0, [], []))

    def test_an_affordable_favourite_is_still_bought_at_the_ask(self):
        cheap = dict(expensive_bet(), price=0.90, shares=1.11, profit_if_win=0.11)
        led, book, msgs = self.run_open(cheap)
        self.assertEqual([b["status"] for b in led["bets"]], ["open"])
        self.assertAlmostEqual(led["cash"], 49.0)

    def test_the_old_functions_are_gone(self):
        self.assertFalse(hasattr(weather, "resting_bid"))
        self.assertFalse(hasattr(weather, "check_resting"))


class LeftoverBids(unittest.TestCase):
    def test_a_leftover_resting_bid_is_cancelled_and_its_cash_returned(self):
        led = {"cash": 49.0, "bets": [dict(RESTING)]}
        msgs = weather.release_resting(led)
        self.assertEqual((led["bets"][0]["status"], led["cash"]), ("cancelled", 50.0))
        self.assertEqual(len(msgs), 1)
        self.assertIn("1 leftover", msgs[0])

    def test_it_happens_once(self):
        led = {"cash": 49.0, "bets": [dict(RESTING)]}
        weather.release_resting(led)
        self.assertEqual(weather.release_resting(led), [])
        self.assertEqual(led["cash"], 50.0)

    def test_several_bids_and_other_bets_untouched(self):
        other = {"status": "open", "stake": 1.0, "asset": "O"}
        old_cancelled = {"status": "cancelled", "stake": 1.0, "asset": "C"}
        led = {"cash": 47.0, "bets": [dict(RESTING), other, dict(RESTING, asset="B"), old_cancelled]}
        weather.release_resting(led)
        self.assertEqual([b["status"] for b in led["bets"]], ["cancelled", "open", "cancelled", "cancelled"])
        self.assertEqual(led["cash"], 49.0)                      # only the two resting stakes come back

    def test_nothing_to_release_says_nothing(self):
        led = {"cash": 50.0, "bets": [{"status": "open", "stake": 1.0}]}
        self.assertEqual((weather.release_resting(led), led["cash"]), ([], 50.0))

    def test_the_scan_releases_before_it_trades(self):
        src = inspect.getsource(weather)
        self.assertIn("release_resting(led) + settle(led)", src)


class Summary(unittest.TestCase):
    def test_the_summary_has_no_resting_or_unfilled_part(self):
        led = {"cash": 40.0, "bets": [{"status": "cancelled", "stake": 1.0}, dict(RESTING),
                                      {"status": "won", "pnl": 0.5, "stake": 1.0}, {"status": "open", "stake": 1.0}]}
        text = weather.summary(led)
        self.assertNotIn("resting", text)
        self.assertNotIn("unfilled", text)
        self.assertIn("open 1", text)
        self.assertIn("record 1-0", text)


class Watcher(InTempDir):
    def test_the_watcher_leaves_weather_ledgers_alone_and_asks_for_no_weather_books(self):
        led = {"cash": 49.0, "bets": [dict(RESTING)]}
        with open("ledger_wx_morning.json", "w") as fh:
            json.dump(led, fh)
        asked = []

        def fetch(tokens):
            asked.append(set(tokens))
            return {}
        watch_books.main(seconds=watch_books.POLL_SECONDS + 1, sleep=lambda s: None, fetch=fetch)
        self.assertEqual(asked, [])                                # nothing is waiting for a fill: no look at all
        with open("ledger_wx_morning.json") as fh:
            self.assertEqual(json.load(fh), led)

    def test_shadow_limit_bids_are_still_watched(self):
        b = shadow.Book("shadow_sports.json")
        b.add_limit("SPORTS", asset="L", limit_price=0.5, best_ask=0.6, fee_rate=0.05, condition="c", title="t")
        b.save()
        asked = []

        def fetch(tokens):
            asked.append(set(tokens))
            return {"L": {"asks": [{"price": "0.50", "size": "9"}], "bids": []}}
        watch_books.main(seconds=watch_books.POLL_SECONDS + 1, sleep=lambda s: None, fetch=fetch)
        self.assertEqual(asked[0], {"L"})
        self.assertEqual(shadow.Book("shadow_sports.json").bets[0]["status"], "open")

    def test_the_watcher_no_longer_imports_the_weather_desk(self):
        self.assertNotIn("import weather", inspect.getsource(watch_books))


if __name__ == "__main__":
    unittest.main()
