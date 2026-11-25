"""Offline tests: a wallet history is scored only when it was read in full, and
both of its lists cover the same stretch of time."""
import os
import sys
import unittest
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
_argv = sys.argv
sys.argv = ["signals.py", "SPORTS"]             # signals.py reads its scan from argv
import signals                                   # noqa: E402
sys.argv = _argv

NOW = datetime.now(timezone.utc)
DAY = 86400


def ago(days):
    return int(NOW.timestamp() - days * DAY)


def page(rows, cursor=None):
    return {"data": rows, "pagination": {"next_cursor": cursor}}


def closed_row(slug, pnl=5.0, days=0.04):
    return {"last_event_at": ago(days), "realized_pnl": pnl, "total_size": 10, "avg_price": 0.5, "event_slug": slug}


def redeemable_row(slug, days=0.04, end_days=None):
    end = NOW.timestamp() - (days if end_days is None else end_days) * DAY
    return {"end_date": datetime.fromtimestamp(end, timezone.utc).date().isoformat(), "last_event_at": ago(days),
            "current_value": 0, "total_cost_usdc": 4, "event_slug": slug}


class Patched(unittest.TestCase):
    def setUp(self):
        self._old = (signals.get, signals.CAT)
        signals.CAT = None

    def tearDown(self):
        signals.get, signals.CAT = self._old

    def serve(self, by_status):
        """by_status: {"CLOSED": [page, ...], "REDEEMABLE": [page, ...]}; a page of None is a failed request.
        Asking for more pages than were given fails the test."""
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


class Paging(Patched):
    def pages(self, *answers):
        queue = list(answers)
        self.asked = 0

        def get(url, params=None, raw=False):
            self.asked += 1
            return queue.pop(0)
        signals.get = get

    def test_stop_page_ends_after_the_page_and_keeps_all_of_its_rows(self):
        self.pages(page([{"i": 1}, {"i": 9}], "c2"), page([{"i": 2}], None))
        got = signals.data_v2("positions", {}, stop_page=lambda rows: any(r["i"] == 9 for r in rows))
        self.assertEqual([r["i"] for r in got], [1, 9])
        self.assertEqual(self.asked, 1)

    def test_capped_when_the_page_limit_ends_the_list_early(self):
        self.pages(page([{"i": 1}], "c2"), page([{"i": 2}], "c3"))
        got = signals.data_v2("positions", {}, max_pages=2)
        self.assertEqual((len(got), got.capped), (2, True))

    def test_not_capped_when_the_list_ends_inside_the_limit(self):
        self.pages(page([{"i": 1}], "c2"), page([{"i": 2}], None))
        self.assertFalse(signals.data_v2("positions", {}, max_pages=2).capped)

    def test_not_capped_when_a_stop_page_ends_it_on_the_last_allowed_page(self):
        self.pages(page([{"i": 1}], "c2"), page([{"i": 2}], "c3"))
        self.assertFalse(signals.data_v2("positions", {}, max_pages=2, stop_page=lambda rows: rows[0]["i"] == 2).capped)

    def test_not_capped_when_a_row_stop_ended_it(self):
        self.pages(page([{"i": 1}, {"i": 2}], "c2"))
        self.assertFalse(signals.data_v2("positions", {}, max_pages=1, stop=lambda r: r["i"] == 2).capped)

    def test_the_rows_still_behave_like_a_plain_list(self):
        self.pages(page([{"i": 1}], None))
        got = signals.data_v2("positions", {})
        self.assertEqual(got, [{"i": 1}])
        self.assertIsInstance(got, list)

    def test_positions_keeps_the_capped_flag(self):
        self.pages(page([{"token_id": "T"}], "c2"), page([{"token_id": "U"}], "c3"))
        got = signals.positions("0x1", max_pages=2)
        self.assertEqual(([p["asset"] for p in got], got.capped), (["T", "U"], True))


