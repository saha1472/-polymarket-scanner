"""Money-flow tag for copy signals: SHADOW ONLY, it decides nothing.

Idea (from the "Esports Fix Monitor" thread, 16 Nov 2026): in low-tier esports
a match whose recent betting is almost all on one side, or dominated by one
wallet, or whose price is moving fast, is a match where somebody may know
more than the market. The copy scan buys into exactly those matches. Before
using that as a filter it has to be shown on our own bets, so for now every
esports signal just gets a `flow` record at the moment it is found:

  usd               recent taker BUY money on both outcomes of the market
  ours_share        share of that money on the outcome we would buy
  side_share        share on whichever outcome got more (0.5 = balanced)
  top_wallet_share  the biggest single wallet's share of all that money
  price_move        change in our outcome's trade price across the window
  flags             which of the provisional alarm rules it trips

`python flow.py` then compares settled bets with and without flags. The
thresholds below are first guesses, not fitted: the raw numbers are stored so
they can be re-cut later without waiting for new data.

Source: Data API `GET /v2/trades?condition=...&side=BUY` (newest first; rows
carry proxy_wallet, token_id, size, price, timestamp in epoch seconds).
Pure functions here; the network call is handed in by signals.py.
"""
import math
import sys
import time

import shadow
import simcore

WINDOW_H = 6            # look at trades from the last this many hours
MIN_TRADE_USD = 5       # ignore dust (asked of the API, and re-checked here)
PAGE_SIZE = 1000
MAX_PAGES = 2           # busy markets: stop after this many pages ("truncated")

# Provisional alarm rules (see module note).
MIN_FLOW_USD = 1000     # below this there is too little money to read anything
ONE_SIDED = 0.90        # one outcome got this share of the recent money
WHALE = 0.40            # one wallet put in this share of the recent money
FAST_MOVE = 0.10        # our outcome's price moved this much inside the window


def trade_params(condition):
    """Query for one market's recent taker buys."""
    return {"condition": condition, "side": "BUY", "limit": PAGE_SIZE,
            "filter_type": "CASH", "filter_amount": MIN_TRADE_USD}


def older_than(now_ts, window_h=WINDOW_H):
    """A `stop(row)` test for paging: true once a row is older than the window."""
    cutoff = now_ts - window_h * 3600
    return lambda row: simcore.num(row.get("timestamp"), 0.0) < cutoff


def summarise(rows, asset, now_ts, window_h=WINDOW_H):
    """The flow record for `asset` from raw trade rows, or None if there is
    nothing usable. Rows outside the window, sells and damaged rows are ignored."""
    cutoff = now_ts - window_h * 3600
    by_token, by_wallet, ours, oldest, n = {}, {}, [], None, 0
    for r in rows or []:
        if not isinstance(r, dict) or str(r.get("side", "BUY")).upper() != "BUY":
            continue
        ts = simcore.num(r.get("timestamp"), None)
        price, size = simcore.num(r.get("price"), None), simcore.num(r.get("size"), None)
        token = r.get("token_id")
        if None in (ts, price, size) or not token or not (cutoff <= ts <= now_ts + 60):
            continue
        usd = price * size
        if not math.isfinite(usd) or usd < MIN_TRADE_USD or not 0 < price < 1:
            continue
        n += 1
        by_token[token] = by_token.get(token, 0.0) + usd
        wallet = str(r.get("proxy_wallet") or "?").lower()
        by_wallet[wallet] = by_wallet.get(wallet, 0.0) + usd
        oldest = ts if oldest is None else min(oldest, ts)
        if token == asset:
            ours.append((ts, price))
    total = sum(by_token.values())
    if n == 0 or total <= 0:
        return None
    top_token = max(by_token, key=by_token.get)
    ours.sort()
    out = {"usd": round(total, 2), "trades": n, "wallets": len(by_wallet),
           "ours_share": round(by_token.get(asset, 0.0) / total, 4),
           "side_share": round(by_token[top_token] / total, 4),
           "ours_is_bigger": top_token == asset,
           "top_wallet_share": round(max(by_wallet.values()) / total, 4),
           "top_wallet_usd": round(max(by_wallet.values()), 2),
           "price_move": round(ours[-1][1] - ours[0][1], 4) if len(ours) >= 2 else None,
           "window_h": window_h,
           "reached_back_h": round((now_ts - oldest) / 3600, 2),
           "truncated": len(rows) >= PAGE_SIZE * MAX_PAGES}
    out["flags"] = flags(out)
    return out


