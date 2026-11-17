"""Tests for the 17 Nov 2026 batch: monthly log files, DEAD needing two readings,
the hand-pause list for copy scans, the merged tag cache, non-JSON replies,
choosing the scan when signals.py is imported, and the scorecard."""
import json
import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_argv = sys.argv
sys.argv = ["signals.py", "SPORTS"]            # signals.py reads its scan from argv
import signals                                  # noqa: E402
sys.argv = _argv
import heartbeat                                # noqa: E402
import scorecard                                # noqa: E402
import shadow                                   # noqa: E402
import simcore                                  # noqa: E402
import weather                                  # noqa: E402
import wx_calibrate                             # noqa: E402
import wx_ensemble_eval                         # noqa: E402


class InTempDir(unittest.TestCase):
    def setUp(self):
        self._cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory()
        os.chdir(self._tmp.name)

    def tearDown(self):
        os.chdir(self._cwd)
        self._tmp.cleanup()


def at(year, month):
    return datetime(year, month, 15, tzinfo=timezone.utc)


# ---------- monthly log files ----------

class MonthlyLogs(InTempDir):
    def test_month_file_name(self):
        self.assertEqual(simcore.month_log("wx_shadow.jsonl", at(2026, 11)), "wx_shadow_2026-11.jsonl")
        self.assertEqual(simcore.month_log("rewards_log.jsonl", at(2027, 1)), "rewards_log_2027-01.jsonl")

    def test_new_lines_go_to_the_month_file_and_the_old_file_is_left_alone(self):
        with open("wx_shadow.jsonl", "w") as fh:
            fh.write('{"n": 1}\n')
        before = os.path.getsize("wx_shadow.jsonl")
        path = simcore.append_log("wx_shadow.jsonl", [{"n": 2}], at(2026, 11))
        self.assertEqual(path, "wx_shadow_2026-11.jsonl")
        self.assertEqual(os.path.getsize("wx_shadow.jsonl"), before)
        self.assertEqual([r["n"] for r in simcore.read_log("wx_shadow.jsonl")], [1, 2])

    def test_files_are_read_oldest_first_across_a_year_end(self):
        simcore.append_log("x.jsonl", [{"n": 3}], at(2027, 1))
        simcore.append_log("x.jsonl", [{"n": 2}], at(2026, 12))
        simcore.append_log("x.jsonl", [{"n": 1}], at(2026, 11))
        with open("x.jsonl", "w") as fh:
            fh.write('{"n": 0}\n')
        self.assertEqual(simcore.log_files("x.jsonl"),
                         ["x.jsonl", "x_2026-11.jsonl", "x_2026-12.jsonl", "x_2027-01.jsonl"])
        self.assertEqual([r["n"] for r in simcore.read_log("x.jsonl")], [0, 1, 2, 3])

    def test_appending_twice_keeps_both_and_leaves_no_temp_file(self):
        simcore.append_log("x.jsonl", [{"n": 1}], at(2026, 11))
        simcore.append_log("x.jsonl", [{"n": 2}, {"n": 3}], at(2026, 11))
        self.assertEqual([r["n"] for r in simcore.read_log("x.jsonl")], [1, 2, 3])
        self.assertEqual(os.listdir("."), ["x_2026-11.jsonl"])

    def test_a_file_missing_its_last_newline_does_not_glue_two_records_together(self):
        with open("x_2026-11.jsonl", "w") as fh:
            fh.write('{"n": 1}')
        simcore.append_log("x.jsonl", [{"n": 2}], at(2026, 11))
        self.assertEqual([r["n"] for r in simcore.read_log("x.jsonl")], [1, 2])

    def test_nan_is_refused_and_nothing_is_written(self):
        with self.assertRaises(ValueError):
            simcore.append_log("x.jsonl", [{"n": float("nan")}], at(2026, 11))
        self.assertEqual(os.listdir("."), [])

    def test_damaged_lines_are_skipped(self):
        with open("x_2026-11.jsonl", "w") as fh:
            fh.write('{"n": 1}\n{broken\n\n{"n": 2}\n')
        self.assertEqual([r["n"] for r in simcore.read_log("x.jsonl")], [1, 2])

    def test_other_files_with_a_similar_name_are_not_picked_up(self):
        for name in ("x_notes.jsonl", "x_2026-11.jsonl.bad", "x_2026-11.json", "xx_2026-11.jsonl"):
            with open(name, "w") as fh:
                fh.write('{"n": 9}\n')
        self.assertEqual(simcore.log_files("x.jsonl"), [])
        self.assertEqual(simcore.read_log("x.jsonl"), [])

    def test_no_log_at_all(self):
        self.assertEqual(simcore.read_log("x.jsonl"), [])
        self.assertIsNone(wx_ensemble_eval.load("x.jsonl"))

    def test_the_readers_see_old_and_monthly_files_together(self):
        with open(wx_calibrate.SHADOW_LOG, "w") as fh:
            fh.write('{"n": 1}\n')
        simcore.append_log(wx_calibrate.SHADOW_LOG, [{"n": 2}], at(2026, 12))
        self.assertEqual([r["n"] for r in wx_calibrate.load_records(wx_calibrate.SHADOW_LOG)], [1, 2])
        self.assertEqual([r["n"] for r in wx_ensemble_eval.load(wx_calibrate.SHADOW_LOG)], [1, 2])

    def test_heartbeat_adds_up_rewards_from_every_file(self):
        row = {"time": "2026-11-16T00:00:00+00:00", "condition": "c1", "hours": 12,
               "accrued": [1.0, 2.0], "fill_pnl": -0.5, "capital": 30}
        with open(heartbeat.REWARDS_LOG, "w") as fh:
            fh.write(json.dumps(row) + "\n")
        simcore.append_log(heartbeat.REWARDS_LOG, [dict(row, time="2026-12-02T00:00:00+00:00")],
                           at(2026, 12))
        text = heartbeat.rewards_line("2026-12-01T00:00:00+00:00")
        self.assertIn("last 24h reward $1.00-$2.00", text)             # only the December line
        self.assertIn("net +$1.00 to +$3.00", text)                    # both lines in the whole record


