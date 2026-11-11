"""Tests for the 11 Nov deep pool: traders ranked 51-200 on the category board,
copied as shadow records only (sports, esports, weather). No network: every
call is faked.

Run:  python -m unittest discover -s tests -v
"""
import contextlib
import fnmatch
import io
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
_argv = sys.argv
sys.argv = ["signals.py", "SPORTS"]
import signals                                 # noqa: E402
sys.argv = _argv
import shadow                                  # noqa: E402


def ago(hours):
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


class Patch:
    """Set module attributes for one test and put them back afterwards."""

    def __init__(self, mod, **values):
        self.mod, self.values, self.saved = mod, values, {}

    def __enter__(self):
        for k, v in self.values.items():
            self.saved[k] = getattr(self.mod, k)
            setattr(self.mod, k, v)

    def __exit__(self, *exc):
        for k, v in self.saved.items():
            setattr(self.mod, k, v)


class InTempDir(unittest.TestCase):
    def setUp(self):
        self._old = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory()
        os.chdir(self._tmp.name)

    def tearDown(self):
        os.chdir(self._old)
        self._tmp.cleanup()


def good(name="T", **extra):
    """A score that clears the deep bar."""
    return dict({"name": name, "win_rate": 0.65, "roi": 0.20, "n": 40, "roi_lo": 0.05,
                 "roi_h1": 0.1, "roi_h2": 0.3, "alt": True}, **extra)


def row(wallet, name="x", pnl=500, vol=1000):
    return {"user_id": wallet, "user_name": name, "pnl": pnl, "volume": vol}


class Board(unittest.TestCase):
    def _board(self, month, all_time, **patch):
        seen = []

        def fake(path, params, max_pages=10, stop=None):
            seen.append((path, dict(params), max_pages))
            return month if params["timePeriod"] == "MONTH" else all_time
        with Patch(signals, SCAN="SPORTS", LEADERBOARD_SIZE=3, data_v2=fake, **patch):
            return signals.deep_board(), seen

    def test_takes_only_ranks_below_the_normal_pool(self):
        rows = [row(f"0x{i}", f"n{i}") for i in range(1, 7)]       # ranks 1-6
        board, _ = self._board(rows, [])
        self.assertEqual(board, {"0x4": "n4", "0x5": "n5", "0x6": "n6"})

    def test_asks_for_200_rows_of_this_category_in_one_call_per_board(self):
        _, seen = self._board([], [])
        self.assertEqual({s[1]["timePeriod"] for s in seen}, {"MONTH", "ALL"})
        for path, params, pages in seen:
            self.assertEqual((path, params["limit"], params["category"], params["orderBy"], pages),
                             ("leaderboard", 200, "SPORTS", "PNL", 1))

    def test_someone_in_the_normal_pool_on_either_board_is_left_out(self):
        month = [row(f"0x{i}") for i in range(1, 7)]
        # 0x5 is rank 5 on the month board but rank 1 on the all-time board
        alltime = [row("0x5")] + [row(f"0xa{i}") for i in range(1, 5)]
        board, _ = self._board(month, alltime)
        self.assertNotIn("0x5", board)
        self.assertEqual(set(board), {"0x4", "0x6", "0xa3", "0xa4"})

    def test_market_makers_are_left_out(self):
        rows = [row("0x1"), row("0x2"), row("0x3"), row("0xmm", pnl=1, vol=1000), row("0xok")]
        board, _ = self._board(rows, [])
        self.assertEqual(set(board), {"0xok"})

    def test_the_watchlist_is_left_out_because_it_is_scored_with_the_normal_pool(self):
        rows = [row("0x1"), row("0x2"), row("0x3"), row("0xAbC", "watched"), row("0xok")]
        board, _ = self._board(rows, [], WATCHLIST={"SPORTS": {"0xabc": "watched"}})
        self.assertEqual(set(board), {"0xok"})

    def test_a_wallet_on_both_boards_appears_once(self):
        rows = [row("0x1"), row("0x2"), row("0x3"), row("0xdd", "first")]
        board, _ = self._board(rows, rows)
        self.assertEqual(board, {"0xdd": "first"})

    def test_failed_reads_give_an_empty_board(self):
        board, _ = self._board(None, None)
        self.assertEqual(board, {})