def flags(rec):
    """Which provisional alarm rules a flow record trips (possibly none)."""
    out = []
    if rec["usd"] >= MIN_FLOW_USD:
        if rec["side_share"] >= ONE_SIDED:
            out.append("one_sided")
        if rec["top_wallet_share"] >= WHALE:
            out.append("whale")
    if rec.get("price_move") is not None and abs(rec["price_move"]) >= FAST_MOVE:
        out.append("fast_move")
    return out


def tag(signal, fetch_rows, now_ts=None):
    """Flow record for one sized signal, or None (no condition id, API down,
    no recent trades). `fetch_rows(condition, stop)` returns raw trade rows or None."""
    cond = signal.get("condition")
    if not cond:
        return None
    now_ts = time.time() if now_ts is None else now_ts
    rows = fetch_rows(cond, older_than(now_ts))
    if rows is None:
        return None
    return summarise(rows, signal.get("asset"), now_ts)


# ---------- report ----------

def groups(bets):
    """Settled bets that carry a flow record, split every way worth looking at."""
    tagged = [b for b in bets if isinstance(b.get("flow"), dict)
              and b.get("status") in shadow.SETTLED]
    with_money = [b for b in tagged if b["flow"].get("usd", 0) >= MIN_FLOW_USD]
    return [
        ("all tagged", tagged),
        ("no flag", [b for b in tagged if not b["flow"].get("flags")]),
        ("any flag", [b for b in tagged if b["flow"].get("flags")]),
        ("one-sided, we are WITH the money",
         [b for b in tagged if "one_sided" in b["flow"].get("flags", ())
          and b["flow"].get("ours_is_bigger")]),
        ("one-sided, we are AGAINST the money",
         [b for b in tagged if "one_sided" in b["flow"].get("flags", ())
          and not b["flow"].get("ours_is_bigger")]),
        ("whale", [b for b in tagged if "whale" in b["flow"].get("flags", ())]),
        ("fast move", [b for b in tagged if "fast_move" in b["flow"].get("flags", ())]),
        (f"under ${MIN_FLOW_USD:,} of recent money",
         [b for b in tagged if b not in with_money]),
    ]


def report(bets, name):
    rows = groups(bets)
    if not rows[0][1]:
        return f"{name}: no settled bets with a flow record yet."
    lines = [f"{name}: settled bets by money flow at the moment they were found",
             f"  {'group':<38} {'bets':>5} {'per bet':>8} {'+/-':>6}"]
    for label, group in rows:
        n, mean, se = shadow.roi_stats(group)
        if n == 0:
            lines.append(f"  {label:<38} {0:>5}")
            continue
        err = f"{se:.0%}" if math.isfinite(se) else "n/a"
        lines.append(f"  {label:<38} {n:>5} {mean:>+8.0%} {err:>6}")
    lines.append("  (+/- is one standard error; under ~30 bets in a group this says nothing yet)")
    return "\n".join(lines)


def main(argv=None):
    scans = [a.upper() for a in (sys.argv[1:] if argv is None else argv)] or ["ESPORTS"]
    for scan in scans:
        low = scan.lower()
        ledger = simcore.read_json("ledger.json" if scan == "GENERAL" else f"ledger_{low}.json", {})
        book = simcore.read_json(shadow.shadow_file(scan), {})
        real = ledger.get("bets", []) if isinstance(ledger, dict) else []
        shadows = book.get("bets", []) if isinstance(book, dict) else []
        print(report(real, f"{scan} paper bets"))
        print(report(shadows, f"{scan} shadow bets"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