# ---------- DEAD: one reading is not enough ----------

def ts_local(tz, d, hour, minute=0):
    return int(datetime(d.year, d.month, d.day, hour, minute, tzinfo=ZoneInfo(tz)).timestamp())


class DeadNeedsTwoReadings(unittest.TestCase):
    TZ, D = "UTC", date(2026, 11, 7)

    def event(self, kind="lowest", unit="C"):
        return {"station": "X", "date": self.D, "kind": kind, "unit": unit}

    def obs(self, temps):
        return {"X": [(ts_local(self.TZ, self.D, h), t) for h, t in enumerate(temps)]}

    def test_default_is_unchanged(self):
        obs = self.obs([25.0, 22.0, 24.0, 23.5])
        self.assertEqual(weather.observed_extreme(self.event(), obs, self.TZ), 22)
        self.assertEqual(weather.observed_extreme(self.event("highest"), obs, self.TZ), 25)

    def test_a_single_odd_reading_is_ignored_when_two_are_required(self):
        obs = self.obs([25.0, 22.0, 24.0, 23.5])           # 22.0 is the lone outlier
        self.assertEqual(weather.observed_extreme(self.event(), obs, self.TZ, confirm=2), 24)  # 23.5 -> 24
        self.assertEqual(weather.observed_extreme(self.event("highest"), obs, self.TZ, confirm=2), 24)

    def test_two_readings_at_the_extreme_confirm_it(self):
        obs = self.obs([25.0, 22.0, 22.0, 24.0])
        self.assertEqual(weather.observed_extreme(self.event(), obs, self.TZ, confirm=2), 22)

    def test_one_reading_in_the_day_is_not_enough(self):
        obs = self.obs([22.0])
        self.assertEqual(weather.observed_extreme(self.event(), obs, self.TZ), 22)
        self.assertIsNone(weather.observed_extreme(self.event(), obs, self.TZ, confirm=2))

    def test_the_whole_day_rule_still_applies(self):
        obs = {"X": [(ts_local(self.TZ, self.D, h), 20.0) for h in (15, 16, 17)]}
        self.assertIsNone(weather.observed_extreme(self.event(), obs, self.TZ, confirm=2))

    def _dead(self, temps_c):
        """DEAD signals for a Miami-style low market with buckets 72-73F and 74-75F."""
        now = datetime(2026, 11, 7, 10, 30, tzinfo=timezone.utc)
        ev = dict(self.event(unit="F"), slug="miami", buckets=[
            {"label": "72-73°F", "lo": 72, "hi": 73, "yes_bid": 0.05},
            {"label": "74-75°F", "lo": 74, "hi": 75, "yes_bid": 0.03},
            {"label": "76-77°F", "lo": 76, "hi": 77, "yes_bid": 0.03}])
        obs = {"X": [(ts_local(self.TZ, self.D, h), t) for h, t in enumerate(temps_c)]}
        old = weather.now_utc
        weather.now_utc = lambda: now
        try:
            return [(b["label"], why) for _, _, b, _, why in
                    weather.dead_signals([ev], obs, {"X": {"tz": self.TZ}})]
        finally:
            weather.now_utc = old

    def test_the_miami_case_one_low_reading_no_longer_rules_out_the_next_bucket(self):
        # 22.2C = 72F once; every other reading is 23.5C or more (74F+)
        got = self._dead([24.0, 23.9, 23.5, 22.2, 23.6, 23.8, 24.0, 24.2, 24.5, 25.0, 25.5])
        self.assertEqual(weather.DEAD_CONFIRM, 2)
        self.assertEqual([label for label, _ in got], ["76-77°F"])   # not 74-75°F any more

    def test_a_confirmed_low_still_rules_buckets_out_and_the_message_reads_properly(self):
        got = self._dead([24.0, 23.9, 22.3, 22.2, 23.6, 23.8, 24.0, 24.2, 24.5, 25.0, 25.5])
        self.assertEqual([label for label, _ in got], ["74-75°F", "76-77°F"])
        self.assertIn("observed low so far 72°F", got[0][1])
        self.assertNotIn("lowe", got[0][1])

    def test_the_rule_is_stored_with_every_dead_bet(self):
        self.assertEqual(weather.strategy_settings("DEAD")["confirm"], 2)


