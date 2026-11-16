"""Offline tests for per-market fees (fees.py + signals.py) and the fee-free geopolitics scan."""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
_argv = sys.argv
sys.argv = ["signals.py", "SPORTS"]            # signals.py reads its scan from argv
import signals                                  # noqa: E402
sys.argv = _argv
import fees                                     # noqa: E402
import heartbeat                                # noqa: E402

BOOK = {"asks": [{"price": "0.60", "size": "100"}], "bids": [{"price": "0.58", "size": "100"}],
        "min_order_size": "5"}


def fake_get(gamma_rows, calls=None):
    def get(url, params=None, raw=False):
        if calls is not None:
            calls.append(url)
        if url == signals.BOOK_URL:
            return BOOK
        if url == f"{signals.GAMMA_API}/markets":
            return gamma_rows
        return None
    return get


class Patched(unittest.TestCase):
    """Swap signals.get / signals.CAT for one test and put everything back afterwards."""

    def setUp(self):
        self._old = (signals.get, signals.CAT, signals.REAL_STOP, signals.FEE_RATE)
        signals.FEE_CACHE.clear()

    def tearDown(self):
        signals.get, signals.CAT, signals.REAL_STOP, signals.FEE_RATE = self._old
        signals.FEE_CACHE.clear()

    def signal(self):
        return {"asset": "A", "condition": "C1", "avg_entry": 0.60}


class FeeMath(unittest.TestCase):
    def test_reading_a_markets_own_schedule(self):
        sched = lambda **k: {"feesEnabled": True, "feeSchedule": dict({"rate": 0.03, "exponent": 1}, **k)}  # noqa: E731
        self.assertEqual(fees.market_fee(sched()), {"rate": 0.03, "exp": 1.0, "enabled": True})
        self.assertEqual(fees.market_fee({"feesEnabled": False}), {"rate": 0.0, "exp": 1.0, "enabled": False})
        self.assertEqual(fees.market_fee({"feesEnabled": False, "feeSchedule": {"rate": 0.07}})["rate"], 0.0)
        self.assertEqual(fees.market_fee({"feeSchedule": {"rate": 0.04}})["rate"], 0.04)    # exponent defaults to 1
        for unknown in (None, "x", {}, {"feesEnabled": True}, {"feesEnabled": True, "feeSchedule": "x"},
                        sched(rate=-1), sched(rate="abc"), sched(exponent=2), sched(rate=float("nan"))):
            self.assertIsNone(fees.market_fee(unknown), unknown)

    def test_fee_formula_matches_the_published_table(self):
        self.assertAlmostEqual(fees.fee_amount(100, 0.50, 0.07), 1.75)
        self.assertAlmostEqual(fees.fee_amount(100, 0.90, 0.07), 0.63)
        self.assertEqual(fees.fee_amount(100, 0.50, 0.0), 0.0)

    def test_fees_module_has_no_side_effects_on_import(self):
        self.assertTrue(callable(fees.market_fee))


class SizingUsesTheMarketsOwnFee(Patched):
    def test_the_market_rate_is_used_and_recorded(self):
        signals.get = fake_get([{"feesEnabled": True, "feeSchedule": {"rate": 0.03, "exponent": 1}}])
        s = signals.size_bet(self.signal())
        self.assertEqual((s["fee_rate"], s["fee_src"]), (0.03, "market"))
        self.assertNotEqual(s["fee_rate"], signals.FEE_RATE)               # the scan constant is 0.05
        self.assertAlmostEqual(s["fee"], fees.fee_amount(s["shares"], 0.60, 0.03))

    def test_a_fee_free_market_pays_nothing(self):
        signals.get = fake_get([{"feesEnabled": False}])
        s = signals.size_bet(self.signal())
        self.assertEqual((s["fee"], s["fee_rate"], s["fee_src"]), (0.0, 0.0, "market"))

    def test_a_failed_lookup_falls_back_to_the_scan_rate_and_is_retried_next_time(self):
        calls = []
        signals.get = fake_get(None, calls)
        s = signals.size_bet(self.signal())
        self.assertEqual((s["fee_rate"], s["fee_src"]), (signals.FEE_RATE, "scan"))
        signals.size_bet(self.signal())
        self.assertEqual(calls.count(f"{signals.GAMMA_API}/markets"), 2)    # not cached after a failure

    def test_a_market_that_does_not_say_is_cached_as_unknown(self):
        calls = []
        signals.get = fake_get([{"question": "no fee fields"}], calls)
        s = signals.size_bet(self.signal())
        self.assertEqual((s["fee_rate"], s["fee_src"]), (signals.FEE_RATE, "scan"))
        signals.size_bet(self.signal())
        self.assertEqual(calls.count(f"{signals.GAMMA_API}/markets"), 1)

    def test_the_lookup_is_by_condition_id_and_skipped_without_one(self):
        seen = []

        def get(url, params=None, raw=False):
            seen.append((url, params))
            return BOOK if url == signals.BOOK_URL else [{"feesEnabled": False}]
        signals.get = get
        signals.size_bet(self.signal())
        self.assertIn((f"{signals.GAMMA_API}/markets", {"condition_ids": "C1"}), seen)
        self.assertEqual(signals.market_fee_rate({"asset": "A"}), (signals.FEE_RATE, "scan"))


