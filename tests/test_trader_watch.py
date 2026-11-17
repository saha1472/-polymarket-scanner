"""Tests for the per-trader pause (trader_watch.py and its hook in signals.py).
It ships SHADOW ONLY, so the first thing to prove is that it changes no bet
until the switch is turned on."""
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_argv = sys.argv
sys.argv = ["signals.py", "SPORTS"]            # signals.py reads its scan from argv
import signals                                  # noqa: E402
sys.argv = _argv
import shadow                                   # noqa: E402
import simcore                                  # noqa: E402
import trader_watch as tw                       # noqa: E402

NOW = datetime(2026, 11, 17, 6, 0, tzinfo=timezone.utc)


def iso(days=0):
    return (NOW + timedelta(days=days)).isoformat()


def bet(pnl, traders=("Bad",), days=-5, status=None, **extra):
    return dict({"status": status or ("won" if pnl > 0 else "lost"), "pnl": pnl, "stake": 1.0,
                 "fee": 0.0, "traders": list(traders), "opened": iso(days), "asset": "a",
                 "price": 0.5, "shares": 2.0}, **extra)


def losing(n=12, trader="Bad", days=-5):
    """Clearly negative: 11 losses and one small win."""
    return [bet(-1.0, [trader], days) for _ in range(n - 1)] + [bet(0.5, [trader], days)]


def winning(n=12, trader="Bad", days=1):
    return [bet(0.6, [trader], days) for _ in range(n - 2)] + [bet(-1.0, [trader], days) for _ in range(2)]


class Flagging(unittest.TestCase):
    def test_a_trader_with_clearly_bad_copies_is_flagged(self):
        led = {"cash": 50, "bets": losing()}
        msgs = tw.update(led, [], now=iso())
        self.assertEqual(tw.flagged(led), {"Bad"})
        self.assertEqual(len(msgs), 1)
        self.assertIn("Trader flagged (shadow only: still being copied): Bad", msgs[0])
        self.assertIn("our last 12 copies", led["trader_watch"]["Bad"]["why"])
        self.assertEqual(led["trader_watch"]["Bad"]["since"], iso())

    def test_the_message_drops_the_shadow_note_when_live(self):
        led = {"cash": 50, "bets": losing()}
        self.assertIn("Trader flagged: Bad", tw.update(led, [], live=True, now=iso())[0])

    def test_too_few_copies_is_never_flagged_however_bad(self):
        led = {"cash": 50, "bets": [bet(-1.0) for _ in range(tw.MIN_COPIES - 1)]}
        self.assertEqual((tw.update(led, [], now=iso()), tw.flagged(led)), ([], set()))
        self.assertNotIn("trader_watch", led)              # nothing to store: ledger left alone

    def test_mildly_negative_is_not_enough(self):
        # like Tokor on 17 Nov: 14 copies around -16% with a wide range
        led = {"cash": 50, "bets": [bet(0.5) for _ in range(8)] + [bet(-1.0) for _ in range(6)]}
        tw.update(led, [], now=iso())
        self.assertEqual(tw.flagged(led), set())

    def test_good_and_bad_traders_are_judged_separately(self):
        led = {"cash": 50, "bets": losing(trader="Bad") + winning(trader="Good", days=-5)}
        tw.update(led, [], now=iso())
        self.assertEqual(tw.flagged(led), {"Bad"})

    def test_a_shared_bet_counts_for_each_trader_behind_it(self):
        shared = [bet(-1.0, ["Bad", "Other"]) for _ in range(12)]
        led = {"cash": 50, "bets": shared}
        tw.update(led, [], now=iso())
        self.assertEqual(tw.flagged(led), {"Bad", "Other"})

    def test_open_bets_and_damaged_numbers_are_ignored(self):
        led = {"cash": 50, "bets": [bet(-1.0, status="open") for _ in range(20)]
               + [bet(float("nan")) for _ in range(20)]}
        tw.update(led, [], now=iso())
        self.assertEqual(tw.flagged(led), set())

    def test_running_it_again_changes_nothing_and_says_nothing(self):
        led = {"cash": 50, "bets": losing()}
        tw.update(led, [], now=iso())
        before = dict(led["trader_watch"]["Bad"])
        self.assertEqual(tw.update(led, [], now=iso(1)), [])
        self.assertEqual(led["trader_watch"]["Bad"], before)

    def test_a_damaged_watch_list_is_rebuilt_not_fatal(self):
        led = {"cash": 50, "bets": losing(), "trader_watch": ["junk"]}
        tw.update(led, [], now=iso())
        self.assertEqual(tw.flagged(led), {"Bad"})
        self.assertEqual(tw.flagged({"trader_watch": "junk"}), set())
        self.assertEqual(tw.flagged({"trader_watch": {"X": "junk", "Y": {"paused": True}}}), {"Y"})


