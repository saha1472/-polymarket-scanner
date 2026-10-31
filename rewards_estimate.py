"""READ-ONLY estimate: what would a minimum two-sided quote earn in
Polymarket liquidity rewards right now? It never trades and never writes.

For each chosen reward market it reads the live order book and applies
Polymarket's published scoring rule (docs.polymarket.com, "Liquidity rewards"):

  order score   S = ((v - s) / v)^2 x size     v = max spread (c), s = distance
                                               from the size-adjusted midpoint (c)
  side totals   Q1 = bids, Q2 = asks (scored on the YES book; NO orders show up
                there mirrored, so counting both books would double count)
  maker score   Q_min = max(min(Q1, Q2), max(Q1, Q2) / 3) if 0.10 <= mid <= 0.90
                        min(Q1, Q2)                          otherwise
  your share    your Q_min / (your Q_min + everyone else's)

The book shows price levels, not makers, so "everyone else's" total is
unknown. It is bounded instead: for makers holding Q1 + Q2 between them, the
sum of their Q_min is between (Q1 + Q2) / 4 and (Q1 + Q2) / 2. That gives
a low and a high estimate of your share.

Two placements are tried, each with the market's minimum qualifying size:
  join  - level with the best qualifying bid and ask (most reward, most fills)
  back  - 1c behind them (less reward, fewer fills; what the LP tool does)

What this can't see, so treat results as a ceiling for an always-on quoter:
  - one snapshot, as if the book stayed like this all day (it's sampled each minute)
  - levels, not orders: a level is counted if its total size >= min size
  - the market's reward multiplier b (assumed equal for everyone)
  - fills: being hit means holding a position. "ahead" shows the shares
    queued in front of you at or better than your price on each side.

Usage: python rewards_estimate.py [--top 20] [--max-capital 25]
"""
import argparse
import json
import math
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

CLOB = "https://clob.polymarket.com"
GAMMA_MARKETS = "https://gamma-api.polymarket.com/markets"
C_SINGLE = 3.0                 # single-sided divisor (docs: 3.0 on all markets)
SIDE_OK = (0.10, 0.90)         # midpoint range where one-sided quotes still score
MID_RANGE = (0.15, 0.85)       # markets considered (away from the edges)
BACK_C = 1.0                   # the "back" placement, in cents behind the best


# ---------- pure helpers (tested) ----------

def num(v, default=None):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return default
    return x if math.isfinite(x) else default


def levels(book, side):
    """[(price, size)] best first, positive and finite only."""
    out = []
    for lv in (book or {}).get(side, []) or []:
        p, s = num(lv.get("price")), num(lv.get("size"))
        if p is not None and s is not None and 0 < p < 1 and s > 0:
            out.append((p, s))
    return sorted(out, key=lambda x: -x[0] if side == "bids" else x[0])


def adjusted_mid(bids, asks, min_size):
    """Midpoint of the best levels holding at least min_size (falls back to the
    raw best levels when none do). None if a side is empty."""
    if not bids or not asks:
        return None
    qb = [p for p, s in bids if s >= min_size] or [bids[0][0]]
    qa = [p for p, s in asks if s >= min_size] or [asks[0][0]]
    return (qb[0] + qa[0]) / 2


def order_score(price, size, mid, v_cents):
    """((v - s) / v)^2 x size, or 0 outside the max spread."""
    if v_cents <= 0:
        return 0.0
    s = abs(price - mid) * 100
    if s > v_cents + 1e-9:
        return 0.0
    return ((v_cents - s) / v_cents) ** 2 * size


def side_total(lvls, mid, v_cents, min_size):
    return sum(order_score(p, s, mid, v_cents) for p, s in lvls if s >= min_size - 1e-9)


def q_min(q1, q2, mid):
    if SIDE_OK[0] <= mid <= SIDE_OK[1]:
        return max(min(q1, q2), max(q1, q2) / C_SINGLE)
    return min(q1, q2)


def share_range(ours, q1_others, q2_others):
    """(low, high) share of the pool for a maker scoring `ours`."""
    total = q1_others + q2_others
    if ours <= 0:
        return 0.0, 0.0
    worst, best = total / 2, total / 4
    return ours / (ours + worst), ours / (ours + best)


def placement(bids, asks, min_size, back_cents, tick):
    """Our bid and ask prices: level with the best qualifying quotes, then step
    back `back_cents`, rounded outward to the tick."""
    qb = [p for p, s in bids if s >= min_size] or [bids[0][0]]
    qa = [p for p, s in asks if s >= min_size] or [asks[0][0]]
    step = back_cents / 100
    bid = math.floor((qb[0] - step) / tick + 1e-9) * tick
    ask = math.ceil((qa[0] + step) / tick - 1e-9) * tick
    return round(bid, 6), round(ask, 6)


