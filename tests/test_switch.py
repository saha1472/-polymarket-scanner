"""Offline tests for the automatic pause/resume switch: fixed review schedule, bets grouped by
event and day, a minimum number of days."""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import shadow  # noqa: E402


def bet(i, roi, day=None, event=None):
    d = day if day is not None else i % 12
    return {"status": "won" if roi > 0 else "lost", "pnl": roi, "stake": 1.0, "fee": 0.0,
            "opened": f"2026-11-{1 + d:02d}T10:00:00+00:00", "closed": f"2026-12-01T{i // 60:02d}:{i % 60:02d}:00+00:00",
            "event": event if event is not None else f"e{i}"}


def losers(n, roi=-0.8, **kw):
    return [bet(i, roi, **kw) for i in range(n)]


def shadow_bets(rois, since_day=2, month=11, days=12):
    return [{"strategy": "S", "kind": "skipped", "reason": "auto_paused", "status": "won" if r > 0 else "lost",
             "stake": 1.0, "fee": 0.0, "pnl": r, "event": f"s{i}",
             "created": f"2026-{month:02d}-{since_day + i % days:02d}T00:00:00+00:00"} for i, r in enumerate(rois)]


def paused_ledger(real=()):
    return {"cash": 50, "bets": list(real), "auto": {"paused": True, "since": "2026-11-01T00:00:00+00:00"}}


