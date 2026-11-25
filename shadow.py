"""Shadow bets: "what would have happened" records that never touch a ledger.

The real paper ledgers stay exactly as they are. Next to them, every scan
keeps a shadow file (shadow_<scan>.json) with:

  skipped   a signal the bot found but didn't bet, and why (per-event or
            per-trader cap, cash, daily loss limit, auto-pause, price filter,
            chase limit...). Priced at the real best ask at that moment.
  limit     copy trades only: a maker bid at the copied traders' average entry
            instead of buying at the ask. Fills when a later look at the book
            (scan snapshots, or the book watcher every ~30s) shows a seller at
            or below our price. No fee (makers pay none).
  unfilled  (old rows only, no longer written) a weather resting bid that expired
            unfilled; never counted as a result, see is_result

Each record settles on Polymarket's official payout like a real bet. One
record per (strategy, kind, asset): a signal seen on every run is one record.

The automatic strategy switch (simcore.switch_update) also lives off this:
while a strategy is auto-paused, its signals keep landing here as
"skipped: auto_paused", and that is what decides whether it comes back.
"""
import json
import math
import os
from datetime import datetime, timedelta, timezone

import simcore

KINDS = ("skipped", "limit", "unfilled")
STATUSES = ("open", "resting", "expired", "won", "lost", "split")
LIMIT_HOURS = 24          # a shadow limit bid lives at most this long


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def shadow_file(scan):
    return f"shadow_{scan.lower()}.json"


class Book:
    def __init__(self, path):
        self.path = path
        self.bets = []
        if os.path.exists(path):
            try:
                with open(path) as fh:
                    data = json.load(fh)
                bets = data.get("bets") if isinstance(data, dict) else None
                if not isinstance(bets, list) or not all(self._sane(b) for b in bets):
                    raise ValueError("bad structure")
                self.bets = bets
            except (OSError, ValueError) as err:
                # shadow data is evidence, not money: keep the broken file aside
                # and start a fresh one rather than stopping the bot
                bad = f"{path}.bad"
                print(f"WARNING: {path} unreadable ({err}); moved to {bad}, starting fresh")
                try:
                    os.replace(path, bad)
                except OSError:
                    pass
        self.keys = {(b["strategy"], b["kind"], b["asset"]) for b in self.bets}
        self.added = 0

    @staticmethod
    def _sane(b):
        return (isinstance(b, dict) and b.get("kind") in KINDS and b.get("status") in STATUSES
                and isinstance(b.get("asset"), str)
                and all(simcore.finite(b.get(k)) for k in ("price", "shares", "stake", "fee")))

    def save(self):
        simcore.write_json_atomic(self.path, {"bets": self.bets})

    # ---------- adding ----------

    def add(self, strategy, kind, reason, *, asset, condition, title, price, shares,
            stake, fee, event=None, outcome=None, extra=None, status="open"):
        key = (strategy, kind, asset)
        if key in self.keys:
            return None
        for v in (price, shares, stake, fee):
            if not simcore.finite(v) or v < 0:
                return None
        rec = {"strategy": strategy, "kind": kind, "reason": reason, "asset": asset,
               "condition": condition, "title": title, "event": event, "outcome": outcome,
               "price": price, "shares": shares, "stake": stake, "fee": fee,
               "status": status, "created": now_iso()}
        if status == "open":
            rec["opened"] = rec["created"]
        if extra:
            rec.update(extra)
        self.bets.append(rec)
        self.keys.add(key)
        self.added += 1
        return rec

    def add_taker(self, strategy, reason, *, asset, condition, title, price, fee_rate,
                  dollars=1.0, **kw):
        """A would-have market buy of `dollars` at `price` (the best ask seen)."""
        if not simcore.finite(price) or not 0 < price < 1:
            return None
        shares = dollars / price
        fee = shares * fee_rate * price * (1 - price)
        return self.add(strategy, "skipped", reason, asset=asset, condition=condition,
                        title=title, price=price, shares=shares, stake=dollars, fee=fee, **kw)

    def add_limit(self, strategy, *, asset, condition, title, limit_price, best_ask,
                  fee_rate, ends=None, dollars=1.0, **kw):
        """A maker bid at `limit_price`. If the ask is already at or below it,
        it would cross straight away: recorded as filled at the ask, with fee."""
        if not simcore.finite(limit_price) or not 0 < limit_price < 1:
            return None
        expires = datetime.now(timezone.utc) + timedelta(hours=LIMIT_HOURS)
        if ends:
            try:
                expires = min(expires, datetime.fromisoformat(ends.replace("Z", "+00:00")))
            except (ValueError, TypeError):
                pass
        extra = dict(kw.pop("extra", None) or {}, limit=limit_price,
                     expires=expires.isoformat())
        if best_ask is not None and best_ask <= limit_price:
            shares = dollars / best_ask
            extra["fill"] = {"time": now_iso(), "seen_ask": best_ask, "how": "crossed at once"}
            return self.add(strategy, "limit", "trader_entry", asset=asset, condition=condition,
                            title=title, price=best_ask, shares=shares, stake=dollars,
                            fee=shares * fee_rate * best_ask * (1 - best_ask), extra=extra, **kw)
        return self.add(strategy, "limit", "trader_entry", asset=asset, condition=condition,
                        title=title, price=limit_price, shares=dollars / limit_price,
                        stake=dollars, fee=0.0, extra=extra, status="resting", **kw)

    # ---------- limit fills ----------

    def resting(self):
        return [b for b in self.bets if b["status"] == "resting"]

    def check_limits(self, asks, how="snapshot"):
        """asks: token -> best ask seen now. Expiry first, then fill proxy."""
        filled = 0
        now = now_iso()
        for b in self.resting():
            if now >= b["expires"]:
                b["status"] = "expired"
                continue
            ask = asks.get(b["asset"])
            if ask is not None and ask <= b["limit"]:
                b["status"] = "open"
                b["opened"] = now
                b["fill"] = {"time": now, "seen_ask": ask, "how": how}
                filled += 1
        return filled

    # ---------- settlement ----------

    def settle(self, get):
        todo = [b for b in self.bets if b["status"] == "open"]
        if not todo:
            return 0
        pays = simcore.final_payouts(get, todo)
        n = 0
        for b in todo:
            pay = pays.get(b["asset"])
            if pay is None:
                continue
            b["status"] = simcore.outcome_label(pay)
            b["pnl"] = b["shares"] * pay - b["stake"] - b["fee"]
            b["closed"] = now_iso()
            n += 1
        return n