class CoveredFrom(unittest.TestCase):
    def rows(self, days, capped=True):
        out = signals.PagedRows({"last_event_at": ago(d)} for d in days)
        out.capped = capped
        return out

    def test_a_complete_list_covers_the_whole_window(self):
        since = ago(30)
        self.assertEqual(signals.covered_from(self.rows([1, 2, 3], capped=False), since), since)
        self.assertEqual(signals.covered_from(self.rows([], capped=True), since), since)
        self.assertEqual(signals.covered_from([{"last_event_at": ago(1)}], since), since)    # a plain list

    def test_a_cut_off_list_covers_back_to_the_newest_of_its_last_five_rows(self):
        since = ago(30)
        self.assertEqual(signals.covered_from(self.rows([1, 2, 3, 4, 5, 7.0, 7.1, 7.2, 7.3, 7.4]), since), ago(7.0))

    def test_rows_with_no_timestamp_are_ignored_when_finding_the_edge(self):
        since = ago(30)
        rows = self.rows([1, 2, 3, 4, 5, 7.0, 7.1, 7.2, 7.3])
        rows += [{"last_event_at": 0}, {}, {"last_event_at": None}]     # no usable timestamp
        self.assertEqual(signals.covered_from(rows, since), ago(5))      # the last five rows that have one

    def test_a_cut_off_list_with_no_timestamps_covers_nothing(self):
        rows = signals.PagedRows([{"last_event_at": 0}, {}])
        rows.capped = True
        self.assertEqual(signals.covered_from(rows, ago(30)), float("inf"))

    def test_never_earlier_than_the_window(self):
        since = ago(30)
        self.assertEqual(signals.covered_from(self.rows([40, 41, 42, 43, 44]), since), since)


class TraderHistory(Patched):
    def test_whole_history_is_returned(self):
        self.serve({"CLOSED": [page([closed_row("a")])], "REDEEMABLE": [page([redeemable_row("b")])]})
        self.assertEqual(signals.trader_history("0x1"), [("a", 5.0, 5.0), ("b", -4.0, 4.0)])

    def test_both_lists_are_asked_newest_first_and_closed_in_big_pages(self):
        self.serve({"CLOSED": [page([])], "REDEEMABLE": [page([])]})
        signals.trader_history("0x1")
        closed, unredeemed = self.calls
        for call in (closed, unredeemed):
            self.assertEqual((call["sort_by"], call["sort_direction"]), ("TIMESTAMP", "DESC"))
        self.assertEqual((closed["status"], closed["limit"]), ("CLOSED", 500))
        self.assertEqual(unredeemed["status"], "REDEEMABLE")

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


