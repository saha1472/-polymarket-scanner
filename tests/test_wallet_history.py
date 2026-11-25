"""Offline tests: a wallet history that was only partly read is never scored as if it were whole."""
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
_argv = sys.argv
sys.argv = ["signals.py", "SPORTS"]             # signals.py reads its scan from argv
import signals                                   # noqa: E402
sys.argv = _argv

NOW = datetime.now(timezone.utc)
RECENT = int(NOW.timestamp()) - 3600


def page(rows, cursor=None):
    return {"data": rows, "pagination": {"next_cursor": cursor}}


def closed_row(slug, pnl=5.0):
    return {"last_event_at": RECENT, "realized_pnl": pnl, "total_size": 10, "avg_price": 0.5, "event_slug": slug}


def redeemable_row(slug):
    return {"end_date": NOW.date().isoformat(), "current_value": 0, "total_cost_usdc": 4, "event_slug": slug}


class Patched(unittest.TestCase):
    def setUp(self):
        self._old = (signals.get, signals.CAT)
        signals.CAT = None

    def tearDown(self):
        signals.get, signals.CAT = self._old

    def serve(self, by_status):
        """by_status: {"CLOSED": [page, ...], "REDEEMABLE": [page, ...]}; a page of None is a failed request."""
        queues = {k: list(v) for k, v in by_status.items()}
        self.calls = []

        def get(url, params=None, raw=False):
            self.calls.append(dict(params or {}))
            return queues[params["status"]].pop(0)
        signals.get = get


class DataV2Complete(Patched):
    def pages(self, *answers):
        queue = list(answers)
        signals.get = lambda url, params=None, raw=False: queue.pop(0)

    def test_default_keeps_the_rows_read_before_a_failed_page(self):
        self.pages(page([{"i": 1}], "c2"), None)
        self.assertEqual(signals.data_v2("positions", {}), [{"i": 1}])

    def test_complete_gives_none_when_a_later_page_fails(self):
        self.pages(page([{"i": 1}], "c2"), None)
        self.assertIsNone(signals.data_v2("positions", {}, complete=True))

    def test_complete_gives_none_when_a_later_page_is_not_a_list(self):
        self.pages(page([{"i": 1}], "c2"), {"data": "oops"})
        self.assertIsNone(signals.data_v2("positions", {}, complete=True))

    def test_complete_gives_none_when_the_first_page_fails(self):
        self.pages(None)
        self.assertIsNone(signals.data_v2("positions", {}, complete=True))
        self.pages(None)
        self.assertIsNone(signals.data_v2("positions", {}))

    def test_complete_returns_everything_when_every_page_works(self):
        self.pages(page([{"i": 1}], "c2"), page([{"i": 2}], None))
        self.assertEqual([r["i"] for r in signals.data_v2("positions", {}, complete=True)], [1, 2])

    def test_complete_is_fine_when_the_stop_ends_paging_before_the_failing_page(self):
        self.pages(page([{"i": 1}, {"i": 2}], "c2"), None)
        got = signals.data_v2("positions", {}, stop=lambda r: r["i"] == 2, complete=True)
        self.assertEqual([r["i"] for r in got], [1])

    def test_an_empty_list_is_still_an_empty_list(self):
        self.pages(page([], None))
        self.assertEqual(signals.data_v2("positions", {}, complete=True), [])

    def test_positions_passes_complete_on(self):
        self.pages(page([{"token_id": "T"}], "c2"), None)
        self.assertIsNone(signals.positions("0x1", complete=True))
        self.pages(page([{"token_id": "T"}], "c2"), None)
        self.assertEqual([p["asset"] for p in signals.positions("0x1")], ["T"])


class TraderHistory(Patched):
    def test_whole_history_is_returned(self):
        self.serve({"CLOSED": [page([closed_row("a")])], "REDEEMABLE": [page([redeemable_row("b")])]})
        self.assertEqual(signals.trader_history("0x1"), [("a", 5.0, 5.0), ("b", -4.0, 4.0)])

    def test_a_failed_later_closed_page_means_no_history(self):
        self.serve({"CLOSED": [page([closed_row("a")], "c2"), None],
                    "REDEEMABLE": [page([redeemable_row("b")])]})
        self.assertIsNone(signals.trader_history("0x1"))

    def test_a_failed_later_redeemable_page_means_no_history(self):
        self.serve({"CLOSED": [page([closed_row("a")])],
                    "REDEEMABLE": [page([redeemable_row("b")], "c2"), None]})
        self.assertIsNone(signals.trader_history("0x1"))

    def test_a_failed_first_page_still_means_no_history(self):
        self.serve({"CLOSED": [None], "REDEEMABLE": [page([])]})
        self.assertIsNone(signals.trader_history("0x1"))
        self.serve({"CLOSED": [page([closed_row("a")])], "REDEEMABLE": [None]})
        self.assertIsNone(signals.trader_history("0x1"))

    def test_a_failed_closed_page_stops_before_asking_for_redeemable(self):
        self.serve({"CLOSED": [None], "REDEEMABLE": [page([])]})
        signals.trader_history("0x1")
        self.assertEqual([c["status"] for c in self.calls], ["CLOSED"])


class Scoring(Patched):
    def test_a_trader_with_a_half_read_history_gets_no_score_and_the_others_still_do(self):
        full = [(f"e{i}", 1.0 if i % 2 else -1.0, 2.0, RECENT) for i in range(20)]
        hist = {"0xfull": full, "0xhalf": None}
        old = signals.trader_history
        signals.trader_history = lambda w, with_time=False: hist[w]
        try:
            scores = signals.score_traders({"0xfull": "Full", "0xhalf": "Half"})
        finally:
            signals.trader_history = old
        self.assertEqual(list(scores), ["0xfull"])

    def test_end_to_end_a_page_failure_drops_the_trader(self):
        rows = [closed_row(f"e{i}", pnl=1.0) for i in range(20)]
        queues = {"0xgood": {"CLOSED": [page(rows)], "REDEEMABLE": [page([])]},
                  "0xcut": {"CLOSED": [page(rows[:10], "c2"), None], "REDEEMABLE": [page([])]}}

        def get(url, params=None, raw=False):
            return queues[params["user"]][params["status"]].pop(0)
        signals.get = get
        scores = signals.score_traders({"0xgood": "Good", "0xcut": "Cut"})
        self.assertEqual(list(scores), ["0xgood"])
        self.assertEqual(scores["0xgood"]["n"], 20)


if __name__ == "__main__":
    unittest.main()