# ---------- copy scans paused by hand ----------

def sig(n):
    return {"asset": f"N{n}", "condition": f"cN{n}", "event": f"eN{n}", "stake": 1, "fee": 0.01,
            "price": 0.6, "shares": 1.6, "order": "market", "traders": ["A"], "holders": {},
            "hours": 5, "title": "t", "outcome": "o", "avg_entry": 0.6,
            "profit_if_win": 0.5, "breakeven": 0.6}


class ManualPauseForCopyScans(InTempDir):
    def setUp(self):
        super().setUp()
        self._pause = signals.MANUAL_PAUSE

    def tearDown(self):
        signals.MANUAL_PAUSE = self._pause
        super().tearDown()

    def test_a_paused_scan_bets_nothing_and_records_every_signal(self):
        signals.MANUAL_PAUSE = {signals.SCAN: "test"}
        led, book = {"cash": 50, "bets": []}, shadow.Book("s.json")
        msgs = signals.open_paper_bets(led, [sig(1), sig(2)], book)
        self.assertEqual((led["bets"], led["cash"], msgs), ([], 50, []))
        self.assertEqual([(b["asset"], b["reason"]) for b in book.bets],
                         [("N1", "manual_pause"), ("N2", "manual_pause")])

    def test_a_scan_that_is_not_listed_bets_as_before(self):
        signals.MANUAL_PAUSE = {"SOMETHING_ELSE": "test"}
        led, book = {"cash": 50, "bets": []}, shadow.Book("s.json")
        signals.open_paper_bets(led, [sig(1)], book)
        self.assertEqual([b["asset"] for b in led["bets"]], ["N1"])
        self.assertEqual(book.bets, [])

    def test_open_bets_of_a_paused_scan_still_settle(self):
        signals.MANUAL_PAUSE = {signals.SCAN: "test"}
        led = {"cash": 49, "bets": [{"asset": "a1", "condition": "c1", "status": "open",
                                     "shares": 2.0, "stake": 1.0, "fee": 0.0, "price": 0.5,
                                     "title": "t", "outcome": "o"}]}
        old = signals.get
        signals.get = lambda url, params=None, raw=False: [
            {"token_id": "a1", "resolved": True, "final_price": 1.0}]
        try:
            signals.settle(led)
        finally:
            signals.get = old
        self.assertEqual((led["bets"][0]["status"], led["cash"]), ("won", 51.0))

    def test_sports_is_the_scan_paused_today(self):
        # a fresh copy of the file: other test files empty the list for their own checks
        import subprocess
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        code = (f"import sys; sys.path.insert(0, {root!r}); sys.argv = ['signals.py']; import signals; "
                "print(sorted(signals.MANUAL_PAUSE)); "
                "print(set(signals.MANUAL_PAUSE) <= set(signals.CATEGORIES) | {'GENERAL'})")
        with tempfile.TemporaryDirectory() as tmp:
            out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                                 cwd=tmp, timeout=60)
        self.assertEqual(out.stdout.split("\n")[:2], ["['SPORTS']", "True"], out.stderr)