class Stats(unittest.TestCase):
    def test_the_error_bar_groups_bets_that_move_together(self):
        # twelve days of five bets: each day's bets all do the same thing
        rois = [-0.8] * 5 * 5 + [-0.2] * 5 * 5 + [0.6] * 5 * 2
        bets = [bet(i, r, day=i // 5) for i, r in enumerate(rois)]
        n, mean, se, days = shadow.switch_stats(bets)
        _, _, se_iid = shadow.roi_stats(bets)
        self.assertEqual((n, days), (60, 12))
        self.assertAlmostEqual(mean, -0.31666, places=4)
        self.assertGreater(se, 2 * se_iid)                       # by day, not bet by bet
        self.assertLess(mean + shadow.PAUSE_Z * se_iid, 0)       # counting bets one by one would have paused it

    def test_bets_on_one_event_are_grouped_too(self):
        rois = [-0.8] * 25 + [-0.2] * 25 + [0.6] * 10
        by_event = [bet(i, r, event=f"ev{i // 10}") for i, r in enumerate(rois)]      # six events, spread over days
        alone = [bet(i, r) for i, r in enumerate(rois)]
        self.assertGreater(shadow.switch_stats(by_event)[2], 1.5 * shadow.switch_stats(alone)[2])

    def test_a_bet_without_an_event_or_day_is_a_group_of_its_own(self):
        bare = [{"status": "lost", "pnl": -1.0 if i % 3 else 0.5, "stake": 1.0, "fee": 0.0} for i in range(30)]
        n, mean, se, days = shadow.switch_stats(bare)
        _, mean_iid, se_iid = shadow.roi_stats(bare)
        self.assertEqual((n, days), (30, 0))
        self.assertAlmostEqual(se, se_iid)
        self.assertAlmostEqual(mean, mean_iid)

    def test_one_group_gives_no_error_bar(self):
        n, mean, se, days = shadow.switch_stats([bet(i, -1.0, day=3, event="same") for i in range(40)])
        self.assertEqual(se, float("inf"))

    def test_unsettled_and_broken_bets_are_left_out(self):
        bets = [bet(i, -1.0) for i in range(5)] + [{"status": "open", "pnl": -1, "stake": 1.0, "fee": 0.0},
                                                    {"status": "lost", "pnl": float("nan"), "stake": 1.0, "fee": 0.0},
                                                    {"status": "lost", "pnl": -1, "stake": 0.0, "fee": 0.0}]
        self.assertEqual(shadow.switch_stats(bets)[0], 5)
        self.assertEqual(shadow.switch_stats([])[0], 0)


class ReviewSchedule(unittest.TestCase):
    def test_review_due_waits_for_enough_bets_and_then_for_more(self):
        self.assertFalse(shadow.review_due(shadow.SWITCH_MIN_N - 1, {}))
        self.assertTrue(shadow.review_due(shadow.SWITCH_MIN_N, {}))
        auto = {"reviewed": 40}
        self.assertFalse(shadow.review_due(40 + shadow.REVIEW_EVERY - 1, auto))
        self.assertTrue(shadow.review_due(40 + shadow.REVIEW_EVERY, auto))

    def test_a_smaller_count_than_the_last_look_starts_afresh(self):
        self.assertTrue(shadow.review_due(shadow.SWITCH_MIN_N, {"reviewed": 90}))

    def test_the_fresh_start_is_remembered_across_calls(self):
        n = shadow.SWITCH_MIN_N
        led = {"cash": 50, "bets": [bet(i, 0.05 if i % 2 else -0.05) for i in range(n)], "auto": {"reviewed": n}}
        led["bets"] = led["bets"][:10]                                   # the count drops (judging restarted)
        self.assertIsNone(shadow.switch_update(led, [], "S"))
        self.assertEqual(led["auto"]["reviewed"], 0)                     # the old look is forgotten
        led["bets"] += [bet(100 + i, 0.05 if i % 2 else -0.05) for i in range(n - 10)]
        self.assertIsNone(shadow.switch_update(led, [], "S"))            # back at the minimum: a look is taken now
        self.assertEqual(led["auto"]["reviewed"], n)

    def test_a_look_is_remembered_and_the_next_one_waits(self):
        mild = [bet(i, -0.05 if i % 2 else 0.05) for i in range(35)]                 # nothing to pause
        led = {"cash": 50, "bets": mild}
        self.assertIsNone(shadow.switch_update(led, [], "S"))
        self.assertEqual(led["auto"]["reviewed"], 35)
        led["bets"] += losers(shadow.REVIEW_EVERY - 1, roi=-1.0)                    # terrible, but too soon
        self.assertIsNone(shadow.switch_update(led, [], "S"))
        self.assertEqual(led["auto"]["reviewed"], 35)                                 # no look was taken
        self.assertFalse(shadow.is_paused(led))

    def test_the_next_look_can_pause(self):
        led = {"cash": 50, "bets": [bet(i, -0.05 if i % 2 else 0.05) for i in range(35)]}
        shadow.switch_update(led, [], "S")
        led["bets"] += losers(shadow.REVIEW_EVERY, roi=-1.0)
        self.assertIn("Auto-paused", shadow.switch_update(led, [], "S"))
        self.assertTrue(shadow.is_paused(led))

    def test_calling_it_on_every_scan_takes_one_look_per_schedule(self):
        led = {"cash": 50, "bets": [bet(i, -0.05 if i % 2 else 0.05) for i in range(35)]}
        for _ in range(50):
            shadow.switch_update(led, [], "S")
        self.assertEqual(led["auto"]["reviewed"], 35)

    def test_history_survives_the_bookkeeping(self):
        led = {"cash": 50, "bets": [bet(i, 0.05) for i in range(35)], "auto": {"paused": False, "history": ["h"]}}
        shadow.switch_update(led, [], "S")
        self.assertEqual(led["auto"]["history"], ["h"])


class Pausing(unittest.TestCase):
    def test_a_clear_loser_over_enough_days_is_paused_and_says_so(self):
        led = {"cash": 50, "bets": losers(40)}
        msg = shadow.switch_update(led, [], "S")
        self.assertIn("Auto-paused", msg)
        self.assertIn("12 days", msg)
        self.assertTrue(shadow.is_paused(led))
        self.assertNotIn("reviewed", led["auto"])                    # the shadow bets are counted from zero

    def test_too_few_days_is_no_verdict(self):
        led = {"cash": 50, "bets": [bet(i, -1.0, day=i % (shadow.SWITCH_MIN_DAYS - 1)) for i in range(60)]}
        self.assertIsNone(shadow.switch_update(led, [], "S"))
        self.assertFalse(shadow.is_paused(led))

    def test_exactly_the_minimum_days_is_enough(self):
        led = {"cash": 50, "bets": [bet(i, -1.0, day=i % shadow.SWITCH_MIN_DAYS) for i in range(60)]}
        self.assertIn("Auto-paused", shadow.switch_update(led, [], "S"))

    def test_a_loss_that_only_looks_clear_bet_by_bet_is_not_paused(self):
        rois = [-0.8] * 25 + [-0.2] * 25 + [0.6] * 10
        led = {"cash": 50, "bets": [bet(i, r, day=i // 5) for i, r in enumerate(rois)]}
        self.assertIsNone(shadow.switch_update(led, [], "S"))

    def bets_with_z(self, z):
        """60 bets spread over seven days with no pattern by day, shifted so the mean sits exactly z error bars below zero."""
        base = [(-0.9, 0.5, 0.1, -0.6, 0.7)[(i * 3) % 5] for i in range(60)]
        probe = [bet(i, r, day=i % 7) for i, r in enumerate(base)]
        _, mean, se, _ = shadow.switch_stats(probe)
        shift = -z * se - mean
        return [bet(i, r + shift, day=i % 7) for i, r in enumerate(base)]

    def test_the_bar_is_the_new_one(self):
        # a loss clear of the old 90% bar (1.645 error bars) but not of the new one
        z = (1.645 + shadow.PAUSE_Z) / 2
        bets = self.bets_with_z(z)
        n, mean, se, days = shadow.switch_stats(bets)
        self.assertAlmostEqual(-mean / se, z)
        self.assertLess(mean + 1.645 * se, 0)
        self.assertIsNone(shadow.switch_update({"cash": 50, "bets": bets}, [], "S"))

    def test_a_loss_beyond_the_bar_is_paused(self):
        bets = self.bets_with_z(shadow.PAUSE_Z + 0.3)
        self.assertIn("Auto-paused", shadow.switch_update({"cash": 50, "bets": bets}, [], "S"))

    def test_only_the_most_recent_window_is_judged(self):
        old_losses = [bet(i, -1.0) for i in range(shadow.SWITCH_WINDOW)]
        recent_wins = [dict(bet(1000 + i, 0.5), closed=f"2026-12-02T{i // 60:02d}:{i % 60:02d}:00+00:00")
                       for i in range(shadow.SWITCH_WINDOW)]
        led = {"cash": 50, "bets": old_losses + recent_wins}
        self.assertIsNone(shadow.switch_update(led, [], "S"))

    def test_bets_that_cannot_be_scored_do_not_count_towards_the_minimum(self):
        broken = [{"status": "lost", "pnl": float("nan"), "stake": 1.0, "fee": 0.0, "opened": "2026-11-03T00:00:00+00:00",
                   "closed": "2026-12-03T00:00:00+00:00"} for _ in range(10)]
        led = {"cash": 50, "bets": losers(shadow.SWITCH_MIN_N - 5) + broken}      # enough settled bets, too few that can be scored
        self.assertIsNone(shadow.switch_update(led, [], "S"))
        self.assertFalse(shadow.is_paused(led))

    def test_judge_from_still_hides_old_bets(self):
        led = {"cash": 50, "bets": losers(40)}
        self.assertIsNone(shadow.switch_update(led, [], "S", judge_from="2026-12-31T00:00:00"))


class Resuming(unittest.TestCase):
    def good(self, n=40):
        return shadow_bets([0.7 if i % 5 else -0.5 for i in range(n)])

    def test_convincing_shadow_results_resume_it(self):
        led = paused_ledger()
        msg = shadow.switch_update(led, self.good(), "S")
        self.assertIn("Auto-resumed", msg)
        self.assertIn("days", msg)
        self.assertFalse(shadow.is_paused(led))
        self.assertEqual(len(led["auto"]["history"]), 1)

    def test_a_resume_is_not_undone_by_the_next_scan(self):
        led = paused_ledger(losers(40))                                    # the bets that led to the pause are still there
        shadow.switch_update(led, self.good(), "S")
        self.assertFalse(shadow.is_paused(led))
        for _ in range(5):
            self.assertIsNone(shadow.switch_update(led, self.good(), "S"))
        self.assertFalse(shadow.is_paused(led))
        self.assertEqual(led["auto"]["reviewed"], 40)

    def test_after_a_resume_the_next_pause_needs_new_settled_bets(self):
        led = paused_ledger(losers(40))
        shadow.switch_update(led, self.good(), "S")
        led["bets"] += losers(shadow.REVIEW_EVERY - 1)
        self.assertIsNone(shadow.switch_update(led, [], "S"))
        led["bets"] += losers(1)
        self.assertIn("Auto-paused", shadow.switch_update(led, [], "S"))

    def test_too_few_shadow_days_is_no_verdict(self):
        rois = [0.7 if i % 5 else -0.5 for i in range(40)]
        sh = shadow_bets(rois, days=shadow.SWITCH_MIN_DAYS - 1)
        led = paused_ledger()
        self.assertIsNone(shadow.switch_update(led, sh, "S"))
        self.assertTrue(shadow.is_paused(led))
        sh = shadow_bets(rois, days=shadow.SWITCH_MIN_DAYS)
        self.assertIn("Auto-resumed", shadow.switch_update(paused_ledger(), sh, "S"))

    def test_shadow_bets_from_before_the_pause_do_not_count(self):
        before = shadow_bets([0.7 if i % 5 else -0.5 for i in range(40)], month=10)       # spread over days, but before 1 Nov
        led = paused_ledger()
        self.assertIsNone(shadow.switch_update(led, before, "S"))
        self.assertTrue(shadow.is_paused(led))

    def test_unsettled_shadow_bets_are_not_enough_for_a_look(self):
        sh = shadow_bets([0.5] * 20)
        sh += [dict(b, status="open", pnl=None) for b in shadow_bets([0.5] * 20)]
        led = paused_ledger()
        self.assertIsNone(shadow.switch_update(led, sh, "S"))
        self.assertNotIn("reviewed", led["auto"])                         # only 20 have settled: no look taken

    def test_a_weak_result_stays_paused_and_is_looked_at_again_only_later(self):
        weak = shadow_bets([0.05 if i % 2 else -0.05 for i in range(40)])
        led = paused_ledger()
        self.assertIsNone(shadow.switch_update(led, weak, "S"))
        self.assertEqual(led["auto"]["reviewed"], 40)
        self.assertTrue(shadow.is_paused(led))
        # ten more good ones are not a new look yet
        self.assertIsNone(shadow.switch_update(led, weak + shadow_bets([1.0] * 10), "S"))
        self.assertEqual(led["auto"]["reviewed"], 40)

    def test_an_old_pause_without_bookkeeping_still_works(self):
        led = {"cash": 50, "bets": [], "auto": {"paused": True, "since": "2026-11-01T00:00:00+00:00", "why": "x"}}
        self.assertIn("Auto-resumed", shadow.switch_update(led, self.good(), "S"))

    def test_only_shadow_bets_of_this_strategy_since_the_pause_count(self):
        sh = self.good()
        for b in sh:
            b["strategy"] = "OTHER"
        led = paused_ledger()
        self.assertIsNone(shadow.switch_update(led, sh, "S"))


if __name__ == "__main__":
    unittest.main()
