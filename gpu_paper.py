"""GPU rental-price markets: a separate PAPER test. READ-ONLY on Polymarket (it places no order and
needs no key); it writes only its own two files. The rules and the pass mark are in GPU_PAPER.md,
written before the first record.

Each run (about every 15 minutes, a step in scan.yml):
  1. score the quote placed by the last run: estimated reward until the first fill, and fills taken
     from the public trade feed, counted two ways ("sure" and "possible"), closed at today's book;
  2. place the next quote.

Report: python gpu_paper.py --report
"""
import argparse
import os
import sys
import time
from collections import Counter
from datetime import datetime, timezone

import fees
import rewards_estimate as rx
import simcore

STATE_FILE = "gpu_paper_state.json"
LOG_FILE = "gpu_paper_log.jsonl"
REWARDS = rx.CLOB + "/rewards/markets/multi"
DATA = "https://data-api.polymarket.com/v2"
SLUG = "-average-rental-price-in-"
SIZE = 50.0                   # shares per side
BUSY_RATE = 100.0             # pool $/day that puts a market in the "busy" group
PER_GROUP = 5
MIN_DAYS = 30.0
MAX_AGE_S = 30 * 60           # a quote counts as resting for this long at most
GIVE_UP_S = 24 * 3600         # a quote that cannot be scored for this long is logged as unscored
MAX_LAG_S = 25                # trade feed further behind than this: change nothing this run
SETTLE_S = 30                 # wait after reading the books, so the feed has every trade up to then
RULES = (("sure", True), ("possible", False))
MIN_COVERAGE, MIN_FILL_WINDOWS = 0.9, 30
EPS = 1e-9


# ---------- pure helpers (tested) ----------

def maker_fills(every, takers):
    """[(timestamp, "bid"|"ask", YES price, shares)], one per RESTING order that traded.

    A taker row carries only the average price of its sweep; the full list (taker_only=false) also
    has each resting order at its own price. ValueError if the two lists disagree, so a half-read
    feed is never mistaken for a quiet market."""
    def key(r):
        return (r["transaction_hash"], r["proxy_wallet"], r["side"], r["token_id"],
                round(r["price"], 6), round(r["size"], 6))
    left = Counter(key(r) for r in takers)
    matched, resting = set(), []
    for r in every:
        if left[key(r)] > 0:
            left[key(r)] -= 1
            matched.add(r["transaction_hash"])
            continue
        yes = r["price"] if r["outcome_index"] == 0 else 1 - r["price"]
        side = "bid" if (r["side"] == "BUY") == (r["outcome_index"] == 0) else "ask"
        resting.append((r["transaction_hash"], (r["timestamp"], side, yes, r["size"])))
    if sum(left.values()) or any(tx not in matched for tx, _ in resting):
        raise ValueError("taker rows and resting-order rows disagree")
    return [fill for _, fill in resting]


def fills_against(quote, fills, sure):
    """(shares bought, shares sold, time of the first fill) for our quote. `sure` counts only trades
    at a price worse than ours; otherwise trades at our price count too. Each side fills up to the
    quote's size."""
    got = {"bid": 0.0, "ask": 0.0}
    first = None
    for ts, side, price, size in sorted(fills):
        through = quote["bid"] - price if side == "bid" else price - quote["ask"]
        if through > EPS or (not sure and through >= -EPS):
            take = min(size, quote["size"] - got[side])
            if take > EPS:
                got[side] += take
                first = ts if first is None else first
    return got["bid"], got["ask"], first


def close_out(book, shares, fee_rate):
    """Cash from closing a position of `shares` YES against `book`, walking the depth and paying the
    taker fee. Long shares are sold into the bids (what finds no buyer is worth 0); short shares are
    bought back from the asks (what finds no seller costs $1 a share)."""
    left, cash = abs(shares), 0.0
    if left <= EPS:
        return 0.0
    for price, size in rx.levels(book, "bids" if shares > 0 else "asks"):
        take = min(size, left)
        cash += (take * price if shares > 0 else -take * price) - fees.fee_amount(take, price, fee_rate)
        left -= take
        if left <= EPS:
            break
    return cash - (max(left, 0.0) if shares < 0 else 0.0)


def reward_rate(book, quote, pool):
    """Low estimate, $/day, of what `quote` earns against this book and pool (0 if it cannot score)."""
    if not pool or pool["rate"] <= 0 or pool["min_size"] > quote["size"]:
        return 0.0
    bids, asks = rx.levels(book, "bids"), rx.levels(book, "asks")
    mid = rx.adjusted_mid(bids, asks, pool["min_size"])
    if mid is None or not rx.MID_RANGE[0] <= mid <= rx.MID_RANGE[1]:
        return 0.0
    ours = rx.q_min(rx.order_score(quote["bid"], quote["size"], mid, pool["v"]),
                    rx.order_score(quote["ask"], quote["size"], mid, pool["v"]), mid)
    low = rx.share_range(ours, rx.side_total(bids, mid, pool["v"], pool["min_size"]),
                         rx.side_total(asks, mid, pool["v"], pool["min_size"]))[0]
    return pool["rate"] * low