# ---------- statistics (shared with the strategy switch and reports) ----------

SETTLED = ("won", "lost", "split", "stopped")


def is_result(b):
    """False for an "unfilled" record: a resting bid that expired without a fill, kept only to see
    how it would have ended. Such a record is not a bet that happened, and what it would have
    earned is not what the bid earns: the bids that never fill are the ones where the price
    ran away from them. No statistic, gate or switch may count it."""
    return b.get("kind") != "unfilled"


def roi_stats(bets):
    """(n, mean ROI per bet, standard error) over settled bets."""
    rois = [b["pnl"] / (b["stake"] + b["fee"]) for b in bets
            if is_result(b) and b.get("status") in SETTLED and simcore.finite(b.get("pnl"))
            and (b.get("stake", 0) + b.get("fee", 0)) > 0]
    n = len(rois)
    if n == 0:
        return 0, 0.0, float("inf")
    mean = sum(rois) / n
    if n < 2:
        return n, mean, float("inf")
    var = sum((r - mean) ** 2 for r in rois) / (n - 1)
    return n, mean, math.sqrt(var / n)


def summarize(bets):
    bets = [b for b in bets if is_result(b)]
    s = [b for b in bets if b.get("status") in SETTLED]
    n, mean, se = roi_stats(s)
    return {"settled": n, "won": sum(b["status"] == "won" for b in s),
            "pnl": sum(b["pnl"] for b in s), "roi": mean, "se": se,
            "open": sum(b.get("status") == "open" for b in bets),
            "resting": sum(b.get("status") == "resting" for b in bets),
            "expired": sum(b.get("status") == "expired" for b in bets)}


# ---------- automatic strategy switch ----------

# A pause or a resume is a verdict on noisy results, so it is reached carefully:
#   - bets on one event, or opened on one day, move together (a day's weather, a match with
#     several markets), so the error bar groups them instead of counting each bet as news;
#   - the verdict is looked at on a fixed schedule, every REVIEW_EVERY newly settled bets,
#     not on every scan: each extra look is another chance for luck to cross the line;
#   - it needs bets from at least SWITCH_MIN_DAYS different days.
SWITCH_MIN_N = 30        # never judge on fewer settled bets than this
SWITCH_MIN_DAYS = 6      # ... or on fewer different days than this
SWITCH_WINDOW = 100      # judge on the most recent 100 settled real bets
REVIEW_EVERY = 25        # look again after this many more settled bets
PAUSE_Z = 2.6            # pause only if even the optimistic end of the range is a loss
RESUME_Z = 1.645         # resume when the cautious end (95%, one-sided) of the
                         # shadow results since the pause is a profit
# Chosen by simulation (see the pull request that introduced them): a strategy with no edge at all, over 300
# bets with eight a day, was paused by the old rule 46-88% of the time (the more a day's bets move
# together, the more often) and resumed 68-77%; with these settings 2-10% and 13-17%. A clear loser
# (-35% per bet) is still paused within about 100-130 bets, 70-100% of the time.


