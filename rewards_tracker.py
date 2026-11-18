"""Rewards tracker: a running, hypothetical market-making record. READ-ONLY
on Polymarket (it never places an order); it only writes its own files.

rewards_estimate.py gives a one-off snapshot and says itself it's a ceiling:
it ignores what happens when your quotes get hit. This keeps a continuous
record instead, every run (~15 min), for a fixed set of reward markets:

  1. once a day, pick the richest reward pools we could afford
     (minimum two-sided quote <= MAX_CAPITAL, at least MIN_DAYS left)
  2. each run, place a hypothetical minimum quote level with the best
     qualifying bid and ask ("join"), and credit the reward share it would
     earn until the next run (low and high estimate, as in rewards_estimate)
  3. next run, check whether that quote would have been hit: a seller at or
     below our bid, or a buyer at or above our ask, seen by the book watcher
     (~every 30s) or in the new book. A hit is charged at the new midpoint:
     bought at our bid -> size x (mid - bid); sold at our ask -> size x (ask - mid)

Files: rewards_state.json (markets, open quotes, watcher extremes),
rewards_log.jsonl (one line per market per run).
Report: python rewards_tracker.py --report

Still optimistic in places: queue position is ignored for rewards (we assume
we're always in the book), and a hit is only marked to market over one run,
not held to resolution. Pessimistic in one: any touch of our price counts as
a fill even if others were ahead of us in the queue.

A run in which a quote was hit earns less reward: we were probably out of the
book for part of it, so the low estimate is 0 and the high estimate is halved.
A quote whose market has no usable book is kept for up to REFRESH_HOURS and
scored when the book returns; one that never returns is logged as "unmarked"
so it is visible in the report instead of vanishing. A market that has just
left the list is scored one last time; if its book is missing then, its quote
is logged as unmarked at once (it cannot be kept: the market is no longer listed).
"""
import argparse
import json
import sys
from datetime import datetime, timedelta, timezone

import rewards_estimate as rx
import simcore

STATE_FILE = "rewards_state.json"
LOG_FILE = "rewards_log.jsonl"
N_MARKETS = 10
MAX_CAPITAL = 25.0
MIN_DAYS = 3.0
REFRESH_HOURS = 24
MAX_GAP_HOURS = 1.0     # a quote older than this (missed runs) earns nothing


def now_utc():
    return datetime.now(timezone.utc)


def read_json(path, default):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


# ---------- pure helpers (tested) ----------

def fills(quote, min_ask, max_bid, mid_now):
    """(bid_hit, ask_hit, P&L) for one quote, given the lowest ask and highest
    bid seen since it was placed, marked at the current midpoint."""
    bid_hit = min_ask is not None and min_ask <= quote["bid"] + 1e-9
    ask_hit = max_bid is not None and max_bid >= quote["ask"] - 1e-9
    pnl = 0.0
    if bid_hit:
        pnl += quote["size"] * (mid_now - quote["bid"])
    if ask_hit:
        pnl += quote["size"] * (quote["ask"] - mid_now)
    return bid_hit, ask_hit, pnl


HIT_REWARD_FACTOR = (0.0, 0.5)     # (low, high) share of the reward kept on a run where the quote was hit


def accrual(quote, hours, hit=False):
    """Reward credited for holding `quote` for `hours` (low, high). A quote that was filled
    during the run was not resting in the book the whole time, so it earns less."""
    if hours <= 0 or hours > MAX_GAP_HOURS:
        return 0.0, 0.0
    lo_f, hi_f = HIT_REWARD_FACTOR if hit else (1.0, 1.0)
    return quote["usd_day"][0] * hours / 24 * lo_f, quote["usd_day"][1] * hours / 24 * hi_f


def elapsed_days(lines):
    """Calendar days the log spans (first quote placed to last scoring), or None when the lines
    carry no usable times."""
    starts, ends = [], []
    for r in lines:
        try:
            end = datetime.fromisoformat(str(r["time"]))
            start = end - timedelta(hours=float(r.get("hours") or 0))
            if r.get("placed"):                       # an unmarked line remembers when its quote was placed
                placed = datetime.fromisoformat(str(r["placed"]))
                placed = placed if placed.tzinfo else placed.replace(tzinfo=timezone.utc)
        except (KeyError, ValueError, TypeError):
            return None                               # one line without a usable time: the span is unknown
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        ends.append(end)
        starts.append(min(start, placed) if r.get("placed") else start)
    if not ends:
        return None
    return max((max(ends) - min(starts)).total_seconds() / 86400, 0.0)