def evaluate(book, rate_per_day, v_cents, min_size):
    """Estimate for one market's YES book. None if it can't be scored."""
    bids, asks = levels(book, "bids"), levels(book, "asks")
    mid = adjusted_mid(bids, asks, min_size)
    if mid is None:
        return None
    tick = num(book.get("tick_size"), 0.01) or 0.01
    size = max(min_size, num(book.get("min_order_size"), 0) or 0)
    q1 = side_total(bids, mid, v_cents, min_size)
    q2 = side_total(asks, mid, v_cents, min_size)
    out = {"mid": mid, "spread_c": (asks[0][0] - bids[0][0]) * 100, "size": size,
           "others_q": q1 + q2, "placements": {}}
    for name, back in (("join", 0.0), ("back", BACK_C)):
        bid, ask = placement(bids, asks, min_size, back, tick)
        mine = q_min(order_score(bid, size, mid, v_cents),
                     order_score(ask, size, mid, v_cents), mid)
        lo, hi = share_range(mine, q1, q2)
        capital = size * bid + size * (1 - ask)        # selling YES = buying NO
        out["placements"][name] = {
            "bid": bid, "ask": ask, "score": mine, "share": (lo, hi),
            "usd_day": (rate_per_day * lo, rate_per_day * hi), "capital": capital,
            "ahead_bid": sum(s for p, s in bids if p >= bid - 1e-9),
            "ahead_ask": sum(s for p, s in asks if p <= ask + 1e-9),
        }
    return out


# ---------- network ----------

def http_get(session, url, params=None):
    for attempt in range(3):
        try:
            r = session.get(url, params=params, timeout=30)
        except Exception as err:
            print(f"request failed {url}: {err}", file=sys.stderr)
            time.sleep(1 + attempt)
            continue
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(2 + 2 * attempt)
            continue
        if r.status_code != 200:
            return None
        return r.json()
    return None


def reward_markets(session, max_pages=200):
    """All markets with an active reward pool: [{condition, rate, v, min_size}]."""
    out, seen, cursor = [], set(), ""
    for _ in range(max_pages):
        page = http_get(session, f"{CLOB}/rewards/markets/current",
                        {"next_cursor": cursor} if cursor else None)
        if not isinstance(page, dict):
            break
        for r in page.get("data") or []:
            rate = num(r.get("total_daily_rate"), 0) or 0
            v, ms = num(r.get("rewards_max_spread"), 0), num(r.get("rewards_min_size"), 0)
            if rate > 0 and v and v > 0 and r.get("condition_id") \
                    and r["condition_id"] not in seen:     # the list has duplicates
                seen.add(r["condition_id"])
                out.append({"condition": r["condition_id"], "rate": rate,
                            "v": v, "min_size": ms or 0})
        cursor = page.get("next_cursor")
        if not cursor or cursor == "LTE=":
            break
    return out


def market_info(session, condition):
    rows = http_get(session, GAMMA_MARKETS, {"condition_ids": condition})
    if not rows:
        return None
    m = rows[0]
    try:
        ids = json.loads(m.get("clobTokenIds") or "[]")
    except ValueError:
        return None
    if len(ids) != 2 or m.get("closed") or not m.get("active", True):
        return None
    return {"yes": ids[0], "question": m.get("question", "?"),
            "volume_24h": num(m.get("volume24hr"), 0) or 0,
            "days_left": days_until(m.get("endDate"))}


def days_until(end, now=None):
    if not end:
        return None
    try:
        dt = datetime.fromisoformat(str(end).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt - (now or datetime.now(timezone.utc))).total_seconds() / 86400