def quote_for(book, pool):
    """Our bid and ask level with the best qualifying prices, or None if we should not quote."""
    if not book or not pool or pool["min_size"] > SIZE:
        return None
    bids, asks = rx.levels(book, "bids"), rx.levels(book, "asks")
    mid = rx.adjusted_mid(bids, asks, pool["min_size"])
    if mid is None:
        return None
    tick = rx.num(book.get("tick_size"), 0.01) or 0.01
    bid, ask = rx.placement(bids, asks, pool["min_size"], 0.0, tick)
    if not 0 < bid < ask < 1 or any(rx.order_score(p, SIZE, mid, pool["v"]) <= 0 for p in (bid, ask)):
        return None
    quote = {"bid": bid, "ask": ask, "size": SIZE}
    quote["usd_day"] = reward_rate(book, quote, pool)
    return quote if quote["usd_day"] > 0 else None


def record(market, now, quote, covered_s):
    """An empty log line for one market and run."""
    nothing = {"reward": 0.0, "bought": 0.0, "sold": 0.0, "pnl": 0.0}
    return {"time": datetime.fromtimestamp(now, timezone.utc).isoformat(), "t": now,
            "condition": market["condition"], "group": market["group"], "question": market["question"],
            "covered_s": covered_s, "quote": quote, "sure": dict(nothing), "possible": dict(nothing)}


def score(open_, now, book, pool, market, fills):
    """The log line for one market's window: from when `open_` was placed to this run."""
    placed, quote = open_["t"], open_["quote"]
    fresh = now - placed <= MAX_AGE_S
    inside = [f for f in fills if placed < f[0] <= min(now, placed + MAX_AGE_S)]
    rate = min(quote["usd_day"], reward_rate(book, quote, pool)) if quote and fresh else 0.0
    line = record(market, now, quote, now - placed if fresh else 0.0)
    for name, sure in RULES:
        bought, sold, first = fills_against(quote, inside, sure) if quote else (0.0, 0.0, None)
        pnl = 0.0
        if bought or sold:
            pnl = sold * quote["ask"] - bought * quote["bid"] + close_out(book, bought - sold, market["fee_rate"])
        reward = rate * ((now if first is None else first) - placed) / 86400
        line[name] = {"reward": round(reward, 9), "bought": bought, "sold": sold, "pnl": round(pnl, 9)}
    return line


def choose(candidates, fee_of, now=None):
    """The fixed market list (GPU_PAPER.md): per group, one per event, highest pool, then nearest end
    date, then midpoint closest to 0.50. fee_of(condition) -> taker fee rate, or None if unknown."""
    fit = []
    for c in candidates:
        left = rx.days_until(c["end"], now)
        if left is None or left < MIN_DAYS or not quote_for(c["book"], c):
            continue
        look = rx.evaluate(c["book"], c["rate"], c["v"], c["min_size"])
        if look["others_q"] > 0 and look["spread_c"] <= c["v"] + EPS:
            fit.append(((-c["rate"], c["end"], abs(look["mid"] - 0.5), c["condition"]), c))
    chosen, events, count = [], set(), Counter()
    for _, c in sorted(fit, key=lambda f: f[0]):
        group = "busy" if c["rate"] >= BUSY_RATE else "quiet"
        if count[group] >= PER_GROUP or (group, c["event"]) in events:
            continue
        fee = fee_of(c["condition"])
        if fee is None:
            continue
        events.add((group, c["event"]))
        count[group] += 1
        chosen.append({"condition": c["condition"], "group": group, "token": c["token"],
                       "question": c["question"], "event": c["event"], "fee_rate": fee})
    return chosen


# ---------- the pass mark ----------

