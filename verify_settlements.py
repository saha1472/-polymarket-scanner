"""Independent, READ-ONLY check of the paper ledgers.

Validation runs in a fixed order, and arithmetic only ever runs on data
that passed the earlier steps:

  1. ledger    - readable JSON object with finite `cash` and a `bets` list
  2. bets      - each bet is an object with a known status, a non-empty asset id,
                 and every field its status requires as a finite, non-boolean
                 number (no zero defaults; `recovered` is the only optional field)
  3. stops     - stop-loss sale P&L checked on its own, resolved or not:
                 modern stops   pnl == recovered - stake - fee
                 legacy stops   (no sale record) pnl within its possible range
  4. payouts   - Polymarket's official settled payout (Data API GET /v2/tokens)
                 must be exactly 0, 0.5 or 1; anything else is reported
  5. results   - outcome, P&L and the "if held" baseline vs the official payout,
                 then each ledger's cash reconciliation

It never writes anything. Exit codes: 0 all good, 1 problems found,
2 nothing to check (no ledger files).

Usage:  python verify_settlements.py              (all ledger*.json)
        python verify_settlements.py ledger.json  (specific files)
"""
import glob
import json
import math
import sys

V2_TOKENS = "https://data-api.polymarket.com/v2/tokens"
START_BANKROLL = 50.0
TOL = 1e-6
SETTLED = ("won", "lost", "split", "stopped")
VALID_PAYOUTS = (0.0, 0.5, 1.0)

# Fields each status MUST have as finite numbers. Nothing here is defaulted.
REQUIRED = {
    "open":      ("stake", "fee", "shares", "price"),
    "resting":   ("stake", "fee", "shares", "price"),
    "won":       ("stake", "fee", "shares", "price", "pnl"),
    "lost":      ("stake", "fee", "shares", "price", "pnl"),
    "split":     ("stake", "fee", "shares", "price", "pnl"),
    "stopped":   ("stake", "fee", "shares", "price", "pnl"),
    "cancelled": ("stake",),
}
OPTIONAL = ("recovered", "orig_shares", "hold_pnl")   # finite when present
NON_NEGATIVE = ("stake", "fee", "shares", "price", "recovered", "orig_shares")


def finite(x):
    """A real, finite number. Booleans are rejected (True is an int in Python)."""
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def label(payout):
    return {1.0: "won", 0.0: "lost", 0.5: "split"}[payout]


# ---------- step 4 helper: official payouts ----------

def parse_payout_records(records):
    """Split /v2/tokens records into ({token: payout}, {token: reason}).
    Unresolved tokens appear in neither; invalid payouts go in the second."""
    good, invalid = {}, {}
    for rec in records or []:
        if not isinstance(rec, dict) or not isinstance(rec.get("token_id"), str):
            continue
        tok = rec["token_id"]
        if rec.get("resolved") is not True:
            continue
        p = rec.get("final_price")
        if not finite(p):
            invalid[tok] = f"non-numeric or non-finite final_price {p!r}"
            continue
        match = [v for v in VALID_PAYOUTS if abs(p - v) < 1e-9]
        if not match:
            invalid[tok] = f"unsupported final_price {p!r} (expected 0, 0.5 or 1)"
            continue
        good[tok] = match[0]
    return good, invalid


def official_payouts(assets, http=None):
    if http is None:
        import requests as http
    good, invalid = {}, {}
    assets = sorted(assets)
    for i in range(0, len(assets), 40):
        chunk = assets[i:i + 40]
        r = http.get(V2_TOKENS, params={"token_id": ",".join(chunk)}, timeout=30)
        r.raise_for_status()
        body = r.json()
        g, bad = parse_payout_records(body.get("data") if isinstance(body, dict) else None)
        good.update(g)
        invalid.update(bad)
    return good, invalid


# ---------- steps 1-2: structure ----------

def ledger_problems(led):
    if not isinstance(led, dict):
        return ["ledger is not a JSON object"]
    out = []
    if not finite(led.get("cash")):
        out.append(f"cash is missing or not a finite number ({led.get('cash')!r})")
    if not isinstance(led.get("bets"), list):
        out.append("`bets` is missing or not a list")
    return out


def bet_problems(b):
    if not isinstance(b, dict):
        return ["bet is not a JSON object"]
    status = b.get("status")
    if status not in REQUIRED:
        return [f"unknown status {status!r}"]
    out = []
    if not isinstance(b.get("asset"), str) or not b["asset"]:
        out.append("missing asset id")
    bad = [k for k in REQUIRED[status] if not finite(b.get(k))]
    bad += [k for k in OPTIONAL if k in b and not finite(b[k])]
    if bad:
        out.append(f"missing or non-finite {', '.join(bad)}")
    neg = [k for k in NON_NEGATIVE if finite(b.get(k)) and b[k] < 0]
    if neg:
        out.append(f"negative {', '.join(neg)}")
    return out


# ---------- the check ----------