def average(estimates):
    """Average several snapshots of one market (scores, shares, $/day). Prices,
    capital and queue sizes come from the latest snapshot."""
    good = [e for e in estimates if e]
    if not good:
        return None
    out = dict(good[-1], samples=len(good), placements={})
    for name in good[-1]["placements"]:
        ps = [e["placements"][name] for e in good]
        avg = dict(ps[-1])
        avg["score"] = sum(p["score"] for p in ps) / len(ps)
        for key in ("share", "usd_day"):
            avg[key] = tuple(sum(p[key][i] for p in ps) / len(ps) for i in (0, 1))
        avg["usd_day_range"] = (min(p["usd_day"][0] for p in ps), max(p["usd_day"][1] for p in ps))
        out["placements"][name] = avg
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--top", type=int, default=20, help="markets to evaluate")
    ap.add_argument("--max-capital", type=float, default=25.0,
                    help="skip markets needing more than this for a two-sided quote")
    ap.add_argument("--min-days", type=float, default=3.0,
                    help="skip markets ending sooner than this")
    ap.add_argument("--samples", type=int, default=3, help="book snapshots per market")
    ap.add_argument("--gap", type=float, default=60.0, help="seconds between snapshots")
    args = ap.parse_args(argv)
    import requests
    session = requests.Session()

    pools = reward_markets(session)
    if not pools:
        print("Couldn't read the reward market list. Nothing estimated.")
        return 2
    total = sum(p["rate"] for p in pools)
    rates = sorted(p["rate"] for p in pools)
    print(f"{len(pools)} markets have an active pool, ${total:,.0f}/day in all "
          f"(median ${rates[len(rates) // 2]:g}/day per market).")
    # a two-sided quote costs about min_size dollars (bid + (1 - ask) is ~$1/share)
    fits = sorted((p for p in pools if p["min_size"] <= args.max_capital),
                  key=lambda p: -p["rate"])
    print(f"{len(fits)} need <= ${args.max_capital:g} for a minimum two-sided quote. "
          f"Checking the richest ones...\n")

    with ThreadPoolExecutor(6) as pool:
        infos = list(pool.map(lambda p: market_info(session, p["condition"]),
                              fits[:args.top * 4]))
    chosen = [(p, i) for p, i in zip(fits, infos) if i and i["days_left"] is not None
              and i["days_left"] >= args.min_days][:args.top * 2]

    def snapshot():
        with ThreadPoolExecutor(6) as pool:
            books = list(pool.map(lambda pi: http_get(session, f"{CLOB}/book",
                                                      {"token_id": pi[1]["yes"]}), chosen))
        return [evaluate(b, p["rate"], p["v"], p["min_size"]) if b else None
                for (p, _), b in zip(chosen, books)]

    snaps = []
    for k in range(max(1, args.samples)):
        if k:
            print(f"  snapshot {k} of {args.samples} taken; next in {args.gap:g}s...")
            time.sleep(args.gap)
        snaps.append(snapshot())
    results = []
    for j, (p, info) in enumerate(chosen):
        est = average([s[j] for s in snaps])
        if est and MID_RANGE[0] <= est["mid"] <= MID_RANGE[1]:
            results.append((p, info, est))
    results = results[:args.top]
    if not results:
        print("No market in range could be scored.")
        return 2

    print(f"{'pool/day':>8} {'v':>4} {'min':>4} {'mid':>5} {'spr':>4} | "
          f"{'JOIN $/day':>13} {'cap':>5} {'ahead b/a':>11} | "
          f"{'BACK 1c $/day':>13} {'ahead b/a':>11} | 24h vol  market")
    sums = {"join": [0, 0, 0], "back": [0, 0, 0]}
    for p, info, e in results:
        j, b = e["placements"]["join"], e["placements"]["back"]
        for name, x in (("join", j), ("back", b)):
            sums[name][0] += x["usd_day"][0]
            sums[name][1] += x["usd_day"][1]
            sums[name][2] += x["capital"]
        print(f"{p['rate']:8.0f} {p['v']:4g} {p['min_size']:4g} {e['mid']:5.2f} "
              f"{e['spread_c']:4.1f} | {j['usd_day'][0]:5.2f}-{j['usd_day'][1]:<6.2f} "
              f"{j['capital']:5.1f} {j['ahead_bid']:5.0f}/{j['ahead_ask']:<5.0f} | "
              f"{b['usd_day'][0]:5.2f}-{b['usd_day'][1]:<6.2f} "
              f"{b['ahead_bid']:5.0f}/{b['ahead_ask']:<5.0f} | "
              f"${info['volume_24h']:>7,.0f}  {info['question'][:48]}")
    n = len(results)
    print(f"\nAcross these {n} markets, one minimum two-sided quote in each:")
    for name, label in (("join", "join the best quotes"), ("back", "sit 1c back")):
        lo, hi, cap = sums[name]
        print(f"  {label:21}: ${lo:.2f}-${hi:.2f}/day on ${cap:.0f} of capital "
              f"({lo / cap:.2%}-{hi / cap:.2%} a day), before any losses from fills")
    swing = [(info["question"][:40], e["placements"]["join"]["usd_day_range"])
             for _, info, e in results if e.get("samples", 1) > 1]
    if swing:
        big = max(swing, key=lambda x: x[1][1] - x[1][0])
        print(f"  biggest swing between snapshots: {big[0]} "
              f"(${big[1][0]:.2f}-${big[1][1]:.2f}/day)")
    print(f"\nCeiling, not a forecast: {args.samples} snapshot(s) "
          f"{args.gap:g}s apart stand in for a whole day, quotes are never pulled, no fills.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
