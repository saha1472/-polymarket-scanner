"""SHADOW experiment (never trades, never touches a ledger): politics plays where
several tracked traders hold the same side.

Idea (from the polyintel.pages.dev "smart money" site): when 3 or more good
politics traders are on the same side, and the price hasn't run away from where
they got in, is that side better than average? Our real scans split traders by
topic and look at markets ending within 36 hours, so they almost never see this.

What it does each run:
  1. Settles earlier shadow records on Polymarket's official payout.
  2. Builds the tracked pool: the top politics traders on the leaderboard, scored
     ONLY on their resolved politics bets (last 30 days). Anyone profitable with
     at least 15 such bets is tracked; `strict` marks those that also pass the
     bot's normal bar (58% wins, +10% ROI, 30+ bets). Cached 6 hours.
  3. Reads their open politics positions on markets ending within 30 days.
  4. Records a $1 would-have buy at the best ask when
        POLITICS    3+ tracked traders on the side, ask within 5c of their average
                    entry ("consensus"), or further away ("consensus_chased")
        POLITICS_2  exactly 2 traders, ask within 5c (the control group: does the
                    third trader matter?)
     One record per signal, one per event per group. Each record keeps who agreed,
     their entries and how many of them pass the strict bar.

Results show up in shadow_report.py (strategies POLITICS and POLITICS_2).
The first run may only warm the event-tag cache (a time budget keeps it short);
the next runs carry on from where it stopped.

Usage: python shadow_politics.py
"""
import os
import sys
import time
from datetime import datetime, timezone

# signals.py reads its scan name from argv; this script is not a scan
_argv = sys.argv
sys.argv = ["signals.py"]
import signals                                  # noqa: E402
sys.argv = _argv
import shadow                                   # noqa: E402
import simcore                                  # noqa: E402

STRATEGY = "POLITICS"
CONTROL = "POLITICS_2"
TAG = "politics"                      # Gamma tag that marks a politics event
TRADERS_FILE = "traders_politics.json"
SHADOW_FILE = shadow.shadow_file(STRATEGY)      # shadow_politics.json

LEADERBOARD_SIZE = 200
MIN_BETS = 15                         # politics bets needed before a trader is tracked
MIN_AGREE = 3
HOURS_MIN = 1
HOURS_MAX = 24 * 30                   # markets ending within about 30 days
MAX_CHASE = 0.05                      # ask within 5c of the traders' average entry
MIN_PRICE = 0.40                      # price band, same as the category scans
MAX_PRICE = 0.90
MIN_POSITION_USD = 50                 # ignore tiny positions, like the real scans
FEE_RATE = 0.07                       # the worst case, so results err on the cautious side
MAX_PER_RUN = 20
MAX_CONTROL_PER_RUN = 10
SCORE_REFRESH_HOURS = 6
TIME_BUDGET_S = 300                   # stop warming tags after this long, carry on next run

# Politics traders from outside lists that our own check found worth watching
# (3 Nov-heavy results, so shadow only). Tracked even if they miss the leaderboard.
# mrworldwide (added 11 Nov, from predicting.top): 299 bets in 30 days, 77% win,
# +21.8% ROI, still +15.6% outside the 3-5 Nov election days.
WATCHLIST = {
    "0xc8b9a30184244d427169cf62485dde6041b2b836": "SnowLover7",
    "0x1c144e30f405a25f991cbd8baa15d40599090869": "bernardbulletin",
    "0x3dca854f147d9b9ecc56d37800f36b347ce23c13": "Ryxon21",
    "0x1ee9a5fc09665909c0cce297c581703bfbb9197f": "vinii",
    "0xf1f9a438f2697381b4ac8eb283d6f4dd772d1245": "tunatyler",
    "0x98d04f9eb52d08ef9697f6eb8d7cfe8d46e5f14a": "mrworldwide",
}


# ---------- event tags (which events are politics) ----------

def warm_tags(slugs, deadline):
    """Look up the tags of events not seen before, a batch at a time, until the
    deadline. Returns True when every slug has been tried, False if the deadline
    cut it short. A lookup that fails isn't cached, so it is simply tried again
    next run (meanwhile that event counts as not politics)."""
    todo = sorted({s for s in slugs if s and s not in signals.TAGS})
    for i in range(0, len(todo), 60):
        if time.monotonic() > deadline:
            return False
        signals.pmap(signals.event_tags, todo[i:i + 60], workers=6)
    return True


def is_politics(slug):
    return TAG in (signals.TAGS.get(slug) or [])