class FeeFreeScan(Patched):
    def setUp(self):
        super().setUp()
        signals.CAT = dict(signals.CAT, fee_free_only=True)

    def test_only_markets_that_charge_nothing_are_copied(self):
        signals.get = fake_get([{"feesEnabled": False}])
        self.assertIsNotNone(signals.size_bet(self.signal()))
        signals.FEE_CACHE.clear()
        signals.get = fake_get([{"feesEnabled": True, "feeSchedule": {"rate": 0.04, "exponent": 1}}])
        s = self.signal()
        self.assertIsNone(signals.size_bet(s))
        self.assertEqual(s["reject"], "fee")

    def test_an_unreadable_fee_is_treated_as_not_free_even_if_the_fallback_rate_were_zero(self):
        signals.FEE_RATE = 0.0                                     # a zero fallback must not be mistaken for "free"
        signals.get = fake_get(None)                               # the lookup failed
        s = self.signal()
        self.assertIsNone(signals.size_bet(s))
        self.assertEqual(s["reject"], "fee")
        signals.FEE_CACHE.clear()
        signals.get = fake_get([{"question": "silent"}])           # the market does not say
        self.assertIsNone(signals.size_bet(self.signal()))

    def test_the_category_is_configured_the_way_the_leaderboard_needs(self):
        cat = signals.CATEGORIES["GEOPOLITICS"]
        self.assertEqual((cat["tag"], cat["board"], cat["fee_free_only"]), ("geopolitics", "POLITICS", True))
        self.assertGreater(cat["fee"], 0)                          # the fallback is NOT zero

    def test_traders_come_from_the_politics_board_because_geopolitics_has_none(self):
        asked = []

        def data_v2(path, params, max_pages=10, stop=None):
            asked.append((path, dict(params)))
            return []
        old = (signals.data_v2, signals.SCAN)
        signals.data_v2, signals.SCAN = data_v2, "GEOPOLITICS"
        signals.CAT = signals.CATEGORIES["GEOPOLITICS"]
        try:
            signals.top_traders()
        finally:
            signals.data_v2, signals.SCAN = old
        self.assertTrue(asked)
        self.assertTrue(all(p["category"] == "POLITICS" for _, p in asked))

    def test_other_scans_still_ask_for_their_own_category(self):
        asked = []
        old = (signals.data_v2, signals.SCAN)
        signals.data_v2 = lambda path, params, max_pages=10, stop=None: asked.append(dict(params)) or []
        signals.SCAN = "SPORTS"
        signals.CAT = signals.CATEGORIES["SPORTS"]
        try:
            signals.top_traders()
        finally:
            signals.data_v2, signals.SCAN = old
        self.assertTrue(all(p["category"] == "SPORTS" for p in asked))


class BetsKeepTheirFee(Patched):
    def test_new_bets_record_their_fee_rate_and_source(self):
        signals.get = fake_get([{"feesEnabled": True, "feeSchedule": {"rate": 0.03, "exponent": 1}}])
        s = signals.size_bet(dict(self.signal(), outcome="Yes", title="t", event="e1", hours=5.0, traders=["x"],
                                  holders={"0xa": {"name": "x", "size": 5.0}}))
        led = {"cash": 50.0, "bets": []}
        signals.open_paper_bets(led, [s], book=None)
        self.assertEqual((led["bets"][0]["fee_rate"], led["bets"][0]["fee_src"]), (0.03, "market"))

    def test_bet_fee_rate_helper(self):
        self.assertEqual(signals.bet_fee_rate({"fee_rate": 0.02}), 0.02)
        self.assertEqual(signals.bet_fee_rate({"fee_rate": 0.0}), 0.0)
        for odd in ({}, {"fee_rate": None}, {"fee_rate": "x"}, {"fee_rate": -0.1}, None):
            self.assertEqual(signals.bet_fee_rate(odd), signals.FEE_RATE)

    def test_selling_uses_the_rate_the_bet_was_opened_under(self):
        book = {"bids": [{"price": "0.30", "size": "0.5"}]}
        self.assertAlmostEqual(signals.sell_fill(book, 0.5)[1], 0.15 - 0.5 * signals.FEE_RATE * 0.30 * 0.70)
        self.assertAlmostEqual(signals.sell_fill(book, 0.5, 0.0)[1], 0.15)

    def _stop_recovered(self, bet_extra):
        bet = {"status": "open", "price": 0.80, "shares": 1.25, "stake": 1.0, "fee": 0.01, "asset": "A",
               "condition": "c1", "title": "t", "outcome": "o", "traders": ["x"], "event": "e1"}
        bet.update(bet_extra)
        led = {"cash": 48.99, "bets": [bet]}
        signals.get, signals.REAL_STOP, signals.FEE_RATE = (lambda u, p=None: book_30), 0.40, 0.05
        signals.check_stops(led)
        return led["bets"][0]["recovered"]

    def test_a_stop_sells_with_the_bets_own_fee_rate(self):
        global book_30
        book_30 = {"bids": [{"price": "0.30", "size": "0.5"}], "asks": []}
        self.assertAlmostEqual(self._stop_recovered({}), 0.14475)                      # the scan's 0.05
        self.assertAlmostEqual(self._stop_recovered({"fee_rate": 0.0}), 0.15)          # a fee-free market


class Wiring(unittest.TestCase):
    def read(self, *parts):
        with open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
            return fh.read()

    def test_the_workflow_runs_the_geopolitics_scan_in_both_places(self):
        yml = self.read(".github", "workflows", "scan.yml")
        self.assertEqual(yml.count("TECH WEATHER GEOPOLITICS"), 2)

    def test_the_heartbeat_knows_the_new_ledger(self):
        self.assertIn("ledger_geopolitics.json", heartbeat.NAMES)

    def test_files_follow_the_existing_naming(self):
        self.assertEqual("ledger_geopolitics.json", "ledger_%s.json" % "GEOPOLITICS".lower())
        self.assertIn("GEOPOLITICS", signals.CATEGORIES)
        self.assertNotIn("GEOPOLITICS", signals.DEEP_SCANS)       # no deep-pool shadow for it


if __name__ == "__main__":
    unittest.main()
