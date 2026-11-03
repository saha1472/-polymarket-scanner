"""Book watcher: for the rest of each run's time slot, look at the order books
of everything waiting for a fill about every 30 seconds, instead of only once
per 15-minute run.

Watched:
  - weather resting bids (MORNING / AFTERNOON) in the real paper ledgers:
    filled when a seller at or below our price shows up, exactly the rule the
    weather desk uses, just checked far more often
  - shadow limit bids (shadow_*.json), same rule
  - the rewards tracker's hypothetical quotes (rewards_state.json): the lowest
    ask and highest bid seen are kept, so the tracker can tell whether a
    quote would have been hit

Still a proxy: a seller appearing at our price doesn't prove we were first
in the queue. But at 30 seconds it misses far less than a 15-minute snapshot.

Usage: python watch_books.py [--seconds N]
  Without --seconds it watches until RUN_STARTED (epoch seconds, set by the
  workflow) + WATCH_UNTIL_MIN minutes (default 11), so a run ends in time
  for the next one.
"""
import argparse
import glob
import os
import sys
import time

import shadow
import simcore
import weather

POLL_SECONDS = 30
WATCH_UNTIL_MIN = 11
REWARDS_STATE = "rewards_state.json"


def budget_seconds(now=None, env=None):
    env = os.environ if env is None else env
    try:
        start = float(env.get("RUN_STARTED", ""))
    except ValueError:
        return 0.0
    until = float(env.get("WATCH_UNTIL_MIN", WATCH_UNTIL_MIN)) * 60
    return max(0.0, start + until - (now or time.time()))


def update_extremes(watch, books):
    """Keep the lowest ask and highest bid seen per token."""
    for tok, bk in books.items():
        w = watch.get(tok)
        if w is None:
            continue
        a, b = simcore.best_ask(bk), simcore.best_bid(bk)
        if a is not None and (w.get("min_ask") is None or a < w["min_ask"]):
            w["min_ask"] = a
        if b is not None and (w.get("max_bid") is None or b > w["max_bid"]):
            w["max_bid"] = b
        w["looks"] = w.get("looks", 0) + 1


def main(seconds=None, sleep=time.sleep, fetch=simcore.fetch_books):
    seconds = budget_seconds() if seconds is None else seconds
    if seconds < POLL_SECONDS:
        print(f"Book watcher: {seconds:.0f}s left in this run's slot, not watching.")
        return 0
    deadline = time.time() + seconds
    max_looks = int(seconds // POLL_SECONDS) + 1

    wx_paths = sorted(glob.glob("ledger_wx_*.json"))
    ledgers = {p: weather.load_ledger(p) for p in wx_paths}
    books = {p: shadow.Book(p) for p in sorted(glob.glob("shadow_*.json"))}
    rewards = weather.read_json(REWARDS_STATE, {})
    watch = rewards.setdefault("watch", {})
    for tok in (rewards.get("quotes") or {}):
        watch.setdefault(tok, {})
    wx_book = books.get(weather.SHADOW_FILE)

    msgs, looks, filled_real, filled_shadow = [], 0, 0, 0
    while True:
        tokens = {b["asset"] for led in ledgers.values() for b in led["bets"]
                  if b["status"] == "resting"}
        tokens |= {b["asset"] for bk in books.values() for b in bk.resting()}
        tokens |= set(watch)
        if not tokens:
            break
        got = fetch(tokens)
        asks = {t: simcore.best_ask(bk) for t, bk in got.items()}
        asks = {t: a for t, a in asks.items() if a is not None}
        for p, led in ledgers.items():
            new = weather.check_resting(led, asks, set(), wx_book, how="book watch (~30s)")
            filled_real += len(new)
            if new:
                msgs += [f"**[WX-{p[10:-5].upper()}]**"] + new
        for bk in books.values():
            filled_shadow += bk.check_limits(asks, how="book watch (~30s)")
        update_extremes(watch, got)
        looks += 1
        if looks >= max_looks or time.time() + POLL_SECONDS > deadline:
            break
        sleep(POLL_SECONDS)

    for p, led in ledgers.items():
        weather.write_json(p, led)
    for bk in books.values():
        bk.save()
    if watch:
        simcore.write_json_atomic(REWARDS_STATE, rewards, indent=1)
    print(f"Book watcher: {looks} look(s) over {seconds:.0f}s budget | real bids filled "
          f"{filled_real} | shadow limits filled {filled_shadow} | rewards quotes watched {len(watch)}")
    if msgs:
        print("\n".join(msgs))
        if not weather.notify(msgs):
            sys.exit("Discord delivery failed (files were saved).")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=None)
    sys.exit(main(ap.parse_args().seconds))
