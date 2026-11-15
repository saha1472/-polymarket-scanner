"""Offline tests for wx_calibrate.py (no network, no files outside a temp dir)."""
import math
import os
import random
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import simcore  # noqa: E402
import wx_calibrate as wc  # noqa: E402


def synth_rows(n, truth, seed=1, start=date(2026, 11, 1), days=10):
    """Rows whose outcome depends on `truth` ('model' or 'market' or 'none')."""
    rnd = random.Random(seed)
    rows = []
    for i in range(n):
        pm, pk = rnd.uniform(0.02, 0.98), rnd.uniform(0.02, 0.98)
        p = {"model": pm, "market": pk, "none": 0.5}[truth]
        rows.append({"g": "highest@10", "date": (start + timedelta(days=i % days)).isoformat(),
                     "slug": f"ev{i}", "y": 1 if rnd.random() < p else 0,
                     "pm": pm, "pk": pk, "ask": min(pk + 0.02, 0.99), "bid": max(pk - 0.02, 0.01),
                     "two": True})
    return rows


class Fit(unittest.TestCase):
    def test_learns_to_trust_the_model_when_it_is_the_truth(self):
        w = wc.fit_blend(synth_rows(3000, "model"))
        self.assertGreater(w[1], 0.6)
        self.assertLess(w[2], 0.4)

    def test_learns_to_trust_the_market_when_it_is_the_truth(self):
        w = wc.fit_blend(synth_rows(3000, "market"))
        self.assertGreater(w[2], 0.6)
        self.assertLess(abs(w[1]), 0.4)

    def test_thin_data_stays_near_the_market(self):
        w = wc.fit_blend(synth_rows(8, "model"))
        self.assertLess(abs(w[1]), 0.5)
        self.assertLess(abs(w[2] - 1.0), 0.5)

    def test_no_rows_gives_identity(self):
        self.assertEqual(wc.fit_blend([]), list(wc.IDENTITY))

    def test_blend_of_identity_is_the_market(self):
        self.assertAlmostEqual(wc.blend_prob(wc.IDENTITY, 0.9, 0.3), 0.3, places=3)

    def test_solve3(self):
        x = wc.solve3([[2, 0, 0], [0, 3, 0], [1, 0, 1]], [4, 9, 3])
        self.assertAlmostEqual(x[0], 2)
        self.assertAlmostEqual(x[1], 3)
        self.assertAlmostEqual(x[2], 1)


class Prices(unittest.TestCase):
    def test_market_probs_sum_to_one_and_missing_sides_are_not_a_favourite(self):
        bks = [{"bid": 0.4, "ask": 0.5}, {"bid": None, "ask": 0.2}, {"bid": 0.3, "ask": 0.4}]
        p = wc.market_probs(bks)
        self.assertAlmostEqual(sum(p), 1.0)
        self.assertLess(p[1], p[0])           # a lone ask of 0.2 reads as (0 + 0.2) / 2

    def test_two_sided_needs_both_sides_and_a_tight_spread(self):
        self.assertTrue(wc.two_sided(0.40, 0.45))
        self.assertFalse(wc.two_sided(None, 0.45))
        self.assertFalse(wc.two_sided(0.40, None))
        self.assertFalse(wc.two_sided(0.20, 0.45))       # 25c wide
        self.assertFalse(wc.two_sided(0.50, 0.45))       # crossed

    def test_hypo_pnl_matches_the_eval_formula(self):
        # win at 0.80: 1.25 shares, fee 1.25 * 0.05 * 0.8 * 0.2 = 0.01
        self.assertAlmostEqual(wc.hypo_pnl(0.8, True), 1.25 - 1 - 0.01)
        self.assertAlmostEqual(wc.hypo_pnl(0.8, False), -1 - 0.01)