def summarize(lines):
    per = {}
    for r in lines:
        m = per.setdefault(r["condition"], {"question": r.get("question", "?"), "hours": 0.0,
                                            "lo": 0.0, "hi": 0.0, "hits": 0, "fill_pnl": 0.0,
                                            "capital": 0.0, "runs": 0, "unmarked": 0})
        m["hours"] += r.get("hours", 0.0)
        m["lo"] += r.get("accrued", [0, 0])[0]
        m["hi"] += r.get("accrued", [0, 0])[1]
        m["hits"] += int(bool(r.get("bid_hit"))) + int(bool(r.get("ask_hit")))
        m["fill_pnl"] += r.get("fill_pnl", 0.0)
        m["capital"] = max(m["capital"], r.get("capital", 0.0))
        m["runs"] += 1
        m["unmarked"] += int(bool(r.get("unmarked")))
    return per


def report(lines):
    per = summarize(lines)
    if not per:
        return "No tracked quotes yet."
    out = [f"{'hours':>6} {'reward low-high':>17} {'hits':>5} {'fill P&L':>9} "
           f"{'net low-high':>17} {'cap':>5}  market"]
    t = {"hours": 0, "lo": 0, "hi": 0, "hits": 0, "fill_pnl": 0, "capital": 0, "unmarked": 0}
    for m in sorted(per.values(), key=lambda m: -(m["hi"] + m["fill_pnl"])):
        out.append(f"{m['hours']:6.1f} {m['lo']:7.2f}-{m['hi']:<8.2f} {m['hits']:5} "
                   f"{m['fill_pnl']:+9.2f} {m['lo'] + m['fill_pnl']:+7.2f}-"
                   f"{m['hi'] + m['fill_pnl']:<+8.2f} {m['capital']:5.1f}  {m['question'][:50]}")
        for k in t:
            t[k] += m[k]
    per_market_days = t["hours"] / 24 / max(len(per), 1)
    out.append(f"\nTotal over ~{per_market_days:.1f} day(s) per market (average time each market was tracked): "
               f"reward ${t['lo']:.2f}-${t['hi']:.2f}, "
               f"hits {t['hits']}, fill P&L ${t['fill_pnl']:+.2f}, "
               f"net ${t['lo'] + t['fill_pnl']:+.2f} to ${t['hi'] + t['fill_pnl']:+.2f} "
               f"on about ${t['capital']:.0f} of capital (summed over all {len(per)} market(s))")
    calendar = elapsed_days(lines)
    if calendar:
        # the markets change over time, so dividing the total by the average tracking time of ONE market
        # would overstate the daily figure by about the number of markets that rotated through
        out.append(f"Per calendar day (the log spans {calendar:.1f} day(s)): net "
                   f"${(t['lo'] + t['fill_pnl']) / calendar:+.2f} to ${(t['hi'] + t['fill_pnl']) / calendar:+.2f}")
    elif per_market_days > 0:
        out.append(f"Per market-day (these lines carry no times, so the calendar span is unknown): net "
                   f"${(t['lo'] + t['fill_pnl']) / per_market_days:+.2f} to "
                   f"${(t['hi'] + t['fill_pnl']) / per_market_days:+.2f}")
    if t["unmarked"]:
        out.append(f"{t['unmarked']} quote(s) could not be scored (their market had no usable book for too long); "
                   f"any fills on them are not in the numbers above")
    return "\n".join(out)


# ---------- network ----------

def choose_markets(session):
    pools = rx.reward_markets(session)
    fits = sorted((p for p in pools if p["min_size"] <= MAX_CAPITAL), key=lambda p: -p["rate"])
    chosen = []
    for p in fits[:N_MARKETS * 4]:
        info = rx.market_info(session, p["condition"])
        if info and info["days_left"] is not None and info["days_left"] >= MIN_DAYS:
            chosen.append(dict(p, yes=info["yes"], question=info["question"]))
        if len(chosen) >= N_MARKETS:
            break
    return chosen


