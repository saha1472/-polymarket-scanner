"""The per-trader cap (no trader may drive more than MAX_OPEN_PER_TRADER open bets) counts WALLETS, not display names:
two wallets can share a name and one wallet can change its name. No network: every call is faked."""
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
_argv = sys.argv
sys.argv = ["signals.py", "ESPORTS"]           # signals.py reads its scan from argv
import shadow    # noqa: E402
import signals   # noqa: E402
sys.argv = _argv
signals.MANUAL_PAUSE = {}

CAP = signals.MAX_OPEN_PER_TRADER


def holders(*pairs):
    return {w: {"name": n, "size": 9.0, "entry": 0.5} for w, n in pairs}


def open_bet(i, *pairs):
    h = holders(*pairs)
    return {"status": "open", "asset": f"O{i}", "condition": f"cO{i}", "event": f"eO{i}", "traders": [n for _, n in pairs],
            "holders": h, "stake": 1.0, "fee": 0.0}


def sig(i, *pairs, **kw):
    h = holders(*pairs)
    return dict({"asset": f"N{i}", "condition": f"cN{i}", "event": f"eN{i}", "stake": 1.0, "fee": 0.01, "price": 0.6,
                 "shares": 1.6, "order": "market", "traders": [n for _, n in pairs], "holders": h, "hours": 5,
                 "title": "t", "outcome": "o", "avg_entry": 0.6, "profit_if_win": 0.5, "breakeven": 0.6}, **kw)


class InTempDir(unittest.TestCase):
    def setUp(self):
        self._old = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory()
        os.chdir(self._tmp.name)

    def tearDown(self):
        os.chdir(self._old)
        self._tmp.cleanup()

    def run_cap(self, open_bets, signals_in, cash=50.0):
        led = {"cash": cash, "bets": list(open_bets)}
        book = shadow.Book("s.json")
        signals.open_paper_bets(led, signals_in, book)
        placed = [b["asset"] for b in led["bets"] if b["asset"].startswith("N")]
        why = {r["asset"]: r["reason"] for r in book.bets}
        return placed, why


class TraderKeys(unittest.TestCase):
    def test_wallets_lower_cased(self):
        self.assertEqual(signals.trader_keys({"holders": holders(("0xAbC", "x"), ("0xdef", "y"))}), {"0xabc", "0xdef"})

    def test_names_only_as_a_fallback_and_never_equal_to_a_wallet(self):
        self.assertEqual(signals.trader_keys({"traders": ["A", "B"]}), {"name:A", "name:B"})
        self.assertEqual(signals.trader_keys({"traders": ["0xabc"], "holders": {}}), {"name:0xabc"})
        self.assertNotEqual(signals.trader_keys({"traders": ["0xabc"]}), signals.trader_keys({"holders": holders(("0xabc", "z"))}))

    def test_nothing_to_go_on_gives_no_keys(self):
        self.assertEqual(signals.trader_keys({}), set())