def check_ledger(name, led, payouts, invalid_payouts=None):
    """Returns (rows, problems, cash gap). rows = (where, status, payout, note).
    note is "ok", "legacy" (old stop: P&L range-checked only), "pending"
    (awaiting an official result) or a description of what's wrong."""
    invalid_payouts = invalid_payouts or {}
    rows, problems = [], []
    structure = ledger_problems(led)
    if structure:
        return rows, [f"{name}: {p}" for p in structure], float("nan")

    bets = led["bets"]
    valid = []
    for i, b in enumerate(bets):
        where = f"{name} #{i} {str(b.get('title', ''))[:60] if isinstance(b, dict) else ''}"
        issues = bet_problems(b)
        if issues:
            problems += [f"{where}: {p}" for p in issues]
            rows.append((where, b.get("status") if isinstance(b, dict) else None, None,
                         "INVALID FIELDS"))
            continue
        valid.append((where, b))

    for where, b in valid:
        if b["status"] not in SETTLED:
            continue
        notes = []
        # step 3: stop-loss sale proceeds, independent of any market result
        if b["status"] == "stopped":
            if "recovered" in b:
                want = b["recovered"] - b["stake"] - b["fee"]
                if abs(want - b["pnl"]) > TOL:
                    problems.append(f"{where}: stopped pnl {b['pnl']} != recovered - stake - "
                                    f"fee = {want:.6f}")
                    notes.append("STOP PNL MISMATCH")
            else:
                lo, hi = -b["stake"] - b["fee"], b["shares"] - b["stake"] - b["fee"]
                if not lo - TOL <= b["pnl"] <= hi + TOL:
                    problems.append(f"{where}: legacy stopped pnl {b['pnl']} outside the "
                                    f"possible range [{lo:.4f}, {hi:.4f}]")
                    notes.append("STOP PNL OUT OF RANGE")
                else:
                    notes.append("legacy")

        # step 4: the official payout itself must be valid
        if b["asset"] in invalid_payouts:
            problems.append(f"{where}: Polymarket returned an invalid payout: "
                            f"{invalid_payouts[b['asset']]}")
            rows.append((where, b["status"], None, "INVALID OFFICIAL PAYOUT"))
            continue
        pay = payouts.get(b["asset"])
        if pay is None:
            if b["status"] != "stopped":
                problems.append(f"{where}: settled as {b['status']} but Polymarket has no "
                                f"resolved payout for this token")
                notes.append("NO OFFICIAL RESULT")
            bad = [n for n in notes if n not in ("legacy",)]
            rows.append((where, b["status"], None, ", ".join(bad) or "pending"))
            continue

        # step 5: results against the official payout
        if b["status"] != "stopped":
            if label(pay) != b["status"]:
                problems.append(f"{where}: ledger says {b['status']}, Polymarket payout {pay}")
                notes.append("OUTCOME MISMATCH")
            want = b["shares"] * pay + b.get("recovered", 0.0) - b["stake"] - b["fee"]
            if abs(want - b["pnl"]) > TOL:
                problems.append(f"{where}: pnl {b['pnl']} != expected {want:.6f}")
                notes.append("PNL MISMATCH")
        if ("orig_shares" in b or b["status"] == "stopped") and "hold_pnl" not in b:
            problems.append(f"{where}: resolved bet touched by a stop-loss has no hold_pnl "
                            f"(if it resolved after the bot's last run, re-check after the next run)")
            notes.append("HOLD MISSING")
        if "hold_pnl" in b:
            want = b.get("orig_shares", b["shares"]) * pay - b["stake"] - b["fee"]
            if abs(want - b["hold_pnl"]) > TOL:
                problems.append(f"{where}: hold_pnl {b['hold_pnl']} != expected {want:.6f}")
                notes.append("HOLD MISMATCH")
        bad = [n for n in notes if n != "legacy"]
        rows.append((where, b["status"], pay,
                     ", ".join(bad) if bad else ("legacy" if notes else "ok")))

    # cash reconciliation only on fully valid ledgers: no defaults, no guesses
    if len(valid) != len(bets):
        problems.append(f"{name}: cash not reconciled because some bets have invalid fields")
        return rows, problems, float("nan")
    realized = sum(b["pnl"] for _, b in valid if b["status"] in SETTLED)
    tied = sum(b["stake"] + b["fee"] - b.get("recovered", 0.0)
               for _, b in valid if b["status"] in ("open", "resting"))
    gap = led["cash"] + tied - (START_BANKROLL + realized)
    if abs(gap) > 1e-6:
        problems.append(f"{name}: cash does not reconcile (off by {gap:+.6f})")
    return rows, problems, gap


def load(path):
    try:
        with open(path) as fh:
            return json.load(fh), None
    except (OSError, ValueError) as err:
        return None, f"{path}: can't be read as JSON ({err})"


def main(paths, http=None):
    if not paths:
        print("No ledger files found (expected ledger*.json). Nothing was checked.")
        return 2
    ledgers, problems = {}, []
    for p in paths:
        led, err = load(p)
        if err:
            problems.append(err)
        else:
            ledgers[p] = led
    assets = {b["asset"] for led in ledgers.values() if isinstance(led, dict)
              and isinstance(led.get("bets"), list) for b in led["bets"]
              if isinstance(b, dict) and b.get("status") in SETTLED
              and isinstance(b.get("asset"), str) and b["asset"]}
    payouts, invalid = official_payouts(assets, http) if assets else ({}, {})
    counts = {"ok": 0, "legacy": 0, "pending": 0, "bad": 0}
    for name, led in ledgers.items():
        rows, probs, gap = check_ledger(name, led, payouts, invalid)
        problems += probs
        c = {k: sum(1 for r in rows if r[3] == k) for k in ("ok", "legacy", "pending")}
        c["bad"] = len(rows) - sum(c.values())
        for k in counts:
            counts[k] += c[k]
        print(f"{name:28} settled checked {len(rows):4} | ok {c['ok']:4} | legacy {c['legacy']} | "
              f"pending {c['pending']:3} | problems {c['bad']:3} | cash gap {gap:+.6f}")
    print(f"\nTotal: {counts['ok']} ok, {counts['legacy']} legacy (old stop records: P&L "
          f"range-checked only), {counts['pending']} awaiting an official result, "
          f"{counts['bad']} with problems")
    for p in problems:
        print("PROBLEM:", p)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:] or sorted(glob.glob("ledger*.json"))))