class Refresh(unittest.TestCase):
    """deep_refresh with a fake board and a fake scorer."""

    def setUp(self):
        self.calls = []
        self.board_reads = 0

    def board(self, wallets):
        def read():
            self.board_reads += 1
            return dict(wallets)
        return read

    def scorer(self, results=None, fail=()):
        results = results if results is not None else {}

        def score(traders):
            self.calls.append(list(traders))
            if any(w in fail for w in traders):
                raise RuntimeError("boom")
            return {w: results.get(w, good(traders[w])) for w in traders
                    if results.get(w, 1) is not None}
        return score

    def run_(self, cache, wallets, **kw):
        with Patch(signals, DEEP_MAX_SCORED_PER_RUN=kw.pop("cap", 4), DEEP_CHUNK=kw.pop("chunk", 2)):
            return signals.deep_refresh(cache, board=kw.pop("board", self.board(wallets)),
                                        score=kw.pop("score", self.scorer()), **kw)

    WALLETS = {f"0x{i}": f"n{i}" for i in range(1, 7)}

    def test_first_run_reads_the_board_and_scores_up_to_the_cap(self):
        cache, done = self.run_({}, self.WALLETS)
        self.assertEqual(done, 4)
        self.assertEqual(len(cache["scores"]), 4)
        self.assertEqual(len(cache["checked"]), 4)
        self.assertEqual(cache["board"], self.WALLETS)
        self.assertEqual([len(c) for c in self.calls], [2, 2])         # in chunks of 2

    def test_the_next_runs_pick_up_the_rest_then_go_quiet(self):
        cache, _ = self.run_({}, self.WALLETS)
        cache, done = self.run_(cache, self.WALLETS)
        self.assertEqual(done, 2)
        self.assertEqual(set(cache["scores"]), set(self.WALLETS))
        self.calls.clear()
        cache, done = self.run_(cache, self.WALLETS)
        self.assertEqual((done, self.calls), (0, []))                  # nothing due: no work at all
        self.assertEqual(self.board_reads, 1)                          # board read once, not every run

    def test_a_wallet_with_too_few_bets_is_not_retried_all_day(self):
        cache, _ = self.run_({}, {"0x1": "a", "0x2": "b"}, score=self.scorer({"0x2": None}))
        self.assertEqual(set(cache["scores"]), {"0x1"})
        self.assertEqual(set(cache["checked"]), {"0x1", "0x2"})        # 0x2 was looked at
        self.calls.clear()
        cache, done = self.run_(cache, {"0x1": "a", "0x2": "b"})
        self.assertEqual(done, 0)

    def test_old_scores_are_refreshed_oldest_first(self):
        w = {"0x1": "a", "0x2": "b", "0x3": "c"}
        cache = {"board": w, "board_at": ago(1),
                 "checked": {"0x1": ago(30), "0x2": ago(50), "0x3": ago(2)},
                 "scores": {k: good() for k in w}}
        cache, done = self.run_(cache, w, cap=1, chunk=1)
        self.assertEqual((done, self.calls), (1, [["0x2"]]))           # the 50-hour-old one

    def test_never_scored_comes_before_old(self):
        w = {"0x1": "a", "0x2": "b"}
        cache = {"board": w, "board_at": ago(1), "checked": {"0x1": ago(90)},
                 "scores": {"0x1": good()}}
        self.run_(cache, w, cap=1, chunk=1)
        self.assertEqual(self.calls, [["0x2"]])

    def test_board_is_re_read_after_a_day_and_dropped_wallets_are_forgotten(self):
        old = {"0x1": "a", "0x2": "b"}
        cache = {"board": old, "board_at": ago(30), "checked": {"0x1": ago(1), "0x2": ago(1)},
                 "scores": {"0x1": good(), "0x2": good()}}
        cache, _ = self.run_(cache, {"0x2": "b", "0x3": "c"})
        self.assertEqual(self.board_reads, 1)
        self.assertEqual(set(cache["board"]), {"0x2", "0x3"})
        self.assertEqual(set(cache["scores"]) - {"0x3"}, {"0x2"})      # 0x1 forgotten
        self.assertNotIn("0x1", cache["checked"])

    def test_a_failed_board_read_keeps_the_old_board(self):
        old = {"0x1": "a"}
        cache = {"board": old, "board_at": ago(30), "checked": {"0x1": ago(1)},
                 "scores": {"0x1": good()}}
        cache, _ = self.run_(cache, {})
        self.assertEqual(cache["board"], old)
        self.assertIn("0x1", cache["scores"])

    def test_an_empty_board_stays_empty_without_crashing(self):
        cache, done = self.run_({}, {})
        self.assertEqual((done, cache.get("scores")), (0, {}))

    def test_stops_scoring_when_the_time_budget_is_spent(self):
        ticks = iter([0, 0, 1000, 2000, 3000, 4000])                   # 2nd look is past the budget
        cache, done = self.run_({}, self.WALLETS, cap=6, chunk=2, clock=lambda: next(ticks))
        self.assertEqual(done, 2)                                       # one chunk, then stop
        self.assertEqual(len(cache["scores"]), 2)

    def test_a_chunk_that_blows_up_is_skipped_and_the_rest_carry_on(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cache, done = self.run_({}, {"0x1": "a", "0x2": "b", "0x3": "c", "0x4": "d"},
                                    score=self.scorer(fail={"0x1"}))
        self.assertEqual(done, 4)
        self.assertEqual(set(cache["scores"]), {"0x3", "0x4"})
        self.assertIn("checked", cache)
        self.assertIn("0x1", cache["checked"])                          # not retried every run
        self.assertIn("WARNING", out.getvalue())

    def test_a_failed_rescore_keeps_the_older_score(self):
        w = {"0x1": "a"}
        cache = {"board": w, "board_at": ago(1), "checked": {"0x1": ago(40)},
                 "scores": {"0x1": good("kept")}}
        with contextlib.redirect_stdout(io.StringIO()):
            cache, _ = self.run_(cache, w, score=self.scorer(fail={"0x1"}))
        self.assertEqual(cache["scores"]["0x1"]["name"], "kept")

    def test_scoring_is_capped_per_run(self):
        w = {f"0x{i}": "n" for i in range(100)}
        cache, done = self.run_({}, w, cap=signals.DEEP_MAX_SCORED_PER_RUN, chunk=signals.DEEP_CHUNK)
        self.assertEqual(done, signals.DEEP_MAX_SCORED_PER_RUN)


class Bar(unittest.TestCase):
    def test_the_deep_bar(self):
        scores = {
            "ok": good(),
            "low_win": good(win_rate=0.50),
            "low_roi": good(roi=0.05),
            "few_bets": good(n=29),
            "too_perfect": good(win_rate=0.95),
            "exactly_90": good(win_rate=0.90),
            "range_below_zero": good(roi_lo=-0.02),
            "no_range": good(roi_lo=None),
            "range_missing": {k: v for k, v in good().items() if k != "roi_lo"},
        }
        self.assertEqual(set(signals.deep_pool(scores)), {"ok", "exactly_90"})


class Shadow(InTempDir):
    def _cand(self, n, event=None, cond=None):
        h = {"w": {"name": "D", "size": 90, "entry": 0.55, "win_rate": 0.7, "roi": 0.3,
                   "n": 60, "alt": True}}
        return {"asset": f"A{n}", "condition": cond or f"c{n}", "title": "t",
                "event": event or f"e{n}", "outcome": "Yes", "traders": ["D"],
                "holders": h, "avg_entry": 0.55, "price": 0.56, "hours": 5}

    @staticmethod
    def _size(c, **kw):
        assert kw == {}                       # the normal price band, not the cheap one
        return dict(c, shares=1.8, stake=1.0, fee=0.03)

    def _run(self, book, cands, pool=None, strict=(), ledger=None, scan="SPORTS", seen=None):
        def find(p, **kw):
            if seen is not None:
                seen.append((dict(p), kw))
            return list(cands)
        with Patch(signals, SCAN=scan):
            return signals.shadow_deep(book, pool if pool is not None else {"w": good()},
                                       list(strict), ledger or {"bets": []},
                                       find=find, size=self._size)

    def test_records_deep_pool_copies_as_shadow_only(self):
        b = shadow.Book("s.json")
        seen = []
        self.assertEqual(self._run(b, [self._cand(1)], seen=seen), 1)
        r = b.bets[0]
        self.assertEqual((r["kind"], r["reason"], r["strategy"]), ("skipped", "deep_pool", "SPORTS"))
        self.assertEqual(r["deep"][0]["name"], "D")
        self.assertEqual(r["deep"][0]["alt"], True)
        self.assertEqual(r["traders"], ["D"])
        self.assertEqual(r["stake"], 1.0)
        self.assertEqual(seen[0][1], {})                  # normal price band and position size

    def test_only_in_the_deep_scans(self):
        for scan in ("GENERAL", "TECH"):
            b = shadow.Book("s.json")
            self.assertEqual(self._run(b, [self._cand(1)], scan=scan), 0)
            self.assertEqual(b.bets, [])
        for scan in ("SPORTS", "ESPORTS", "WEATHER"):
            self.assertEqual(self._run(shadow.Book(f"{scan}.json"), [self._cand(1)], scan=scan), 1)

    def test_empty_pool_means_no_work(self):
        b = shadow.Book("s.json")

        def never(pool, **kw):
            raise AssertionError("must not look")
        with Patch(signals, SCAN="SPORTS"):
            self.assertEqual(signals.shadow_deep(b, {}, [], {"bets": []}, find=never), 0)

    def test_leaves_out_what_the_normal_pool_already_has_and_markets_we_hold(self):
        b = shadow.Book("s.json")
        strict = [{"asset": "A1", "condition": "c1"}, {"asset": "A9", "condition": "c3"}]
        ledger = {"bets": [{"asset": "X", "condition": "c2"}]}
        cands = [self._cand(1), self._cand(2), self._cand(3), self._cand(4)]
        self.assertEqual(self._run(b, cands, strict=strict, ledger=ledger), 1)
        self.assertEqual([r["asset"] for r in b.bets], ["A4"])

    def test_the_other_side_of_a_held_market_is_left_out_too(self):
        b = shadow.Book("s.json")
        other_side = dict(self._cand(7, cond="cM"), asset="NO-TOKEN")
        self.assertEqual(self._run(b, [other_side], strict=[{"asset": "YES", "condition": "cM"}]), 0)

    def test_one_record_per_event_even_across_runs(self):
        b = shadow.Book("s.json")
        cands = [self._cand(1, event="m"), self._cand(2, event="m"), self._cand(3)]
        self.assertEqual(self._run(b, cands), 2)
        self.assertEqual(self._run(b, [self._cand(4, event="m"), self._cand(5)]), 1)
        self.assertEqual(self._run(b, cands), 0)           # same assets again: nothing new
        self.assertEqual(len(b.bets), 3)

    def test_per_run_cap(self):
        b = shadow.Book("s.json")
        cands = [self._cand(i) for i in range(30)]
        self.assertEqual(self._run(b, cands), signals.DEEP_MAX_PER_RUN)

    def test_the_wide_and_cheap_records_do_not_count_as_deep_events(self):
        b = shadow.Book("s.json")
        b.add("SPORTS", "skipped", "wide_low_roi", asset="Z", condition="cz", title="t",
              event="e1", price=0.5, shares=2, stake=1, fee=0.01)
        self.assertEqual(self._run(b, [self._cand(1)]), 1)

    def test_nothing_ever_touches_the_ledger(self):
        ledger = {"bets": [], "cash": 50.0}
        before = json.dumps(ledger)
        self._run(shadow.Book("s.json"), [self._cand(1)], ledger=ledger)
        self.assertEqual(json.dumps(ledger), before)


class Recording(InTempDir):
    """record_deep_pool: the never-fail wrapper that refreshes and records."""

    def _args(self, **over):
        h = {"w": {"name": "D", "size": 90, "entry": 0.55, "win_rate": 0.7, "roi": 0.3,
                   "n": 60, "alt": True}}
        cand = {"asset": "A1", "condition": "c1", "title": "t", "event": "e1", "outcome": "Yes",
                "traders": ["D"], "holders": h, "avg_entry": 0.55, "price": 0.56, "hours": 5}
        args = dict(board=lambda: {"0xdeep": "D"},
                    score=lambda traders: {w: good(n) for w, n in traders.items()},
                    find=lambda pool, **kw: [dict(cand)] if "0xdeep" in pool else [],
                    size=lambda c, **kw: dict(c, shares=1.8, stake=1.0, fee=0.03))
        args.update(over)
        return args

    def test_end_to_end_scores_saves_the_cache_and_records(self):
        b = shadow.Book("shadow_sports.json")
        with Patch(signals, SCAN="SPORTS", DEEP_FILE="traders_deep_sports.json"), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            added = signals.record_deep_pool(b, [], {"bets": []}, **self._args())
        self.assertEqual(added, 1)
        self.assertEqual(b.bets[0]["reason"], "deep_pool")
        with open("traders_deep_sports.json") as fh:
            saved = json.load(fh)
        self.assertEqual(set(saved), {"board_at", "board", "checked", "scores"})
        self.assertIn("0xdeep", saved["scores"])
        self.assertIn("1 board wallets", out.getvalue())

    def test_a_second_run_reuses_the_cache_and_records_nothing_new(self):
        b = shadow.Book("shadow_sports.json")
        calls = []
        args = self._args(board=lambda: calls.append("board") or {"0xdeep": "D"},
                          score=lambda t: calls.append("score") or {w: good(n) for w, n in t.items()})
        with Patch(signals, SCAN="SPORTS", DEEP_FILE="traders_deep_sports.json"), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(signals.record_deep_pool(b, [], {"bets": []}, **args), 1)
            self.assertEqual(signals.record_deep_pool(b, [], {"bets": []}, **args), 0)
        self.assertEqual(calls, ["board", "score"])         # the second run did no scoring

    def test_scans_outside_the_deep_set_do_nothing_and_write_nothing(self):
        b = shadow.Book("shadow_tech.json")

        def never():
            raise AssertionError("must not run")
        with Patch(signals, SCAN="TECH", DEEP_FILE="traders_deep_tech.json"):
            self.assertEqual(signals.record_deep_pool(b, [], {"bets": []}, board=never), 0)
        self.assertFalse(os.path.exists("traders_deep_tech.json"))

    def test_any_failure_is_swallowed_and_reported(self):
        b = shadow.Book("shadow_sports.json")

        def boom():
            raise RuntimeError("api down")
        out = io.StringIO()
        with Patch(signals, SCAN="SPORTS", DEEP_FILE="traders_deep_sports.json"), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(signals.record_deep_pool(b, [], {"bets": []}, board=boom), 0)
        self.assertIn("real bets are unaffected", out.getvalue())

    def test_a_failure_while_recording_is_swallowed_too(self):
        b = shadow.Book("shadow_sports.json")

        def boom(pool, **kw):
            raise RuntimeError("book api down")
        out = io.StringIO()
        with Patch(signals, SCAN="SPORTS", DEEP_FILE="traders_deep_sports.json"), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(signals.record_deep_pool(b, [], {"bets": []},
                                                      **self._args(find=boom)), 0)
        self.assertIn("real bets are unaffected", out.getvalue())

    def test_a_damaged_cache_file_is_treated_as_empty(self):
        for junk in ("not json at all", "[1, 2, 3]", "null"):
            with open("traders_deep_sports.json", "w") as fh:
                fh.write(junk)
            b = shadow.Book("shadow_sports.json")
            with Patch(signals, SCAN="SPORTS", DEEP_FILE="traders_deep_sports.json"), \
                    contextlib.redirect_stdout(io.StringIO()):
                added = signals.record_deep_pool(b, [], {"bets": []}, **self._args())
            self.assertEqual(added, 1, junk)
            if os.path.exists("shadow_sports.json"):
                os.remove("shadow_sports.json")


class Report(InTempDir):
    def test_report_has_its_own_line_for_the_deep_pool(self):
        import shadow_report
        b = shadow.Book("shadow_sports.json")
        r = b.add("SPORTS", "skipped", "deep_pool", asset="A1", condition="c", title="t",
                  price=0.6, shares=1.6, stake=1.0, fee=0.01, extra={"deep": [{"name": "D"}]})
        r.update(status="won", pnl=0.6)
        r = b.add("SPORTS", "skipped", "deep_pool", asset="A2", condition="c2", title="t",
                  price=0.6, shares=1.6, stake=1.0, fee=0.01)
        r.update(status="lost", pnl=-1.0)
        b.save()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            shadow_report.main()
        line = [ln for ln in buf.getvalue().splitlines() if "deep pool: ranks 51-200" in ln]
        self.assertEqual(len(line), 1)
        self.assertIn("1-1", line[0])
        self.assertNotIn("skipped: deep_pool", buf.getvalue())


class Wiring(unittest.TestCase):
    def read(self, *parts):
        with open(os.path.join(ROOT, *parts)) as fh:
            return fh.read()

    def test_the_deep_scans_are_real_scans_and_exclude_general_and_tech(self):
        self.assertEqual(signals.DEEP_SCANS, {"SPORTS", "ESPORTS", "WEATHER"})
        self.assertTrue(signals.DEEP_SCANS <= set(signals.CATEGORIES))

    def test_cache_file_is_per_scan_and_the_workflow_commits_it(self):
        self.assertEqual(signals.DEEP_FILE, "traders_deep_sports.json")   # tests import as SPORTS
        yml = self.read(".github", "workflows", "scan.yml")
        self.assertIn("traders_*.json", yml)
        self.assertTrue(fnmatch.fnmatch(signals.DEEP_FILE, "traders_*.json"))
        for scan in signals.DEEP_SCANS:
            # none of the other readers of traders_<scan>.json would pick this file up by mistake
            self.assertNotEqual(f"traders_deep_{scan.lower()}.json", f"traders_{scan.lower()}.json")

    def test_main_block_records_the_deep_pool_only_for_the_deep_scans(self):
        src = self.read("signals.py")
        self.assertIn("    if SCAN in DEEP_SCANS:\n        deep_added = record_deep_pool(book, candidates, ledger)", src)

    def test_sizes_and_limits_keep_a_run_short(self):
        # at most 40 wallets a run, checked against a 150-second budget
        self.assertLessEqual(signals.DEEP_MAX_SCORED_PER_RUN, 40)
        self.assertLessEqual(signals.DEEP_BUDGET_S, 150)
        self.assertEqual(signals.DEEP_BOARD_SIZE, 200)
        self.assertEqual(signals.DEEP_MAX_WIN_RATE, 0.90)


if __name__ == "__main__":
    unittest.main()