# ---------- tag cache, non-JSON replies, scan choice ----------

class TagCacheIsMerged(InTempDir):
    def setUp(self):
        super().setUp()
        self._tags = dict(signals.TAGS)
        signals.TAGS.clear()

    def tearDown(self):
        signals.TAGS.clear()
        signals.TAGS.update(self._tags)
        super().tearDown()

    def test_lookups_saved_by_another_scan_are_kept(self):
        with open(signals.TAGS_FILE, "w") as fh:
            json.dump({"other-scan-event": ["weather"], "both": ["old"]}, fh)
        signals.TAGS.update({"my-event": ["sports"], "both": ["new"]})
        signals.save_tags()
        with open(signals.TAGS_FILE) as fh:
            self.assertEqual(json.load(fh), {"other-scan-event": ["weather"],
                                             "my-event": ["sports"], "both": ["new"]})
        self.assertEqual(os.listdir("."), [signals.TAGS_FILE])       # no temp file left

    def test_a_damaged_cache_on_disk_is_replaced(self):
        with open(signals.TAGS_FILE, "w") as fh:
            fh.write("[1, 2")
        signals.TAGS["my-event"] = ["sports"]
        signals.save_tags()
        with open(signals.TAGS_FILE) as fh:
            self.assertEqual(json.load(fh), {"my-event": ["sports"]})

    def test_an_oversized_cache_is_reset(self):
        with open(signals.TAGS_FILE, "w") as fh:
            json.dump({"old": []}, fh)
        signals.TAGS.update({f"e{i}": [] for i in range(30001)})
        signals.save_tags()
        with open(signals.TAGS_FILE) as fh:
            self.assertEqual(json.load(fh), {})


class FakeResp:
    def __init__(self, status, body):
        self.status_code, self._body, self.text = status, body, str(body)

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class FakeSession:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), 0

    def get(self, url, params=None, timeout=None):
        self.calls += 1
        return self.replies.pop(0)