class ResumeAndReset(unittest.TestCase):
    def flagged_ledger(self):
        led = {"cash": 50, "bets": losing()}
        tw.update(led, [], now=iso())
        return led

    def test_good_results_since_the_flag_lift_it(self):
        led = self.flagged_ledger()
        led["bets"] += winning(days=1)                     # shadow mode: they were still copied
        msgs = tw.update(led, [], now=iso(3))
        self.assertEqual(tw.flagged(led), set())
        self.assertIn("Trader flag lifted", msgs[0])
        st = led["trader_watch"]["Bad"]
        self.assertEqual((st["judge_from"], st["history"][0]["how"]), (iso(3), "resumed"))

    def test_old_losses_do_not_count_toward_a_resume_or_a_second_flag(self):
        led = self.flagged_ledger()
        led["bets"] += winning(days=1)
        tw.update(led, [], now=iso(3))
        self.assertEqual(tw.update(led, [], now=iso(4)), [])       # the 12 old losses are before judge_from
        self.assertEqual(tw.flagged(led), set())

    def test_when_live_the_resume_is_judged_on_the_shadow_skips(self):
        led = self.flagged_ledger()
        skips = [dict(b, kind="skipped", reason="trader_paused", created=b.pop("opened"))
                 for b in winning(days=1)]
        other = [dict(b, kind="skipped", reason="per_event", created=b.pop("opened"))
                 for b in winning(days=1)]
        self.assertEqual(tw.update(led, other, live=True, now=iso(3)), [])   # wrong reason: not evidence
        self.assertIn("lifted", tw.update(led, skips, live=True, now=iso(3))[0])

    def test_a_few_good_bets_are_not_enough_to_lift_it(self):
        led = self.flagged_ledger()
        led["bets"] += winning(n=5, days=1)
        self.assertEqual(tw.update(led, [], now=iso(3)), [])
        self.assertEqual(tw.flagged(led), {"Bad"})

    def test_a_flag_expires_and_the_trader_starts_afresh(self):
        led = self.flagged_ledger()
        self.assertEqual(tw.update(led, [], now=iso(tw.RESET_DAYS - 1)), [])
        msgs = tw.update(led, [], now=iso(tw.RESET_DAYS))
        self.assertIn("expired", msgs[0])
        self.assertEqual(tw.flagged(led), set())
        self.assertEqual(tw.update(led, [], now=iso(tw.RESET_DAYS + 1)), [])   # not re-flagged on old bets
        led["bets"] += losing(days=tw.RESET_DAYS + 2)                          # fresh bad results
        tw.update(led, [], now=iso(tw.RESET_DAYS + 5))
        self.assertEqual(tw.flagged(led), {"Bad"})
        self.assertEqual(len(led["trader_watch"]["Bad"]["history"]), 1)

    def test_a_flagged_trader_who_left_the_pool_still_expires(self):
        led = {"cash": 50, "bets": [], "trader_watch": {"Gone": {"paused": True, "since": iso(-40)}}}
        self.assertIn("expired", tw.update(led, [], now=iso())[0])


class Blocking(unittest.TestCase):
    def test_a_signal_is_blocked_only_if_every_trader_behind_it_is_flagged(self):
        paused = {"Bad", "Worse"}
        self.assertTrue(tw.blocked({"traders": ["Bad"]}, paused))
        self.assertTrue(tw.blocked({"traders": ["Bad", "Worse"]}, paused))
        self.assertFalse(tw.blocked({"traders": ["Bad", "Good"]}, paused))
        self.assertFalse(tw.blocked({"traders": ["Good"]}, paused))
        self.assertFalse(tw.blocked({"traders": []}, paused))
        self.assertFalse(tw.blocked({}, paused))
        self.assertEqual(tw.hits({"traders": ["Good", "Worse", "Bad"]}, paused), ["Worse", "Bad"])


def sig(n, traders):
    return {"asset": f"N{n}", "condition": f"cN{n}", "event": f"eN{n}", "stake": 1, "fee": 0.01,
            "price": 0.6, "shares": 1.6, "order": "market", "traders": list(traders), "holders": {},
            "hours": 5, "title": "t", "outcome": "o", "avg_entry": 0.6,
            "profit_if_win": 0.5, "breakeven": 0.6}