class TheWindow(Patched):
    def slugs(self, hist):
        return [h[0] for h in hist]

    def test_paging_stops_after_a_page_that_is_all_older_than_the_window(self):
        recent = page([closed_row("new")], "c2")
        old_page = page([closed_row("old1", days=40), closed_row("old2", days=41)], "c3")
        self.serve({"CLOSED": [recent, old_page], "REDEEMABLE": [page([])]})        # a third closed page would fail the test
        self.assertEqual(self.slugs(signals.trader_history("0x1")), ["new"])
        self.assertEqual([c["status"] for c in self.calls], ["CLOSED", "CLOSED", "REDEEMABLE"])

    def test_an_empty_page_or_one_with_no_timestamps_is_not_the_end_of_the_window(self):
        bare = {"event_slug": "bare", "realized_pnl": 1, "total_size": 1, "avg_price": 0.5}
        self.serve({"CLOSED": [page([], "c2"), page([bare, dict(bare)], "c3"), page([closed_row("new")])],
                    "REDEEMABLE": [page([])]})
        self.assertEqual(self.slugs(signals.trader_history("0x1")), ["new"])
        self.assertEqual([c["status"] for c in self.calls], ["CLOSED", "CLOSED", "CLOSED", "REDEEMABLE"])

    def test_a_page_of_old_rows_and_rows_with_no_timestamp_is_the_end(self):
        old_page = page([closed_row("old", days=40), {"event_slug": "bare"}], "c3")
        self.serve({"CLOSED": [page([closed_row("new")], "c2"), old_page], "REDEEMABLE": [page([])]})
        self.assertEqual(self.slugs(signals.trader_history("0x1")), ["new"])
        self.assertEqual([c["status"] for c in self.calls], ["CLOSED", "CLOSED", "REDEEMABLE"])

    def test_the_unredeemed_list_stops_the_same_way(self):
        recent = page([redeemable_row("new")], "c2")
        old_page = page([redeemable_row("old", days=40)], "c3")
        self.serve({"CLOSED": [page([])], "REDEEMABLE": [recent, old_page]})
        self.assertEqual(self.slugs(signals.trader_history("0x1")), ["new"])
        self.assertEqual([c["status"] for c in self.calls], ["CLOSED", "REDEEMABLE", "REDEEMABLE"])

    def test_old_rows_among_new_ones_do_not_end_the_list(self):
        # one in the middle, and one as the very last row of the page
        first = page([closed_row("n1"), closed_row("odd", days=60), closed_row("n2"), closed_row("odd2", days=61)], "c2")
        self.serve({"CLOSED": [first, page([closed_row("n3", days=2)])], "REDEEMABLE": [page([])]})
        self.assertEqual(self.slugs(signals.trader_history("0x1")), ["n1", "n2", "n3"])

    def test_rows_older_than_the_window_on_the_last_page_are_left_out(self):
        self.serve({"CLOSED": [page([closed_row("in", days=29), closed_row("out", days=31)])],
                    "REDEEMABLE": [page([redeemable_row("rin", days=29), redeemable_row("rout", days=31)])]})
        self.assertEqual(self.slugs(signals.trader_history("0x1")), ["in", "rin"])

    def test_an_unredeemed_bet_counts_by_its_last_event_not_its_end_date(self):
        late_end = redeemable_row("old_trade_new_end", days=40, end_days=1)      # last traded long ago
        early_end = redeemable_row("new_trade_old_end", days=1, end_days=40)
        self.serve({"CLOSED": [page([])], "REDEEMABLE": [page([early_end, late_end])]})
        self.assertEqual(self.slugs(signals.trader_history("0x1")), ["new_trade_old_end"])

    def test_a_row_with_no_timestamp_is_left_out(self):
        bare = {"end_date": NOW.date().isoformat(), "current_value": 0, "total_cost_usdc": 4, "event_slug": "bare"}
        self.serve({"CLOSED": [page([])], "REDEEMABLE": [page([bare, redeemable_row("ok")])]})
        self.assertEqual(self.slugs(signals.trader_history("0x1")), ["ok"])

    def cut_off_closed(self, step=2.0):
        """Five full pages of closed bets, `step` days apart (the last one at 5 x step days back), and more rows left."""
        return [page([closed_row(f"c{i}-{j}", days=step * (i + 1) + j / 1000.0) for j in range(5)], "more") for i in range(5)]

    def cut_off_unredeemed(self, step=2.0):
        """Six full pages of unredeemed bets, `step` days apart (the last one at 6 x step days back), and more rows left."""
        return [page([redeemable_row(f"u{i}-{j}", days=step * (i + 1) + j / 1000.0) for j in range(5)], "more") for i in range(6)]

    def test_a_cut_off_closed_list_narrows_the_window_for_both_lists(self):
        unredeemed = [redeemable_row("u3", days=3), redeemable_row("u9", days=9), redeemable_row("u15", days=15)]
        self.serve({"CLOSED": self.cut_off_closed(), "REDEEMABLE": [page(unredeemed)]})
        got = self.slugs(signals.trader_history("0x1"))
        self.assertIn("u3", got)
        self.assertIn("u9", got)
        self.assertNotIn("u15", got)                               # inside 30 days, but before the closed list reaches
        # the edge is the newest of the last five closed rows read (c4-0, 10 days back) and is itself left out
        self.assertIn("c3-4", got)
        self.assertNotIn("c4-0", got)
        self.assertEqual(sum(1 for s in got if s.startswith("c")), 20)

    def test_a_cut_off_unredeemed_list_narrows_the_window_for_closed_bets(self):
        closed = [closed_row("c2", days=2), closed_row("c9", days=9), closed_row("c20", days=20)]
        self.serve({"CLOSED": [page(closed)], "REDEEMABLE": self.cut_off_unredeemed()})
        got = self.slugs(signals.trader_history("0x1"))
        self.assertIn("c2", got)
        self.assertIn("c9", got)
        self.assertNotIn("c20", got)
        self.assertEqual(sum(1 for s in got if s.startswith("u")), 25)   # last page (12 days back) is the edge: its newest row is left out

    def test_a_cut_off_list_whose_tail_has_no_timestamps_still_narrows_the_window(self):
        bare = {"event_slug": "bare", "realized_pnl": 1, "total_size": 1, "avg_price": 0.5}
        pages = self.cut_off_closed()[:4]
        pages.append(page([dict(bare) for _ in range(5)], "more"))               # the last page: nothing to date it by
        unredeemed = [redeemable_row("u3", days=3), redeemable_row("u15", days=15)]
        self.serve({"CLOSED": pages, "REDEEMABLE": [page(unredeemed)]})
        got = self.slugs(signals.trader_history("0x1"))
        self.assertIn("u3", got)
        self.assertNotIn("u15", got)                                             # past the last dated row (8 days back)

    def test_a_cut_off_list_with_nothing_dated_gives_no_bets(self):
        bare = {"event_slug": "bare", "realized_pnl": 1, "total_size": 1, "avg_price": 0.5}
        self.serve({"CLOSED": [page([dict(bare) for _ in range(5)], "more") for _ in range(5)],
                    "REDEEMABLE": [page([redeemable_row("u1", days=1)])]})
        self.assertEqual(signals.trader_history("0x1"), [])

    # The minimum window: a list cut off by the page cap that reaches back less than MIN_HISTORY_DAYS is not judged.
    def test_the_minimum_is_a_week_inside_the_thirty_days(self):
        self.assertEqual(signals.MIN_HISTORY_DAYS, 7)
        self.assertLessEqual(signals.MIN_HISTORY_DAYS, signals.HISTORY_DAYS)

    def test_a_cut_off_closed_list_that_reaches_back_under_a_week_gives_no_bets(self):
        self.serve({"CLOSED": self.cut_off_closed(step=1.2), "REDEEMABLE": [page([redeemable_row("u1", days=1)])]})   # 6 days
        self.assertEqual(signals.trader_history("0x1"), [])

    def test_a_cut_off_unredeemed_list_that_reaches_back_under_a_week_gives_no_bets(self):
        self.serve({"CLOSED": [page([closed_row("c1", days=1)])], "REDEEMABLE": self.cut_off_unredeemed(step=1.1)})   # 6.6 days
        self.assertEqual(signals.trader_history("0x1"), [])

    def test_exactly_a_week_is_enough(self):
        self.serve({"CLOSED": self.cut_off_closed(step=1.4), "REDEEMABLE": [page([redeemable_row("u1", days=1)])]})   # 7 days
        got = self.slugs(signals.trader_history("0x1"))
        self.assertIn("u1", got)
        self.assertEqual(sum(1 for s in got if s.startswith("c")), 20)

    def test_just_under_a_week_is_not(self):
        self.serve({"CLOSED": self.cut_off_closed(step=1.38), "REDEEMABLE": [page([redeemable_row("u1", days=1)])]})  # 6.9 days
        self.assertEqual(signals.trader_history("0x1"), [])

    def test_a_young_wallet_with_a_complete_short_history_is_not_affected(self):
        # not cut off by the cap: its whole history is two days of bets, which is its own limit (the 30-bet bar), not a window problem
        self.serve({"CLOSED": [page([closed_row("a", days=0.5), closed_row("b", days=2)])],
                    "REDEEMABLE": [page([redeemable_row("c", days=1)])]})
        self.assertEqual(self.slugs(signals.trader_history("0x1")), ["a", "b", "c"])

    def test_a_cut_off_list_with_only_the_bets_of_the_last_day_or_two_is_not_scored(self):
        # the case the minimum exists for: a very busy wallet, 2500 closed rows in two days
        self.serve({"CLOSED": self.cut_off_closed(step=0.4), "REDEEMABLE": [page([])]})
        self.assertEqual(signals.trader_history("0x1"), [])

    def test_the_minimum_does_not_shorten_the_normal_window(self):
        self.serve({"CLOSED": [page([closed_row("in", days=29), closed_row("out", days=31)])], "REDEEMABLE": [page([])]})
        self.assertEqual(self.slugs(signals.trader_history("0x1")), ["in"])

    def test_a_trader_judged_on_too_short_a_window_gets_no_score(self):
        rows = [page([closed_row(f"e{i}-{j}", pnl=1.0, days=0.2 * (i + 1) + j / 1000.0) for j in range(6)], "more") for i in range(5)]
        queues = {"CLOSED": rows, "REDEEMABLE": [page([])]}
        self.serve(queues)
        self.assertEqual(signals.score_traders({"0x1": "Busy"}), {})

    def test_a_complete_pair_keeps_the_full_thirty_days(self):
        self.serve({"CLOSED": [page([closed_row("c", days=29)])], "REDEEMABLE": [page([redeemable_row("r", days=28)])]})
        self.assertEqual(self.slugs(signals.trader_history("0x1")), ["c", "r"])


class Scoring(Patched):
    def test_a_trader_with_a_half_read_history_gets_no_score_and_the_others_still_do(self):
        full = [(f"e{i}", 1.0 if i % 2 else -1.0, 2.0, ago(1)) for i in range(20)]
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