def main(fetch=simcore.fetch_books, session=None):
    state = read_json(STATE_FILE, {})
    now = now_utc()
    previous = state.get("markets") or []
    chosen_at = state.get("chosen_at")
    if not state.get("markets") or not chosen_at or \
            now - datetime.fromisoformat(chosen_at) > timedelta(hours=REFRESH_HOURS):
        if session is None:
            import requests
            session = requests.Session()
        markets = choose_markets(session)
        if markets:
            state["markets"], state["chosen_at"] = markets, now.isoformat()
            print(f"Rewards tracker: picked {len(markets)} market(s) for the next day")
    markets = state.get("markets") or []
    if not markets:
        print("Rewards tracker: no markets (couldn't read the reward list).")
        return 0
    quotes, watch = state.get("quotes") or {}, state.get("watch") or {}
    current = {m["yes"] for m in markets}
    # a market that just left the list may still hold a quote: score it one last time, place no new one
    retired = [m for m in previous if m["yes"] not in current and m["yes"] in quotes]
    retired_tokens = {m["yes"] for m in retired}
    books = fetch(m["yes"] for m in markets + retired)
    lines, new_quotes, new_watch, unmarked = [], {}, {}, 0
    for m in markets + retired:
        book = books.get(m["yes"])
        est = rx.evaluate(book, m["rate"], m["v"], m["min_size"]) if book else None
        mid = est["mid"] if est else None
        q = quotes.get(m["yes"])
        if q and mid is None:
            age_h = (now - datetime.fromisoformat(q["time"])).total_seconds() / 3600
            if age_h <= REFRESH_HOURS and m["yes"] not in retired_tokens:
                new_quotes[m["yes"]] = q                      # no usable book this run: keep the quote, score it later
                new_watch[m["yes"]] = watch.get(m["yes"], {})
            else:
                unmarked += 1
                lines.append({"time": now.isoformat(), "condition": m["condition"], "question": m["question"],
                              "hours": 0.0, "accrued": [0.0, 0.0], "unmarked": True, "placed": q["time"], "bid_hit": False,
                              "ask_hit": False, "fill_pnl": 0.0, "capital": q.get("capital", 0.0)})
        if q and mid is not None:
            hours = (now - datetime.fromisoformat(q["time"])).total_seconds() / 3600
            w = watch.get(m["yes"], {})
            lows = [x for x in (w.get("min_ask"), simcore.best_ask(book)) if x is not None]
            highs = [x for x in (w.get("max_bid"), simcore.best_bid(book)) if x is not None]
            bid_hit, ask_hit, pnl = fills(q, min(lows) if lows else None,
                                          max(highs) if highs else None, mid)
            lines.append({"time": now.isoformat(), "condition": m["condition"],
                          "question": m["question"], "hours": round(hours, 4),
                          "accrued": [round(x, 6) for x in accrual(q, hours, bid_hit or ask_hit)],
                          "quote": {k: q[k] for k in ("bid", "ask", "size")},
                          "mid_then": q["mid"], "mid_now": mid, "bid_hit": bid_hit,
                          "ask_hit": ask_hit, "fill_pnl": round(pnl, 6),
                          "capital": q["capital"], "watch_looks": w.get("looks", 0)})
        if est and m["yes"] not in retired_tokens and rx.MID_RANGE[0] <= mid <= rx.MID_RANGE[1]:
            j = est["placements"]["join"]
            new_quotes[m["yes"]] = {"time": now.isoformat(), "bid": j["bid"], "ask": j["ask"],
                                    "size": j.get("size", est["size"]), "mid": mid,
                                    "usd_day": list(j["usd_day"]), "capital": j["capital"]}
            new_watch[m["yes"]] = {}                      # the watcher fills this in
    state["quotes"] = new_quotes
    state["watch"] = new_watch
    simcore.write_json_atomic(STATE_FILE, state, indent=1)
    if lines:
        simcore.append_log(LOG_FILE, lines)       # this month's file
    hits = sum(r["bid_hit"] + r["ask_hit"] for r in lines)
    print(f"Rewards tracker: {len(lines) - unmarked} quote(s) scored ({hits} hit), "
          f"{len(new_quotes)} quote(s) now open"
          + (f", {unmarked} could not be scored" if unmarked else ""))
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true")
    if ap.parse_args().report:
        print(report(simcore.read_log(LOG_FILE)))   # every monthly file
        sys.exit(0)
    sys.exit(main())
