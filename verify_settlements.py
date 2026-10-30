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
import math
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


def finite(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def label(payout):
    return "won" if payout >= 0.99 else "lost" if payout <= 0.01 else "split"


# Fields each kind of bet MUST have (finite numbers). Nothing on these lists
# is ever defaulted to zero; `recovered` is the only genuinely optional field.
REQUIRED = {
    "open":      ("stake", "fee", "shares", "price"),
    "resting":   ("stake", "fee", "shares", "price"),
    "won":       ("stake", "fee", "shares", "price", "pnl"),
    "lost":      ("stake", "fee", "shares", "price", "pnl"),
    "split":     ("stake", "fee", "shares", "price", "pnl"),
    "stopped":   ("stake", "fee", "shares", "price", "pnl"),
    "cancelled": ("stake",),
}
OPTIONAL = ("recovered", "orig_shares", "hold_pnl")


def field_problems(b):
    """Missing / non-finite / negative required fields, and non-finite optional ones."""
    bad = [k for k in REQUIRED.get(b.get("status"), ()) if not finite(b.get(k))]
    bad += [k for k in OPTIONAL if k in b and not finite(b[k])]
    neg = [k for k in ("stake", "fee", "shares", "recovered", "orig_shares")
           if finite(b.get(k)) and b[k] < 0]
    return bad, neg


def check_ledger(name, led, payouts):
    """Returns (rows, problems, cash gap). rows: per-bet results for the report.
    A row note of "legacy" marks an old stopped bet whose sale proceeds weren't
    recorded: its P&L can only be range-checked, not re-derived."""
    rows, problems = [], []
    bets = led.get("bets", [])
    fields_ok = True
    for i, b in enumerate(bets):
        where = f"{name} #{i} {str(b.get('title', ''))[:60]}"
        if b.get("status") not in REQUIRED:
            problems.append(f"{where}: unknown status {b.get('status')!r}")
            fields_ok = False
            continue
        bad, neg = field_problems(b)
        if bad or neg:
            fields_ok = False
            msg = []
            if bad:
                msg.append(f"missing or non-finite {', '.join(bad)}")
            if neg:
                msg.append(f"negative {', '.join(neg)}")
            problems.append(f"{where}: {'; '.join(msg)}")
            if b["status"] in SETTLED:
                rows.append((where, b["status"], None, "MISSING/NON-FINITE FIELDS"))
            continue
        if b["status"] not in SETTLED:
            continue

        pay = payouts.get(b["asset"])
        if pay is None:
            rows.append((where, b["status"], None, "not resolved per Polymarket (yet)"))
            if b["status"] != "stopped":
                problems.append(f"{where}: settled as {b['status']} but Polymarket "
                                f"has no resolved payout for this token")
            continue
        notes = []
        rec = b.get("recovered", 0.0)

        if b["status"] == "stopped":
            if "recovered" in b:
                # modern stop: everything was sold, proceeds recorded
                want = rec - b["stake"] - b["fee"]
                if abs(want - b["pnl"]) > TOL:
                    problems.append(f"{where}: stopped pnl {b['pnl']} != recovered - stake - "
                                    f"fee = {want:.6f}")
                    notes.append("PNL MISMATCH")
            else:
                # legacy stop (before sale proceeds were recorded): the loss can't be
                # re-derived, only bounded: proceeds were between $0 and $1 per share
                lo = -b["stake"] - b["fee"]
                hi = b["shares"] - b["stake"] - b["fee"]
                if not lo - TOL <= b["pnl"] <= hi + TOL:
                    problems.append(f"{where}: legacy stopped pnl {b['pnl']} outside the "
                                    f"possible range [{lo:.4f}, {hi:.4f}]")
                    notes.append("PNL OUT OF RANGE")
                else:
                    notes.append("legacy")
        else:
            if label(pay) != b["status"]:
                problems.append(f"{where}: ledger says {b['status']}, Polymarket payout {pay}")
                notes.append("OUTCOME MISMATCH")
            want = b["shares"] * pay + rec - b["stake"] - b["fee"]
            if abs(want - b["pnl"]) > TOL:
                problems.append(f"{where}: pnl {b['pnl']} != expected {want:.6f}")
                notes.append("PNL MISMATCH")

        # a stop-loss touched this bet (fully or partly): holding baseline required
        if ("orig_shares" in b or b["status"] == "stopped") and "hold_pnl" not in b:
            problems.append(f"{where}: resolved bet touched by a stop-loss has no hold_pnl "
                            f"(if it resolved after the bot's last run, re-check after the next run)")
            notes.append("HOLD MISSING")
        if "hold_pnl" in b:
            full = b.get("orig_shares", b["shares"])
            want = full * pay - b["stake"] - b["fee"]
            if abs(want - b["hold_pnl"]) > TOL:
                problems.append(f"{where}: hold_pnl {b['hold_pnl']} != expected {want:.6f}")
                notes.append("HOLD MISMATCH")
        bad_notes = [n for n in notes if n != "legacy"]
        rows.append((where, b["status"], pay,
                     ", ".join(bad_notes) if bad_notes else ("legacy" if notes else "ok")))

    if not finite(led.get("cash")):
        problems.append(f"{name}: cash is missing or non-finite ({led.get('cash')!r})")
        return rows, problems, float("nan")
    if not fields_ok:
        problems.append(f"{name}: cash not reconciled because some bets have invalid fields")
        return rows, problems, float("nan")
    # all required fields are known-good here; only `recovered` may be absent
    realized = sum(b["pnl"] for b in bets if b["status"] in SETTLED)
    tied = sum(b["stake"] + b["fee"] - b.get("recovered", 0.0)
               for b in bets if b["status"] in ("open", "resting"))
    gap = led["cash"] + tied - (START_BANKROLL + realized)
    if abs(gap) > 1e-6:
        problems.append(f"{name}: cash does not reconcile (off by {gap:+.6f})")
    return rows, problems, gap


def main(paths):
    ledgers = {p: json.load(open(p)) for p in paths}
    assets = {b["asset"] for led in ledgers.values() for b in led.get("bets", [])
              if b.get("status") in SETTLED and b.get("asset")}
    payouts = official_payouts(assets)
    all_problems, counts = [], {"ok": 0, "legacy": 0, "pending": 0, "bad": 0}
    for name, led in ledgers.items():
        rows, problems, gap = check_ledger(name, led, payouts)
        all_problems += problems
        ok = sum(1 for r in rows if r[3] == "ok")
        legacy = sum(1 for r in rows if r[3] == "legacy")
        pending = sum(1 for r in rows if r[2] is None and r[3].startswith("not resolved"))
        counts["ok"] += ok
        counts["legacy"] += legacy
        counts["pending"] += pending
        counts["bad"] += len(rows) - ok - legacy - pending
        print(f"{name:28} settled checked {len(rows):4} | ok {ok:4} | legacy {legacy} | "
              f"awaiting official result {pending:3} | cash gap {gap:+.6f}")
    print(f"\nTotal: {counts['ok']} ok, {counts['legacy']} legacy (old stop records: P&L "
          f"range-checked only), {counts['bad']} mismatched, "
          f"{counts['pending']} awaiting an official result")
    for p in all_problems:
        print("PROBLEM:", p)
    return 1 if all_problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:] or sorted(glob.glob("ledger*.json"))))
