"""Tests for the money-flow tag (flow.py and its hook in signals.py). Shadow only:
the main thing to prove is that it records the right numbers and can never
change or break a bet."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_argv = sys.argv
sys.argv = ["signals.py", "SPORTS"]            # signals.py reads its scan from argv
import signals                                  # noqa: E402
sys.argv = _argv
import flow                                     # noqa: E402
import shadow                                   # noqa: E402

NOW = 1794800000
A, B = "tokenA", "tokenB"


def row(token, usd, price=0.5, wallet="w1", mins_ago=10, side="BUY"):
    """A /v2/trades row in the real shape (fields checked against the live API)."""
    return {"proxy_wallet": wallet, "side": side, "token_id": token, "condition_id": "0xc",
            "size": usd / price, "price": price, "timestamp": NOW - mins_ago * 60,
            "outcome": "X", "outcome_index": 0, "event_slug": "e", "title": "t"}


class Summarise(unittest.TestCase):
    def test_balanced_market_trips_nothing(self):
        rows = [row(A, 600, wallet=f"a{i}") for i in range(5)] + \
               [row(B, 600, wallet=f"b{i}") for i in range(5)]
        rec = flow.summarise(rows, A, NOW)
        self.assertEqual((rec["usd"], rec["trades"], rec["wallets"]), (6000.0, 10, 10))
        self.assertEqual((rec["ours_share"], rec["side_share"]), (0.5, 0.5))
        self.assertEqual(rec["top_wallet_share"], 0.1)
        self.assertEqual(rec["flags"], [])

    def test_one_sided_money_on_our_outcome(self):
        rows = [row(A, 500, wallet=f"a{i}") for i in range(19)] + [row(B, 500, wallet="b")]
        rec = flow.summarise(rows, A, NOW)
        self.assertEqual((rec["ours_share"], rec["side_share"], rec["ours_is_bigger"]), (0.95, 0.95, True))
        self.assertEqual(rec["flags"], ["one_sided"])

    def test_one_sided_money_against_us(self):
        rows = [row(B, 500, wallet=f"b{i}") for i in range(19)] + [row(A, 500, wallet="a")]
        rec = flow.summarise(rows, A, NOW)
        self.assertEqual((rec["ours_share"], rec["side_share"], rec["ours_is_bigger"]), (0.05, 0.95, False))
        self.assertIn("one_sided", rec["flags"])

    def test_one_wallet_dominating_is_a_whale(self):
        rows = [row(A, 3000, wallet="BIG"), row(A, 200, wallet="BIG")] + \
               [row(B, 400, wallet=f"b{i}") for i in range(7)]
        rec = flow.summarise(rows, A, NOW)
        self.assertEqual(rec["top_wallet_usd"], 3200.0)
        self.assertAlmostEqual(rec["top_wallet_share"], 3200 / 6000, places=3)
        self.assertEqual(rec["flags"], ["whale"])

    def test_wallet_letter_case_does_not_split_a_wallet(self):
        rec = flow.summarise([row(A, 100, wallet="0xAB"), row(A, 100, wallet="0xab"),
                              row(B, 100, wallet="0xcd")], A, NOW)
        self.assertEqual(rec["wallets"], 2)

    def test_small_markets_are_not_flagged_for_being_lopsided(self):
        rec = flow.summarise([row(A, 50), row(A, 50, wallet="w2")], A, NOW)
        self.assertEqual((rec["side_share"], rec["usd"]), (1.0, 100.0))
        self.assertEqual(rec["flags"], [])                    # under MIN_FLOW_USD

    def test_price_move_is_newest_minus_oldest_for_our_outcome_only(self):
        rows = [row(A, 100, price=0.62, mins_ago=5), row(A, 100, price=0.50, mins_ago=200),
                row(A, 100, price=0.55, mins_ago=90), row(B, 100, price=0.10, mins_ago=1)]
        rec = flow.summarise(rows, A, NOW)                    # deliberately out of order
        self.assertEqual(rec["price_move"], 0.12)
        self.assertEqual(rec["flags"], ["fast_move"])
        self.assertIsNone(flow.summarise([row(A, 100), row(B, 100)], A, NOW)["price_move"])

    def test_old_trades_sells_dust_and_damaged_rows_are_ignored(self):
        rows = [row(A, 100),
                row(A, 9999, mins_ago=flow.WINDOW_H * 60 + 5),      # outside the window
                row(B, 9999, side="SELL"),
                row(B, 1),                                          # dust
                {"token_id": B, "price": "x", "size": 5, "timestamp": NOW},
                {"token_id": B, "price": 0.5, "size": float("nan"), "timestamp": NOW},
                {"token_id": None, "price": 0.5, "size": 500, "timestamp": NOW},
                row(B, 500, price=1.0), "junk", None]
        rec = flow.summarise(rows, A, NOW)
        self.assertEqual((rec["usd"], rec["trades"], rec["ours_share"]), (100.0, 1, 1.0))

    def test_nothing_usable_gives_none(self):
        self.assertIsNone(flow.summarise([], A, NOW))
        self.assertIsNone(flow.summarise(None, A, NOW))
        self.assertIsNone(flow.summarise([row(A, 100, mins_ago=9999)], A, NOW))

    def test_how_far_back_the_data_reached_and_truncation_are_recorded(self):
        rec = flow.summarise([row(A, 100, mins_ago=30), row(B, 100, mins_ago=120)], A, NOW)
        self.assertEqual((rec["reached_back_h"], rec["truncated"]), (2.0, False))
        full = [row(A, 10, wallet=f"w{i}", mins_ago=1) for i in range(flow.PAGE_SIZE * flow.MAX_PAGES)]
        self.assertTrue(flow.summarise(full, A, NOW)["truncated"])

    def test_the_record_is_plain_json(self):
        import json
        rec = flow.summarise([row(A, 700), row(B, 800, wallet="w2")], A, NOW)
        self.assertEqual(json.loads(json.dumps(rec, allow_nan=False)), rec)


class Tag(unittest.TestCase):
    def test_asks_for_the_right_market_and_stops_at_the_window(self):
        seen = {}

        def fetch(cond, stop):
            seen["cond"] = cond
            seen["stops"] = (stop({"timestamp": NOW - 60}), stop({"timestamp": NOW - 7 * 3600}),
                             stop({"timestamp": None}))
            return [row(A, 700), row(B, 800, wallet="w2")]
        rec = flow.tag({"condition": "0xc", "asset": A}, fetch, NOW)
        self.assertEqual(seen, {"cond": "0xc", "stops": (False, True, True)})
        self.assertEqual(rec["usd"], 1500.0)

    def test_api_down_or_no_condition_gives_none(self):
        self.assertIsNone(flow.tag({"condition": "0xc", "asset": A}, lambda c, s: None, NOW))
        self.assertIsNone(flow.tag({"asset": A}, lambda c, s: [row(A, 100)], NOW))

    def test_the_query_only_asks_for_real_sized_buys(self):
        q = flow.trade_params("0xc")
        self.assertEqual((q["condition"], q["side"], q["filter_type"], q["filter_amount"]),
                         ("0xc", "BUY", "CASH", flow.MIN_TRADE_USD))


def sig(n, **extra):
    return dict({"asset": f"N{n}", "condition": f"cN{n}", "event": f"eN{n}", "stake": 1, "fee": 0.01,
                 "price": 0.6, "shares": 1.6, "order": "market", "traders": ["A"], "holders": {},
                 "hours": 5, "title": "t", "outcome": "o", "avg_entry": 0.6,
                 "profit_if_win": 0.5, "breakeven": 0.6}, **extra)


class HookInSignals(unittest.TestCase):
    def setUp(self):
        self._cwd, self._tmp = os.getcwd(), tempfile.TemporaryDirectory()
        os.chdir(self._tmp.name)
        self._pause, signals.MANUAL_PAUSE = signals.MANUAL_PAUSE, {}

    def tearDown(self):
        signals.MANUAL_PAUSE = self._pause
        os.chdir(self._cwd)
        self._tmp.cleanup()

    def test_only_esports_is_tagged_for_now(self):
        self.assertEqual(signals.FLOW_SCANS, {"ESPORTS"})

    def test_tagging_adds_a_record_and_keeps_every_signal(self):
        import time
        fresh = [dict(row("N1", usd, wallet=w), timestamp=int(time.time()) - 600)   # 10 minutes ago, really
                 for usd, w in ((2000, "a"), (100, "b"))]
        sized = [sig(1), sig(2), sig(3)]
        got = signals.tag_flow(sized, fetch=lambda c, stop: (
            fresh if c == "cN1" else None if c == "cN2" else []))
        self.assertEqual((got, len(sized)), (1, 3))
        self.assertEqual(sized[0]["flow"]["flags"], ["one_sided", "whale"])
        self.assertNotIn("flow", sized[1])                    # API failed
        self.assertNotIn("flow", sized[2])                    # no recent trades

    def test_a_crash_inside_the_tag_never_reaches_the_bets(self):
        def boom(c, stop):
            raise RuntimeError("unexpected")
        sized = [sig(1), sig(2)]
        self.assertEqual(signals.tag_flow(sized, fetch=boom), 0)
        led = {"cash": 50, "bets": []}
        signals.open_paper_bets(led, sized, shadow.Book("s.json"))
        self.assertEqual([b["asset"] for b in led["bets"]], ["N1", "N2"])

    def test_only_the_first_few_signals_cost_an_api_call(self):
        calls = []
        sized = [sig(i) for i in range(signals.FLOW_MAX_PER_RUN + 5)]
        signals.tag_flow(sized, fetch=lambda c, stop: calls.append(c) or [])
        self.assertEqual(len(calls), signals.FLOW_MAX_PER_RUN)

    def test_the_record_is_stored_on_the_bet_and_changes_nothing_else(self):
        rec = flow.summarise([row("N1", 2000)], "N1", NOW)
        with_flow, without = {"cash": 50, "bets": []}, {"cash": 50, "bets": []}
        signals.open_paper_bets(with_flow, [sig(1, flow=rec)], shadow.Book("a.json"))
        signals.open_paper_bets(without, [sig(1)], shadow.Book("b.json"))
        a, b = with_flow["bets"][0], without["bets"][0]
        self.assertEqual(a.pop("flow"), rec)
        self.assertNotIn("flow", b)
        for bet in (a, b):
            bet.pop("opened"), bet.pop("ends"), bet.pop("audit")
        self.assertEqual(a, b)
        self.assertEqual(with_flow["cash"], without["cash"])

    def test_a_flagged_signal_is_still_bet(self):
        rec = flow.summarise([row("N1", 5000)], "N1", NOW)
        self.assertTrue(rec["flags"])
        led = {"cash": 50, "bets": []}
        signals.open_paper_bets(led, [sig(1, flow=rec)], shadow.Book("s.json"))
        self.assertEqual(len(led["bets"]), 1)

    def test_skipped_signals_keep_their_record_in_the_shadow_file(self):
        rec = flow.summarise([row("N1", 2000)], "N1", NOW)
        book = shadow.Book("s.json")
        signals.open_paper_bets({"cash": 0.5, "bets": []}, [sig(1, flow=rec), sig(2)], book)
        self.assertEqual([b.get("flow") for b in book.bets], [rec, None])
        book.save()
        self.assertEqual(shadow.Book("s.json").bets[0]["flow"], rec)   # survives a reload

    def test_the_ledger_with_a_flow_record_still_passes_validation(self):
        import simcore
        rec = flow.summarise([row("N1", 2000)], "N1", NOW)
        led = {"cash": 50, "bets": []}
        signals.open_paper_bets(led, [sig(1, flow=rec)], shadow.Book("s.json"))
        simcore.write_json_atomic("ledger_x.json", led)
        self.assertEqual(simcore.load_ledger("ledger_x.json", 50)["bets"][0]["flow"], rec)


def done(pnl, rec):
    return {"status": "won" if pnl > 0 else "lost", "pnl": pnl, "stake": 1.0, "fee": 0.0, "flow": rec}


class Report(unittest.TestCase):
    def test_groups_split_by_flag_and_by_side(self):
        clean = flow.summarise([row(t, 300, wallet=f"{t}{i}") for t in (A, B) for i in range(4)], A, NOW)
        with_money = flow.summarise([row(A, 500, wallet=f"a{i}") for i in range(19)] + [row(B, 500)], A, NOW)
        against = flow.summarise([row(B, 500, wallet=f"b{i}") for i in range(19)] + [row(A, 500)], A, NOW)
        tiny = flow.summarise([row(A, 50)], A, NOW)
        bets = [done(0.5, clean), done(-1, clean), done(0.4, with_money), done(-1, against),
                done(0.3, tiny), {"status": "open", "flow": clean}, {"status": "won", "pnl": 1, "stake": 1, "fee": 0}]
        g = {label: len(group) for label, group in flow.groups(bets)}
        self.assertEqual(g["all tagged"], 5)
        self.assertEqual((g["no flag"], g["any flag"]), (3, 2))
        self.assertEqual(g["one-sided, we are WITH the money"], 1)
        self.assertEqual(g["one-sided, we are AGAINST the money"], 1)
        self.assertEqual(g[f"under ${flow.MIN_FLOW_USD:,} of recent money"], 1)
        text = flow.report(bets, "X")
        self.assertIn("all tagged", text)
        self.assertIn("-7%", text)                  # "no flag": (0.5 - 1 + 0.3) / 3 per bet

    def test_no_data_yet(self):
        self.assertIn("no settled bets with a flow record yet", flow.report([], "X"))
        self.assertIn("no settled bets", flow.report([{"status": "won", "pnl": 1, "stake": 1, "fee": 0}], "X"))


if __name__ == "__main__":
    unittest.main()