class NonJsonReplies(unittest.TestCase):
    def setUp(self):
        self._sleep = (signals.time.sleep, weather.time.sleep)
        self._sessions = (signals.SESSION, weather.SESSION)
        signals.time.sleep = weather.time.sleep = lambda s: None

    def tearDown(self):
        signals.time.sleep, weather.time.sleep = self._sleep
        signals.SESSION, weather.SESSION = self._sessions

    def test_response_json(self):
        self.assertEqual(simcore.response_json(FakeResp(200, {"a": 1})), {"a": 1})
        self.assertIsNone(simcore.response_json(FakeResp(200, ValueError("not json"))))
        self.assertIsNone(simcore.response_json(FakeResp(200, json.JSONDecodeError("x", "", 0))))

    def test_signals_get_retries_a_garbled_reply_instead_of_crashing(self):
        signals.SESSION = FakeSession([FakeResp(200, ValueError("html")), FakeResp(200, {"data": [1]})])
        self.assertEqual(signals.get("u"), [1])
        self.assertEqual(signals.SESSION.calls, 2)

    def test_signals_get_gives_up_with_none(self):
        signals.SESSION = FakeSession([FakeResp(200, ValueError("html"))] * 3)
        self.assertIsNone(signals.get("u"))

    def test_weather_get_does_the_same(self):
        weather.SESSION = FakeSession([FakeResp(200, ValueError("html")), FakeResp(200, [7])])
        self.assertEqual(weather.get("u"), [7])
        weather.SESSION = FakeSession([FakeResp(200, ValueError("html"))] * 4)
        self.assertIsNone(weather.get("u"))


class ScanChoiceOnImport(unittest.TestCase):
    def run_py(self, code, env=None):
        import subprocess
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        full = dict(os.environ, **(env or {}))
        full.pop("SIGNALS_SCAN", None) if env is None else None
        with tempfile.TemporaryDirectory() as tmp:
            return subprocess.run([sys.executable, "-c", f"import sys; sys.path.insert(0, {root!r}); {code}"],
                                  capture_output=True, text=True, cwd=tmp, env=full, timeout=60)

    def test_someone_elses_arguments_do_not_stop_an_import(self):
        out = self.run_py("sys.argv = ['python -m unittest', 'discover']; import signals; print(signals.SCAN)")
        self.assertEqual((out.returncode, out.stdout.strip()), (0, "GENERAL"), out.stderr)

    def test_a_scan_named_on_the_command_line_is_still_used(self):
        out = self.run_py("sys.argv = ['signals.py', 'esports']; import signals; "
                          "print(signals.SCAN, signals.LEDGER_FILE)")
        self.assertEqual(out.stdout.strip(), "ESPORTS ledger_esports.json", out.stderr)

    def test_the_environment_variable_wins(self):
        out = self.run_py("sys.argv = ['x', 'SPORTS']; import signals; print(signals.SCAN)",
                          env={"SIGNALS_SCAN": "tech"})
        self.assertEqual(out.stdout.strip(), "TECH", out.stderr)

    def test_running_it_with_a_wrong_scan_still_stops(self):
        import subprocess
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env = {k: v for k, v in os.environ.items() if k != "SIGNALS_SCAN"}
        with tempfile.TemporaryDirectory() as tmp:
            out = subprocess.run([sys.executable, os.path.join(root, "signals.py"), "NOPE"],
                                 capture_output=True, text=True, cwd=tmp, env=env, timeout=60)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("Unknown scan NOPE", out.stderr)


class SharedHelpers(unittest.TestCase):
    def test_both_bots_use_the_same_helpers(self):
        for mod in (signals, weather):
            self.assertIs(mod.num, simcore.num)
            self.assertIs(mod.pmap, simcore.pmap)
            self.assertIs(mod.read_json, simcore.read_json)

    def test_pmap_keeps_order_and_handles_nothing(self):
        self.assertEqual(simcore.pmap(lambda x: x * 2, [3, 1, 2]), [6, 2, 4])
        self.assertEqual(simcore.pmap(lambda x: x, []), [])

    def test_notify_without_a_webhook_or_lines_is_a_quiet_success(self):
        self.assertTrue(simcore.notify(["hello"], url=""))
        self.assertTrue(simcore.notify([], url="https://example.invalid/hook"))


# ---------- scorecard ----------

def settled(pnls, stake=1.0):
    return {"cash": 50, "bets": [{"status": "won" if p > 0 else "lost", "pnl": p, "stake": stake,
                                  "fee": 0.0} for p in pnls]}