def judge(records, group, rule, start, days):
    """The pass-mark numbers for one group under one fill count, over `days` UTC days from `start`."""
    rows = [r for r in records if r["group"] == group and start <= r["t"] < start + days * 86400]
    reward, pnl = Counter(), Counter()                    # by (market, day)
    for r in rows:
        key = (r["condition"], int((r["t"] - start) // 86400))
        reward[key] += r[rule]["reward"]
        pnl[key] += r[rule]["pnl"]
    keys = set(reward) | set(pnl)

    def total(part, factor=1.0):                          # a market-day under $1 is not paid
        return sum((reward[k] * factor if reward[k] * factor >= 1 else 0.0) + pnl[k] for k in part)

    net = total(keys)
    by_market = [total([k for k in keys if k[0] == c]) for c in {k[0] for k in keys}]
    by_day = [total([k for k in keys if k[1] == d]) for d in range(days)]
    markets = len({r["condition"] for r in records if r["group"] == group})
    fill_pnl = sum(pnl.values())
    out = {"net": net, "reward": net - fill_pnl, "pnl": fill_pnl,
           "coverage": min(1.0, sum(r["covered_s"] for r in rows) / (markets * days * 86400)) if markets else 0.0,
           "fill_windows": sum(1 for r in rows if r["sure"]["bought"] or r["sure"]["sold"]),
           "halves": (sum(by_day[:days // 2]), sum(by_day[days // 2:])),
           "net_without_best_market": net - max(by_market, default=0.0),
           "net_without_best_day": net - max(by_day),
           "net_half_reward": total(keys, 0.5)}
    out["passes"] = all(x > 0 for x in (net, *out["halves"], out["net_without_best_market"],
                                        out["net_without_best_day"], out["net_half_reward"]))
    return out


def verdict(records, group, start, days):
    both = [judge(records, group, rule, start, days) for rule, _ in RULES]
    if both[0]["coverage"] < MIN_COVERAGE - EPS or both[0]["fill_windows"] < MIN_FILL_WINDOWS:
        return "NOT ENOUGH DATA"
    return "PASS" if all(j["passes"] for j in both) else "FAIL"


def report(records):
    if not records:
        return "No records yet."
    first, latest = min(r["t"] for r in records), max(r["t"] for r in records)
    start = (first // 86400 + 1) * 86400                  # the first UTC midnight after the first record
    elapsed = (latest - start) / 86400

    def when(t):
        return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d %H:%M")

    out = [f"GPU paper test. Period starts {when(start)} UTC; latest record {when(latest)} UTC."]
    for group in ("busy", "quiet"):
        if not any(r["group"] == group for r in records):
            out.append(f"\n{group}: no markets")
            continue
        days, final = 14, elapsed >= 14
        if not final:
            days = max(1, int(elapsed) + 1) if elapsed >= 0 else 1
            head = f"INTERIM, day {days if elapsed >= 0 else 0} of 14 (decides nothing)"
        else:
            result = verdict(records, group, start, 14)
            head = f"VERDICT (days 1-14): {result}"
            if result == "NOT ENOUGH DATA" and elapsed < 28:
                head += "; read again at day 28"
            elif result == "NOT ENOUGH DATA":
                days = 28
                result = verdict(records, group, start, 28)
                head = (f"VERDICT (days 1-28): {result}" if result != "NOT ENOUGH DATA"
                        else "VERDICT: not proven (too little data after 28 days); the test ends")
        out.append(f"\n{group}: {head}")
        for rule, _ in RULES:
            j = judge(records, group, rule, start, days)
            out.append(f"  {rule:8} fills: reward counted ${j['reward']:+.2f}, fill result ${j['pnl']:+.2f}, "
                       f"net ${j['net']:+.2f}")
            if final:
                out.append(f"           halves ${j['halves'][0]:+.2f} / ${j['halves'][1]:+.2f}; without best market "
                           f"${j['net_without_best_market']:+.2f}; without best day ${j['net_without_best_day']:+.2f}; "
                           f"with half the reward ${j['net_half_reward']:+.2f}")
        out.append(f"  windows with a sure fill: {j['fill_windows']} (need {MIN_FILL_WINDOWS})"
                   + (f"; runs cover {j['coverage']:.0%} of the period (need {MIN_COVERAGE:.0%})" if final else ""))
    unscored = sum(1 for r in records if r.get("unscored"))
    if unscored:
        out.append(f"\n{unscored} quote(s) could not be scored; any fills on them are not in these numbers.")
    return "\n".join(out)


# ---------- network ----------

def reward_pools(session):
    """{condition: market with its pool} for the monthly GPU price markets, or None if the list could
    not be read in full."""
    out, cursor = {}, ""
    for _ in range(50):
        params = {"tag_slug": "compute", "page_size": 500}
        if cursor:
            params["next_cursor"] = cursor
        page = rx.http_get(session, REWARDS, params)
        if not isinstance(page, dict) or not isinstance(page.get("data"), list):
            return None
        for r in page["data"]:
            if SLUG not in (r.get("event_slug") or "") or not r.get("condition_id"):
                continue
            yes = next((t.get("token_id") for t in r.get("tokens") or []
                        if str(t.get("outcome")).lower() == "yes"), None)
            if yes:
                out[r["condition_id"]] = {
                    "condition": r["condition_id"], "event": r["event_slug"], "token": yes,
                    "question": r.get("question", "?"), "end": r.get("end_date"),
                    "rate": sum(rx.num(g.get("rate_per_day"), 0) for g in r.get("rewards_config") or []),
                    "v": rx.num(r.get("rewards_max_spread"), 0), "min_size": rx.num(r.get("rewards_min_size"), 0)}
        cursor = page.get("next_cursor")
        if not cursor or cursor == "LTE=" or not page["data"]:
            return out
    return None


def fee_rate(session, condition):
    rows = rx.http_get(session, rx.GAMMA_MARKETS, {"condition_ids": condition})
    fee = fees.market_fee(rows[0]) if rows else None
    return None if fee is None else fee["rate"]


def feed_is_current(session):
    status = rx.http_get(session, DATA + "/status")
    lag = rx.num((((status or {}).get("data") or {}).get("serving") or {}).get("lag_seconds"))
    return lag is not None and lag <= MAX_LAG_S


def fetch_trades(session, condition, since, until, taker_only):
    """Trade rows with since < timestamp <= until (the feed is newest first), or None if not all read."""
    out, cursor = [], None
    for _ in range(50):
        params = {"condition": condition, "limit": 1000, "taker_only": "true" if taker_only else "false"}
        if cursor:
            params["cursor"] = cursor
        page = rx.http_get(session, DATA + "/trades", params)
        if not isinstance(page, dict) or not isinstance(page.get("data"), list):
            return None
        out += [r for r in page["data"] if since < r["timestamp"] <= until]
        cursor = (page.get("pagination") or {}).get("next_cursor")
        if not cursor or not page["data"] or page["data"][-1]["timestamp"] <= since:
            return out
    return None


def read_fills(session, condition, since, until):
    every = fetch_trades(session, condition, since, until, False)
    takers = None if every is None else fetch_trades(session, condition, since, until, True)
    if takers is None:
        return None
    try:
        return maker_fills(every, takers)
    except (ValueError, KeyError, TypeError) as err:
        print(f"GPU paper test: trade rows for {condition[:12]} not usable: {err}")
        return None


def main(session=None, fetch=simcore.fetch_books, sleep=time.sleep, clock=time.time):
    state = simcore.read_json(STATE_FILE, None) if os.path.exists(STATE_FILE) else {}
    if state is None:                          # never pick a new market list over a damaged one
        print(f"GPU paper test: {STATE_FILE} is unreadable; nothing changed.")
        return 1
    if session is None:
        import requests
        session = requests.Session()
    pools = reward_pools(session)
    if pools is None or not feed_is_current(session):
        print("GPU paper test: reward list unreadable or trade feed behind; nothing changed.")
        return 0
    markets = state.get("markets")
    if not markets:
        books = fetch(p["token"] for p in pools.values())
        markets = choose([dict(p, book=books.get(p["token"])) for p in pools.values()],
                         lambda condition: fee_rate(session, condition),
                         now=datetime.fromtimestamp(clock(), timezone.utc))
        if not markets:
            print("GPU paper test: no market qualifies yet.")
            return 0
        state = {"markets": markets, "open": {}}
    books = fetch(m["token"] for m in markets)
    now = clock()
    sleep(SETTLE_S)                            # let the trade feed catch up with the moment of the books
    quotes, lines = state.setdefault("open", {}), []
    for m in markets:
        condition = m["condition"]
        book, pool, prev = books.get(m["token"]), pools.get(condition), quotes.get(condition)
        if prev and prev["quote"]:
            fills = None
            if book is not None:
                fills = read_fills(session, condition, prev["t"], min(now, prev["t"] + MAX_AGE_S))
            if fills is not None:
                lines.append(score(prev, now, book, pool, m, fills))
            elif now - prev["t"] < GIVE_UP_S:
                continue                       # keep the quote and score it on a later run
            else:
                lines.append(dict(record(m, now, prev["quote"], 0.0), unscored=True))
        elif prev:
            lines.append(score(prev, now, book, pool, m, []))
        quotes[condition] = {"t": now, "quote": quote_for(book, pool)}
    simcore.write_json_atomic(STATE_FILE, state, indent=1)
    if lines:
        simcore.append_log(LOG_FILE, lines, now=datetime.fromtimestamp(now, timezone.utc))
    print(f"GPU paper test: {len(lines)} window(s) logged, "
          f"{sum(1 for q in quotes.values() if q['quote'])} quote(s) now open on {len(markets)} market(s).")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Separate paper test on the GPU price markets (GPU_PAPER.md).")
    ap.add_argument("--report", action="store_true")
    if ap.parse_args().report:
        print(report(simcore.read_log(LOG_FILE)))
        sys.exit(0)
    sys.exit(main())