class TheCap(InTempDir):
    def full(self, wallet="0xa", name="Alice", n=CAP):
        return [open_bet(i, (wallet, name)) for i in range(n)]

    def test_a_trader_at_the_cap_is_blocked(self):
        placed, why = self.run_cap(self.full(), [sig(1, ("0xa", "Alice"))])
        self.assertEqual((placed, why), ([], {"N1": "per_trader"}))

    def test_a_trader_below_the_cap_is_not(self):
        placed, why = self.run_cap(self.full(n=CAP - 1), [sig(1, ("0xa", "Alice"))])
        self.assertEqual((placed, why), (["N1"], {}))

    def test_two_wallets_with_the_same_name_are_two_traders(self):
        placed, why = self.run_cap(self.full("0xa", "Same"), [sig(1, ("0xb", "Same"))])
        self.assertEqual((placed, why), (["N1"], {}))              # name counting would have blocked it

    def test_one_wallet_under_a_new_name_is_still_one_trader(self):
        placed, why = self.run_cap(self.full("0xa", "OldName"), [sig(1, ("0xa", "NewName"))])
        self.assertEqual((placed, why), ([], {"N1": "per_trader"}))   # name counting would have let it through

    def test_the_wallet_case_does_not_matter(self):
        placed, why = self.run_cap(self.full("0xAbC", "Alice"), [sig(1, ("0xabc", "Alice"))])
        self.assertEqual(why, {"N1": "per_trader"})

    def test_the_cap_stays_strict_for_a_signal_with_several_traders(self):
        bets = self.full("0xa", "Alice")
        placed, why = self.run_cap(bets, [sig(1, ("0xa", "Alice"), ("0xb", "Bob"))])    # ANY trader at the cap blocks it
        self.assertEqual(why, {"N1": "per_trader"})
        placed, why = self.run_cap(bets, [sig(2, ("0xc", "Carol"), ("0xb", "Bob"))])
        self.assertEqual(placed, ["N2"])

    def test_bets_placed_in_this_run_count_towards_the_cap(self):
        sigs = [sig(i, ("0xa", "Alice")) for i in range(1, CAP + 2)]
        placed, why = self.run_cap([], sigs)
        self.assertEqual(len(placed), CAP)
        self.assertEqual(list(why.values()), ["per_trader"])

    def test_a_renamed_wallet_counts_across_a_run_too(self):
        sigs = [sig(1, ("0xa", "One")), sig(2, ("0xa", "Two")), sig(3, ("0xa", "Three"))]
        placed, why = self.run_cap([], sigs)
        self.assertEqual((len(placed), list(why.values())), (CAP, ["per_trader"]))

    def test_records_without_wallets_still_count_by_name(self):
        old = [{"status": "open", "asset": f"O{i}", "condition": f"cO{i}", "event": f"eO{i}", "traders": ["Old"],
                "stake": 1.0, "fee": 0.0} for i in range(CAP)]
        signal = sig(1, ("0xz", "Old"))
        signal["holders"] = {}                                         # a signal that also kept no wallets
        placed, why = self.run_cap(old, [signal])
        self.assertEqual(why, {"N1": "per_trader"})

    def test_a_name_only_record_is_not_confused_with_a_wallet(self):
        old = [{"status": "open", "asset": f"O{i}", "condition": f"cO{i}", "event": f"eO{i}", "traders": ["0xa"],
                "stake": 1.0, "fee": 0.0} for i in range(CAP)]
        placed, why = self.run_cap(old, [sig(1, ("0xa", "Alice"))])    # a trader literally NAMED "0xa" is not wallet 0xa
        self.assertEqual(placed, ["N1"])

    def test_old_name_only_bets_still_count_against_a_signal_that_has_wallets(self):
        old = [{"status": "open", "asset": f"O{i}", "condition": f"cO{i}", "event": f"eO{i}", "traders": ["Alice"],
                "stake": 1.0, "fee": 0.0} for i in range(CAP)]
        placed, why = self.run_cap(old, [sig(1, ("0xa", "Alice"))])
        self.assertEqual(why, {"N1": "per_trader"})                    # a restored or old record must not let a trader past the cap

    def test_old_name_only_bets_do_not_block_a_different_name(self):
        old = [{"status": "open", "asset": f"O{i}", "condition": f"cO{i}", "event": f"eO{i}", "traders": ["Alice"],
                "stake": 1.0, "fee": 0.0} for i in range(CAP)]
        placed, why = self.run_cap(old, [sig(1, ("0xa", "Bob"))])
        self.assertEqual(placed, ["N1"])

    def test_cap_keys_are_the_wallets_and_the_names(self):
        self.assertEqual(signals.cap_keys({"holders": holders(("0xAb", "Al")), "traders": ["Al"]}), {"0xab", "name:Al"})
        self.assertEqual(signals.cap_keys({"traders": ["Al"]}), {"name:Al"})

    def test_bets_with_wallets_are_never_counted_by_name(self):
        # two wallets share a name; the cap for one must not see the other's bets even though cap_keys includes the name
        placed, why = self.run_cap(self.full("0xa", "Same"), [sig(1, ("0xb", "Same"))])
        self.assertEqual(placed, ["N1"])

    def test_closed_bets_do_not_count(self):
        done = [dict(open_bet(i, ("0xa", "Alice")), status="won", pnl=0.5) for i in range(CAP)]
        placed, why = self.run_cap(done, [sig(1, ("0xa", "Alice"))])
        self.assertEqual(placed, ["N1"])


if __name__ == "__main__":
    unittest.main()