class Winners(unittest.TestCase):
    def test_event_winner(self):
        self.assertEqual(wc.event_winner(["a", "b"], {"a": 0.0, "b": 1.0}), ("settled", "b"))
        self.assertEqual(wc.event_winner(["a", "b"], {"a": 0.0, "b": 0.0}), ("settled", None))
        self.assertIsNone(wc.event_winner(["a", "b"], {"a": 0.0}))            # b not final
        self.assertIsNone(wc.event_winner(["a", "b"], {"a": 1.0, "b": 1.0}))  # two winners
        self.assertIsNone(wc.event_winner(["a", "b"], {"a": 0.5, "b": 0.5}))  # split
        self.assertIsNone(wc.event_winner([], {}))

    def records(self, d):
        return [{"slug": "e1", "date": d, "kind": "highest", "checkpoint": 10,
                 "buckets": [{"yes": "t1", "p": 0.5, "bid": 0.4, "ask": 0.5},
                             {"yes": "t2", "p": 0.5, "bid": 0.4, "ask": 0.5}]}]

    def test_fetch_respects_lag_cache_batching_and_cap(self):
        today = date(2026, 11, 15)
        calls = []

        def get(url, params):
            calls.append(params["token_id"].split(","))
            return {"data": [{"token_id": t, "resolved": True, "final_price": 1.0 if t == "t2" else 0.0}
                             for t in params["token_id"].split(",")]}

        win = {}
        # too recent (inside the settlement lag): nothing fetched
        wc.fetch_winners(self.records("2026-11-14"), win, get, today)
        self.assertEqual((win, calls), ({}, []))
        # old enough: fetched once, cached
        wc.fetch_winners(self.records("2026-11-10"), win, get, today)
        self.assertEqual(win, {"e1": "t2"})
        self.assertEqual(len(calls), 1)
        # cached: no new call
        wc.fetch_winners(self.records("2026-11-10"), win, get, today)
        self.assertEqual(len(calls), 1)

    def test_fetch_batches_at_most_40_tokens_and_stops_at_the_cap(self):
        today = date(2026, 11, 15)
        recs = [{"slug": f"e{i}", "date": "2026-11-01", "kind": "highest", "checkpoint": 10,
                 "buckets": [{"yes": f"t{i}_{j}", "p": 0.1, "bid": None, "ask": None} for j in range(5)]}
                for i in range(30)]                                     # 150 tokens
        sizes = []

        def get(url, params):
            toks = params["token_id"].split(",")
            sizes.append(len(toks))
            return [{"token_id": t, "resolved": True, "final_price": 0.0} for t in toks]

        win = {}
        n = wc.fetch_winners(recs, win, get, today, max_calls=2)
        self.assertEqual(n, 2)
        self.assertTrue(all(s <= wc.TOKENS_PER_CALL for s in sizes))
        self.assertLess(len(win), 30)             # the cap stopped it; the rest waits

    def test_a_slow_api_cannot_eat_the_run(self):
        today = date(2026, 11, 15)
        recs = [{"slug": f"e{i}", "date": "2026-11-01", "kind": "highest", "checkpoint": 10,
                 "buckets": [{"yes": f"t{i}_{j}", "p": 0.1, "bid": None, "ask": None} for j in range(5)]}
                for i in range(60)]                                     # 300 tokens = 8 calls
        t = [0.0]
        calls = []

        def get(url, params):
            calls.append(1)
            t[0] += 100.0                                               # every call "takes" 100 s
            return [{"token_id": x, "resolved": True, "final_price": 0.0}
                    for x in params["token_id"].split(",")]

        stats = {}
        n = wc.fetch_winners(recs, {}, get, today, budget_s=150, clock=lambda: t[0], stats=stats)
        self.assertEqual(n, 2)                                          # stopped after 150 s
        self.assertTrue(stats["stopped_early"])
        full = {}
        stats = {}
        wc.fetch_winners(recs, full, lambda u, p: [{"token_id": x, "resolved": True, "final_price": 0.0}
                                                   for x in p["token_id"].split(",")], today, stats=stats)
        self.assertFalse(stats.get("stopped_early"))                    # finished in time: no backlog flag
        self.assertEqual(len(full), 60)

    def test_unresolved_markets_are_not_cached(self):
        get = lambda url, params: [{"token_id": "t1", "resolved": False, "final_price": 0.0},  # noqa: E731
                                   {"token_id": "t2", "resolved": False, "final_price": 0.0}]
        win = {}
        wc.fetch_winners(self.records("2026-11-01"), win, get, date(2026, 11, 15))
        self.assertEqual(win, {})