class Scorecard(unittest.TestCase):
    def test_the_bar_rises_with_the_number_of_strategies(self):
        self.assertAlmostEqual(scorecard.z_needed(1), 1.96, places=2)
        self.assertAlmostEqual(scorecard.z_needed(10), 2.81, places=2)
        self.assertGreater(scorecard.z_needed(20), scorecard.z_needed(10))
        self.assertAlmostEqual(scorecard.z_needed(0), 1.96, places=2)       # never divides by zero

    def test_too_few_bets_gets_no_verdict(self):
        row = scorecard.score("x", settled([0.5] * 5 + [-1.0] * 5), 2.0)
        self.assertIn("too few bets", row["verdict"])
        self.assertIsNone(row["lo"])

    def test_a_clear_loser_and_a_clear_winner(self):
        lose = scorecard.score("x", settled([-1.0] * 45 + [0.3] * 5), 2.8)
        win = scorecard.score("x", settled([0.5] * 45 + [-1.0] * 5), 2.8)
        self.assertEqual((lose["verdict"], win["verdict"]), ("PROVEN LOSER", "PROVEN WINNER"))

    def test_a_coin_flip_is_no_evidence(self):
        row = scorecard.score("x", settled([1.0, -1.0] * 20), 2.8)
        self.assertEqual(row["verdict"], "no evidence either way")
        self.assertEqual(row["n"], 40)
        self.assertIsNone(row["need"])                                      # mean is exactly zero

    def test_a_small_edge_is_promising_but_needs_more_bets(self):
        row = scorecard.score("x", settled([0.25] * 70 + [-1.0] * 30), 2.8)   # -12.5% per bet
        self.assertEqual(row["verdict"], "probably losing")
        self.assertGreater(row["need"], row["n"])
        good = scorecard.score("x", settled([0.7] * 70 + [-1.0] * 30), 2.8)   # +19% per bet
        self.assertEqual(good["verdict"], "promising, not proven")

    def test_bets_to_prove_matches_the_formula(self):
        # sd = 1, mean = 0.1, z = 2  ->  (2 * 1 / 0.1)^2 = 400
        self.assertEqual(scorecard.bets_to_prove(100, 0.1, 0.1, 2.0), 400)
        self.assertEqual(scorecard.bets_to_prove(100, -0.1, 0.1, 2.0), 400)
        self.assertIsNone(scorecard.bets_to_prove(1, 0.1, float("inf"), 2.0))

    def test_identical_results_do_not_crash(self):
        row = scorecard.score("x", settled([0.2] * 30), 2.8)
        self.assertIn("nothing to judge", row["verdict"])

    def test_only_strategies_with_enough_bets_raise_the_bar(self):
        rows, z, judged = scorecard.build({"big": settled([1.0, -1.0] * 20),
                                           "tiny": settled([1.0, -1.0]),
                                           "empty": {"cash": 50, "bets": []}})
        self.assertEqual(judged, 1)
        self.assertAlmostEqual(z, 1.96, places=2)
        self.assertEqual(rows[0]["name"], "big")                            # judged rows first

    def test_open_and_damaged_bets_are_left_out(self):
        led = settled([0.5] * 30)
        led["bets"] += [{"status": "open", "stake": 1, "fee": 0},
                        {"status": "won", "pnl": float("nan"), "stake": 1, "fee": 0}]
        row = scorecard.score("x", led, 2.8)
        self.assertEqual(row["n"], 30)
        self.assertAlmostEqual(row["pnl"], 15.0)

    def test_both_layouts_render(self):
        built = scorecard.build({"a": settled([-1.0] * 45 + [0.3] * 5), "b": {"cash": 50, "bets": []}})
        text, md = scorecard.render(*built), scorecard.render(*built, markdown=True)
        self.assertIn("PROVEN LOSER", text)
        self.assertIn("| a | 50 |", md)
        self.assertIn("n/a", text)                                          # the empty ledger


if __name__ == "__main__":
    unittest.main()