def is_paused(ledger):
    return bool((ledger.get("auto") or {}).get("paused"))


def _day(b):
    return (b.get("opened") or b.get("created") or "")[:10] or None


def _group_se(rows, key, mean):
    """Standard error of the mean when rows sharing `key` move together (a row whose
    key is None is a group of its own). Fewer than two groups: no error bar (inf)."""
    groups = {}
    for i, r in enumerate(rows):
        k = r[key] if key is not None else None
        groups.setdefault(("own", i) if k is None else ("key", k), []).append(r[0] - mean)
    c = len(groups)
    if c < 2:
        return float("inf")
    return math.sqrt(c / (c - 1) * sum(sum(g) ** 2 for g in groups.values())) / len(rows)


def switch_stats(bets):
    """(n, mean ROI per bet, standard error, different days) over settled bets. The error
    bar is the widest of: every bet on its own, bets grouped by event, bets grouped by the day
    they were opened."""
    rows = [(b["pnl"] / (b["stake"] + b["fee"]), b.get("event") or b.get("condition"), _day(b))
            for b in bets if is_result(b) and b.get("status") in SETTLED and simcore.finite(b.get("pnl"))
            and (b.get("stake", 0) + b.get("fee", 0)) > 0]
    n = len(rows)
    if n == 0:
        return 0, 0.0, float("inf"), 0
    mean = sum(r[0] for r in rows) / n
    days = len({r[2] for r in rows if r[2]})
    se = max(_group_se(rows, None, mean), _group_se(rows, 1, mean), _group_se(rows, 2, mean))
    return n, mean, se, days


def review_due(settled, auto):
    """True when `settled` bets are enough for a verdict and REVIEW_EVERY more have
    settled since the last look (auto["reviewed"]). If the count has gone down since
    the last look (judging restarted), the stored look is cleared in `auto` itself."""
    if auto.get("reviewed", 0) > settled:
        auto["reviewed"] = 0        # judging restarted: look afresh
    return settled >= max(SWITCH_MIN_N, auto.get("reviewed", 0) + REVIEW_EVERY)


def switch_update(ledger, shadow_bets, strategy, now=None, judge_from=""):
    """Pause or resume `strategy` in place. Returns a message on a change, else None.
    Pausing looks at the real ledger (only bets opened at or after `judge_from`,
    so results from a since-fixed bug don't count); resuming looks at the shadow
    bets the strategy kept recording while paused (reason "auto_paused"). Either way
    it only looks every REVIEW_EVERY settled bets (see above)."""
    now = now or now_iso()
    auto = ledger.get("auto") or {}
    real = sorted((b for b in ledger["bets"] if b.get("status") in SETTLED
                   and b.get("opened", "") >= (judge_from or "")),
                  key=lambda b: b.get("closed", ""))
    if not auto.get("paused"):
        if not review_due(len(real), auto):
            return None
        ledger["auto"] = dict(auto, reviewed=len(real))
        n, mean, se, days = switch_stats(real[-SWITCH_WINDOW:])
        if n >= SWITCH_MIN_N and days >= SWITCH_MIN_DAYS and mean + PAUSE_Z * se < 0:
            why = f"last {n} settled bets over {days} days: ROI {mean:+.1%} per bet (±{se:.1%})"
            ledger["auto"] = {"paused": True, "since": now, "why": why,
                              "history": auto.get("history", [])}
            return (f"🤖 Auto-paused {strategy}: {why}. Still tracking it in shadow; "
                    f"it resumes by itself if those results turn convincingly positive.")
        return None
    since = auto.get("since", "")
    sh = [b for b in shadow_bets if b.get("strategy") == strategy and b.get("kind") == "skipped"
          and b.get("reason") == "auto_paused" and b.get("created", "") >= since]
    done = sum(1 for b in sh if b.get("status") in SETTLED)
    if not review_due(done, auto):
        return None
    ledger["auto"] = dict(auto, reviewed=done)
    n, mean, se, days = switch_stats(sh)
    if n >= SWITCH_MIN_N and days >= SWITCH_MIN_DAYS and mean - RESUME_Z * se > 0:
        why = f"{n} shadow bets over {days} days since the pause: ROI {mean:+.1%} per bet (±{se:.1%})"
        hist = auto.get("history", []) + [{"paused": since, "resumed": now, "why": auto.get("why")}]
        # the bets that led to the pause are still in the window: the next look waits for REVIEW_EVERY new ones
        ledger["auto"] = {"paused": False, "resumed": now, "why": why, "history": hist,
                          "reviewed": len(real)}
        return f"🤖 Auto-resumed {strategy}: {why}."
    return None