class HookInSignals(unittest.TestCase):
    def setUp(self):
        self._cwd, self._tmp = os.getcwd(), tempfile.TemporaryDirectory()
        os.chdir(self._tmp.name)
        self._pause, signals.MANUAL_PAUSE = signals.MANUAL_PAUSE, {}
        self._live = signals.TRADER_PAUSE_LIVE

    def tearDown(self):
        signals.MANUAL_PAUSE, signals.TRADER_PAUSE_LIVE = self._pause, self._live
        os.chdir(self._cwd)
        self._tmp.cleanup()

    def ledger(self):
        return {"cash": 50, "bets": [], "trader_watch": {"Bad": {"paused": True, "since": iso(-1)}}}

    def test_it_ships_switched_off(self):
        import subprocess
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        code = (f"import sys; sys.path.insert(0, {root!r}); sys.argv = ['signals.py']; "
                "import signals; print(signals.TRADER_PAUSE_LIVE)")
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                             cwd=self._tmp.name, timeout=60)
        self.assertEqual(out.stdout.strip(), "False", out.stderr)

    def test_shadow_mode_still_bets_and_only_marks_the_bet(self):
        signals.TRADER_PAUSE_LIVE = False
        led, book = self.ledger(), shadow.Book("s.json")
        signals.open_paper_bets(led, [sig(1, ["Bad"]), sig(2, ["Good"]), sig(3, ["Good", "Bad"])], book)
        self.assertEqual([b["asset"] for b in led["bets"]], ["N1", "N2", "N3"])
        self.assertEqual([b.get("watch") for b in led["bets"]], [["Bad"], None, ["Bad"]])
        self.assertEqual(book.bets, [])

    def test_shadow_mode_bets_exactly_as_without_a_watch_list(self):
        signals.TRADER_PAUSE_LIVE = False
        a, b = self.ledger(), {"cash": 50, "bets": []}
        signals.open_paper_bets(a, [sig(1, ["Bad"])], shadow.Book("a.json"))
        signals.open_paper_bets(b, [sig(1, ["Bad"])], shadow.Book("b.json"))
        x, y = a["bets"][0], b["bets"][0]
        self.assertEqual(x.pop("watch"), ["Bad"])
        for bet_ in (x, y):
            bet_.pop("opened"), bet_.pop("ends"), bet_.pop("audit")
        self.assertEqual((x, a["cash"]), (y, b["cash"]))

    def test_live_mode_skips_only_signals_backed_solely_by_flagged_traders(self):
        signals.TRADER_PAUSE_LIVE = True
        led, book = self.ledger(), shadow.Book("s.json")
        signals.open_paper_bets(led, [sig(1, ["Bad"]), sig(2, ["Good"]), sig(3, ["Good", "Bad"])], book)
        self.assertEqual([b["asset"] for b in led["bets"]], ["N2", "N3"])
        self.assertEqual([(b["asset"], b["reason"], b["traders"]) for b in book.bets],
                         [("N1", "trader_paused", ["Bad"])])
        self.assertAlmostEqual(led["cash"], 50 - 2 * 1.01)

    def test_live_mode_with_everyone_flagged_places_nothing_and_does_not_fail(self):
        signals.TRADER_PAUSE_LIVE = True
        led, book = self.ledger(), shadow.Book("s.json")
        msgs = signals.open_paper_bets(led, [sig(1, ["Bad"]), sig(2, ["Bad"])], book)
        self.assertEqual((led["bets"], led["cash"], msgs), ([], 50, []))
        self.assertEqual(len(book.bets), 2)

    def test_the_ledger_with_a_watch_list_still_passes_validation(self):
        led = {"cash": 50, "bets": losing()}
        for b in led["bets"]:
            b.update(asset="a1")
        tw.update(led, [], now=iso())
        simcore.write_json_atomic("ledger_x.json", led)
        self.assertEqual(tw.flagged(simcore.load_ledger("ledger_x.json", 50)), {"Bad"})


class Report(unittest.TestCase):
    def test_lists_traders_worst_first_and_marks_flags(self):
        led = {"cash": 50, "bets": losing(trader="Bad") + winning(trader="Good", days=-5)
               + [bet(0.4, ["New"])]}
        tw.update(led, [], now=iso())
        text = tw.report(led, "X")
        self.assertLess(text.index("Bad"), text.index("Good"))
        self.assertIn("FLAGGED", text.split("Bad")[1].split("\n")[0])
        self.assertIn("too few to judge", text.split("New")[1].split("\n")[0])
        self.assertIn("no settled copies yet", tw.report({"bets": []}, "X"))


if __name__ == "__main__":
    unittest.main()