def save_tags():
    """Merge into the tags cache on disk (the scans have finished by now)."""
    merged = signals.read_json(signals.TAGS_FILE, {})
    merged.update(signals.TAGS)
    if len(merged) > 30000:
        merged = dict(signals.TAGS)
    tmp = f"{signals.TAGS_FILE}.politics.tmp"
    signals.write_json(tmp, merged)
    os.replace(tmp, signals.TAGS_FILE)


# ---------- step 1: the tracked pool ----------

def leaderboard_wallets():
    wallets = {}
    for period in ("MONTH", "ALL"):
        rows = signals.data_v2("leaderboard", {"timePeriod": period, "orderBy": "PNL",
                                               "limit": LEADERBOARD_SIZE,
                                               "category": "POLITICS"}, max_pages=1) or []
        for row in map(signals.v1_leader, rows):
            wallet = row.get("proxyWallet")
            if not wallet:
                continue
            pnl, vol = signals.num(row.get("pnl")), signals.num(row.get("vol"))
            if vol > 0 and pnl / vol < signals.MIN_PNL_TO_VOLUME:
                continue                      # weeds out market makers
            wallets[wallet] = row.get("userName") or wallet[:10]
    for wallet, name in WATCHLIST.items():
        wallets.setdefault(wallet, name)
    return wallets


def score_politics(wallets, deadline):
    """{wallet: score} on resolved politics bets only, or None if the tag cache
    couldn't be filled in time (the next run carries on)."""
    histories = dict(zip(wallets, signals.pmap(signals.trader_history, wallets, workers=6)))
    if not warm_tags((s for h in histories.values() if h for s, _, _ in h), deadline):
        return None
    scores = {}
    for wallet, hist in histories.items():
        bets = [x for x in (hist or []) if is_politics(x[0])]
        if len(bets) < MIN_BETS:
            continue
        cost = sum(c for _, _, c in bets)
        scores[wallet] = {"name": wallets[wallet], "n": len(bets),
                          "win_rate": sum(1 for _, p, _ in bets if p > 0) / len(bets),
                          "roi": sum(p for _, p, _ in bets) / cost}
    return scores


def tracked_pool(deadline):
    """{wallet: score + strict flag} of tracked traders, or None while warming."""
    cache = signals.read_json(TRADERS_FILE, {})
    age = signals.hours_until(cache.get("scored_at"))
    if age is not None and -age < SCORE_REFRESH_HOURS and "scores" in cache:
        scores = cache["scores"]
        print(f"Using politics trader scores from {-age:.1f}h ago.")
    else:
        wallets = leaderboard_wallets()
        print(f"{len(wallets)} politics wallets (leaderboard + watchlist). Scoring...")
        scores = score_politics(wallets, deadline)
        if scores is None:
            return None
        signals.write_json(TRADERS_FILE, {"scored_at": datetime.now(timezone.utc).isoformat(),
                                          "scores": scores}, indent=1)
    return {w: dict(s, strict=signals.passes_bar(s)) for w, s in scores.items()
            if s["roi"] > 0 and s["n"] >= MIN_BETS}


# ---------- step 2: what they hold ----------

def find_plays(tracked, deadline):
    """Open politics sides held by tracked traders, with who holds them. Markets
    where tracked traders are on opposite sides are left out. None while the tag
    cache is still warming."""
    wallets = list(tracked)
    all_rows = signals.pmap(lambda w: signals.positions(w, max_pages=2, status="OPEN") or [],
                            wallets)

    def keep(p):
        hrs = signals.hours_until(p.get("endDate"))
        return (not p.get("redeemable") and hrs is not None and HOURS_MIN <= hrs <= HOURS_MAX
                and signals.num(p.get("size")) * signals.num(p.get("curPrice")) >= MIN_POSITION_USD
                and p.get("asset"))

    if not warm_tags((p.get("eventSlug") for rows in all_rows for p in rows if keep(p)),
                     deadline):
        return None
    by_asset, assets_by_condition = {}, {}
    for wallet, rows in zip(wallets, all_rows):
        for p in rows:
            if not keep(p) or not is_politics(p.get("eventSlug")):
                continue
            s = by_asset.setdefault(p["asset"], {
                "asset": p["asset"], "condition": p.get("conditionId"),
                "title": p.get("title", "?"), "event": p.get("eventSlug"),
                "outcome": p.get("outcome", "?"),
                "hours": signals.hours_until(p.get("endDate")),
                "traders": [], "entries": [], "strict": 0, "stats": []})
            t = tracked[wallet]
            s["traders"].append(t["name"])
            s["entries"].append(signals.num(p.get("avgPrice")))
            s["strict"] += bool(t["strict"])
            s["stats"].append({"name": t["name"], "n": t["n"], "win_rate": t["win_rate"],
                               "roi": t["roi"], "strict": bool(t["strict"])})
            assets_by_condition.setdefault(s["condition"], set()).add(p["asset"])
    plays = []
    for s in by_asset.values():
        if len(assets_by_condition[s["condition"]]) > 1:
            continue                          # tracked traders are on opposite sides
        s["agree"] = len(s["traders"])
        s["avg_entry"] = sum(s["entries"]) / s["agree"]
        plays.append(s)
    return sorted(plays, key=lambda s: (-s["agree"], s["asset"]))