class Rows(unittest.TestCase):
    def test_build_rows_only_for_settled_events_and_marks_the_winner(self):
        recs = [{"slug": "e1", "date": "2026-11-01", "kind": "highest", "checkpoint": 10,
                 "buckets": [{"yes": "a", "p": 0.6, "bid": 0.5, "ask": 0.55},
                             {"yes": "b", "p": 0.4, "bid": 0.4, "ask": 0.45}]},
                {"slug": "e2", "date": "2026-11-01", "kind": "highest", "checkpoint": 10,
                 "buckets": [{"yes": "c", "p": 1.0, "bid": 0.9, "ask": 0.95}]}]
        rows = wc.build_rows(recs, {"e1": "b"})
        self.assertEqual([(r["slug"], r["y"]) for r in rows], [("e1", 0), ("e1", 1)])
        self.assertEqual(rows[0]["g"], "highest@10")
        # an event won by a bucket outside the list: everyone listed is a loser
        rows = wc.build_rows(recs[:1], {"e1": None})
        self.assertEqual([r["y"] for r in rows], [0, 0])

    def test_split_has_no_leakage_and_needs_enough_days(self):
        rows = synth_rows(400, "none", days=wc.MIN_TRAIN_DAYS + wc.TEST_DAYS)
        train, test = wc.split_days(rows)
        self.assertTrue(max(r["date"] for r in train) < min(r["date"] for r in test))
        self.assertEqual(len({r["date"] for r in test}), wc.TEST_DAYS)
        self.assertIsNone(wc.split_days(synth_rows(400, "none", days=wc.TEST_DAYS + 1)))


class Gate(unittest.TestCase):
    def test_a_useless_model_is_not_trusted(self):
        res = wc.evaluate_group(synth_rows(6000, "market", days=12))
        self.assertFalse(res["ok"])
        self.assertTrue(res["reasons"])
        self.assertEqual(wc.update_state(None, res, "2026-11-15")["weights"], list(wc.IDENTITY))

    def test_too_little_history_is_not_trusted(self):
        res = wc.evaluate_group(synth_rows(300, "model", days=3))
        self.assertFalse(res["ok"])
        self.assertIn("not enough settled days yet", res["reasons"])

    def test_losing_buys_fail_the_gate_even_if_log_loss_looks_fine(self):
        # a model that is informative but whose cheap "edges" all lose at the real ask
        rows = []
        for i in range(2400):
            rows.append({"g": "lowest@8", "date": f"2026-11-{1 + i % 12:02d}", "slug": f"e{i}",
                         "y": 0, "pm": 0.9, "pk": 0.5, "ask": 0.5, "bid": 0.48, "two": True})
        res = wc.evaluate_group(rows)
        self.assertFalse(res["ok"])

    def test_streak_must_be_consecutive_and_counts_once_per_day(self):
        ok, bad = {"ok": True, "weights": [0.1, 0.5, 0.5]}, {"ok": False, "weights": [0, 0, 1]}
        s = None
        for day in ("2026-11-01", "2026-11-02"):
            s = wc.update_state(s, ok, day)
            self.assertFalse(s["trusted"])
        s_same_day = wc.update_state(s, ok, "2026-11-02")        # a re-run: no double count
        self.assertEqual(s_same_day["streak"], 2)
        s = wc.update_state(s, ok, "2026-11-03")
        self.assertTrue(s["trusted"])
        self.assertEqual(s["weights"], [0.1, 0.5, 0.5])
        s = wc.update_state(s, bad, "2026-11-04")                # one failure resets it
        self.assertFalse(s["trusted"])
        self.assertEqual(s["streak"], 0)


