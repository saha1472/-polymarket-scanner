"""Independent, READ-ONLY check of the paper ledgers.

For every settled bet it asks Polymarket's Data API (GET /v2/tokens) for the
official settled payout and checks:
  1. outcome  - the ledger's won / lost / split matches the official payout
  2. P&L      - pnl == remaining shares x payout + recovered - stake - fee
  3. holding  - for stop-loss bets: hold_pnl == original shares x payout - stake - fee
  4. cash     - each ledger reconciles: cash + money tied up in open/resting
                bets == starting bankroll + realized P&L

It never writes anything. Exit code 0 = all good, 1 = at least one mismatch.

Usage:  python verify_settlements.py              (all ledger*.json)
        python verify_settlements.py ledger.json  (specific files)
"""
import glob
import json
import sys

import requests

V2_TOKENS = "https://data-api.polymarket.com/v2/tokens"
START_BANKROLL = 50.0
TOL = 1e-6
SETTLED = ("won", "lost", "split", "stopped")


def official_payouts(assets, session=None):
    """{token_id: payout} for tokens Polymarket reports as resolved."""
    http = session or requests
    out = {}
    assets = sorted(assets)
    for i in range(0, len(assets), 40):
        chunk = assets[i:i + 40]
        r = http.get(V2_TOKENS, params={"token_id": ",".join(chunk)}, timeout=30)
        r.raise_for_status()
        for rec in r.json().get("data", []):
            p = rec.get("final_price")
            if rec.get("resolved") is True and isinstance(p, (int, float)):
                out[rec["token_id"]] = float(p)
    return out


def label(payout):
    return "won" if payout >= 0.99 else "lost" if payout <= 0.01 else "split"


def check_ledger(name, led, payouts):
    """Returns (rows, problems). rows: per-bet results for the report."""
    rows, problems = [], []
    for i, b in enumerate(led.get("bets", [])):
        if b.get("status") not in SETTLED:
            continue
        pay = payouts.get(b.get("asset"))
        where = f"{name} #{i} {b.get('title', '')[:60]}"
        if pay is None:
            rows.append((where, b["status"], None, "not resolved per Polymarket (yet)"))
            if b["status"] != "stopped":
                problems.append(f"{where}: settled as {b['status']} but Polymarket "
                                f"has no resolved payout for this token")
            continue
        notes = []
        if b["status"] != "stopped":
            if label(pay) != b["status"]:
                problems.append(f"{where}: ledger says {b['status']}, Polymarket payout {pay}")
                notes.append("OUTCOME MISMATCH")
            want = b["shares"] * pay + b.get("recovered", 0.0) - b["stake"] - b["fee"]
            if abs(want - b.get("pnl", float("nan"))) > TOL:
                problems.append(f"{where}: pnl {b.get('pnl')} != expected {want:.6f}")
                notes.append("PNL MISMATCH")
        if "hold_pnl" in b:
            full = b.get("orig_shares", b["shares"])
            want = full * pay - b["stake"] - b["fee"]
            if abs(want - b["hold_pnl"]) > TOL:
                problems.append(f"{where}: hold_pnl {b['hold_pnl']} != expected {want:.6f}")
                notes.append("HOLD MISMATCH")
        rows.append((where, b["status"], pay, ", ".join(notes) or "ok"))

    bets = led.get("bets", [])
    realized = sum(b.get("pnl", 0.0) for b in bets if b.get("status") in SETTLED)
    tied = sum(b["stake"] + b.get("fee", 0.0) - b.get("recovered", 0.0)
               for b in bets if b.get("status") in ("open", "resting"))
    gap = led["cash"] + tied - (START_BANKROLL + realized)
    if abs(gap) > 1e-6:
        problems.append(f"{name}: cash does not reconcile (off by {gap:+.6f})")
    return rows, problems, gap


def main(paths):
    ledgers = {p: json.load(open(p)) for p in paths}
    assets = {b["asset"] for led in ledgers.values() for b in led.get("bets", [])
              if b.get("status") in SETTLED and b.get("asset")}
    payouts = official_payouts(assets)
    all_problems, counts = [], {"ok": 0, "pending": 0, "bad": 0}
    for name, led in ledgers.items():
        rows, problems, gap = check_ledger(name, led, payouts)
        all_problems += problems
        ok = sum(1 for r in rows if r[3] == "ok")
        pending = sum(1 for r in rows if r[2] is None)
        counts["ok"] += ok
        counts["pending"] += pending
        counts["bad"] += len(rows) - ok - pending
        print(f"{name:28} settled checked {len(rows):4} | ok {ok:4} | "
              f"awaiting official result {pending:3} | cash gap {gap:+.6f}")
    print(f"\nTotal: {counts['ok']} ok, {counts['bad']} mismatched, "
          f"{counts['pending']} awaiting an official result")
    for p in all_problems:
        print("PROBLEM:", p)
    return 1 if all_problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:] or sorted(glob.glob("ledger*.json"))))