# ---------- step 3: record ----------

def record_plays(book, plays, books):
    """Add shadow records for the plays that qualify. `books`: asset -> order book.
    Returns {"consensus": n, "consensus_chased": n, "two_agree": n}."""
    added = {"consensus": 0, "consensus_chased": 0, "two_agree": 0}
    seen = {(b["strategy"], b.get("event")) for b in book.bets if b.get("event")}
    for s in plays:
        if s["agree"] < 2:
            continue
        ask = simcore.best_ask(books.get(s["asset"]))
        if ask is None or not MIN_PRICE <= ask <= MAX_PRICE:
            continue
        near = ask - s["avg_entry"] <= MAX_CHASE + 1e-9      # 0.65 - 0.60 is 0.05000000000000004
        if s["agree"] >= MIN_AGREE:
            strategy, reason = STRATEGY, "consensus" if near else "consensus_chased"
            cap = MAX_PER_RUN
        elif near:
            strategy, reason, cap = CONTROL, "two_agree", MAX_CONTROL_PER_RUN
        else:
            continue
        if (strategy, s["event"]) in seen or (strategy, "skipped", s["asset"]) in book.keys:
            continue                          # one record per signal, one per event
        if sum(added[r] for r in _reasons(strategy)) >= cap:
            continue
        rec = book.add_taker(strategy, reason, asset=s["asset"], condition=s["condition"],
                             title=s["title"], price=ask, fee_rate=FEE_RATE,
                             event=s["event"], outcome=s["outcome"],
                             extra={"traders": s["traders"], "entries": s["entries"],
                                    "avg_entry": s["avg_entry"], "agree": s["agree"],
                                    "strict": s["strict"], "hours": s["hours"],
                                    "trader_stats": s["stats"]})
        if rec:
            added[reason] += 1
            seen.add((strategy, s["event"]))
    return added


def _reasons(strategy):
    return ("consensus", "consensus_chased") if strategy == STRATEGY else ("two_agree",)


def wanted_assets(plays, book):
    """Assets whose order book is worth fetching: 2+ agree and not recorded yet."""
    return [s["asset"] for s in plays if s["agree"] >= 2
            and (STRATEGY, "skipped", s["asset"]) not in book.keys
            and (CONTROL, "skipped", s["asset"]) not in book.keys]


def results_line(book):
    rows = []
    for strategy, reason in ((STRATEGY, "consensus"), (STRATEGY, "consensus_chased"),
                             (CONTROL, "two_agree")):
        mine = [b for b in book.bets if b["strategy"] == strategy and b["reason"] == reason]
        if not mine:
            continue
        st = shadow.summarize(mine)
        rows.append(f"{strategy}/{reason}: {len(mine)} records, {st['settled']} settled "
                    f"({st['won']} won, ROI {st['roi']:+.1%}), {st['open']} open")
    return rows


def main():
    deadline = time.monotonic() + TIME_BUDGET_S
    book = shadow.Book(SHADOW_FILE)
    try:
        settled = book.settle(signals.get)
        print(f"Politics shadow: {len(book.bets)} records, {settled} settled now")
        tracked = tracked_pool(deadline)
        plays = None if tracked is None else find_plays(tracked, deadline)
        if plays is None:
            print("Still warming the event-tag cache; carrying on next run.")
            return 0
        strict = sum(1 for s in tracked.values() if s["strict"])
        print(f"{len(tracked)} tracked politics traders ({strict} pass the normal bar), "
              f"{len(plays)} open politics sides, "
              f"{sum(1 for s in plays if s['agree'] >= MIN_AGREE)} with {MIN_AGREE}+ agreeing")
        books = simcore.fetch_books(wanted_assets(plays, book))
        added = record_plays(book, plays, books)
        print("New records:", added)
        for line in results_line(book):
            print(line)
        return 0
    finally:
        book.save()
        save_tags()


if __name__ == "__main__":
    sys.exit(main())