class FailClosed(unittest.TestCase):
    NOW = datetime(2026, 11, 15, 12, tzinfo=timezone.utc)

    def cal(self, **kw):
        g = {"trusted": True, "weights": [0.1, 0.6, 0.5]}
        g.update(kw)
        return {"fitted_on": "2026-11-15", "groups": {"highest@10": g}}

    def test_trusted_fresh_group_uses_its_weights(self):
        self.assertEqual(wc.weights_for(self.cal(), "highest", 10, self.NOW), [0.1, 0.6, 0.5])

    def test_everything_else_is_the_market(self):
        ident = list(wc.IDENTITY)
        self.assertEqual(wc.weights_for({}, "highest", 10, self.NOW), ident)
        self.assertEqual(wc.weights_for(None, "highest", 10, self.NOW), ident)
        self.assertEqual(wc.weights_for(self.cal(trusted=False), "highest", 10, self.NOW), ident)
        self.assertEqual(wc.weights_for(self.cal(trusted="yes"), "highest", 10, self.NOW), ident)
        self.assertEqual(wc.weights_for(self.cal(weights=[1, 2]), "highest", 10, self.NOW), ident)
        self.assertEqual(wc.weights_for(self.cal(weights=[0, float("nan"), 1]), "highest", 10, self.NOW), ident)
        self.assertEqual(wc.weights_for(self.cal(), "lowest", 8, self.NOW), ident)        # unknown group
        stale = self.cal()
        stale["fitted_on"] = "2026-11-01"
        self.assertEqual(wc.weights_for(stale, "highest", 10, self.NOW), ident)
        bad_date = self.cal()
        bad_date["fitted_on"] = "not a date"
        self.assertEqual(wc.weights_for(bad_date, "highest", 10, self.NOW), ident)

    def test_calibrated_probs_equal_market_probs_when_untrusted(self):
        bks = [{"bid": 0.4, "ask": 0.5}, {"bid": 0.45, "ask": 0.55}]
        got = wc.calibrated_probs({}, "highest", 10, bks, [0.99, 0.01], self.NOW)
        for a, b in zip(got, wc.market_probs(bks)):
            self.assertAlmostEqual(a, b, places=3)


class EndToEnd(unittest.TestCase):
    def test_main_writes_files_only_in_cwd_and_is_idempotent_within_a_day(self):
        recs = []
        for i in range(40):
            d = (date(2026, 11, 1) + timedelta(days=i % 8)).isoformat()
            recs.append({"slug": f"e{i}", "date": d, "kind": "highest", "checkpoint": 10,
                         "buckets": [{"yes": f"y{i}a", "p": 0.7, "bid": 0.5, "ask": 0.55},
                                     {"yes": f"y{i}b", "p": 0.3, "bid": 0.4, "ask": 0.45}]})

        def get(url, params):
            return [{"token_id": t, "resolved": True, "final_price": 1.0 if t.endswith("a") else 0.0}
                    for t in params["token_id"].split(",")]

        old = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                import json
                with open(wc.SHADOW_LOG, "w") as fh:
                    fh.write("".join(json.dumps(r) + "\n" for r in recs) + "not json\n")
                self.assertEqual(wc.main(get=get, today=date(2026, 11, 15)), 0)
                self.assertTrue(os.path.exists(wc.CAL_FILE))
                with open(wc.WINNERS_FILE) as fh:
                    self.assertEqual(len(json.load(fh)), 40)
                with open(wc.CAL_FILE) as fh:
                    cal = json.load(fh)
                self.assertFalse(cal["groups"]["highest@10"]["trusted"])     # too little data
                calls = []
                self.assertEqual(wc.main(get=lambda u, p: calls.append(1), today=date(2026, 11, 15)), 0)
                self.assertEqual(calls, [])                                   # same day: skipped
            finally:
                os.chdir(old)


if __name__ == "__main__":
    unittest.main()
