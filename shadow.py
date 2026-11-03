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
  unfilled  a weather resting bid that expired unfilled: would it have won?

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
        extra = {"limit": limit_price, "expires": expires.isoformat()}
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


def roi_stats(bets):
    """(n, mean ROI per bet, standard error) over settled bets."""
    rois = [b["pnl"] / (b["stake"] + b["fee"]) for b in bets
            if b.get("status") in SETTLED and simcore.finite(b.get("pnl"))
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
    s = [b for b in bets if b.get("status") in SETTLED]
    n, mean, se = roi_stats(s)
    return {"settled": n, "won": sum(b["status"] == "won" for b in s),
            "pnl": sum(b["pnl"] for b in s), "roi": mean, "se": se,
            "open": sum(b.get("status") == "open" for b in bets),
            "resting": sum(b.get("status") == "resting" for b in bets),
            "expired": sum(b.get("status") == "expired" for b in bets)}


# ---------- automatic strategy switch ----------

SWITCH_MIN_N = 30        # never judge on fewer settled bets than this
SWITCH_WINDOW = 100      # judge on the most recent 100 settled real bets
PAUSE_Z = 1.645          # pause only if even the optimistic end (90%, one-sided) is a loss
RESUME_Z = 0.674         # resume when the cautious end (75%, one-sided) of the
                         # shadow results since the pause is a profit


def is_paused(ledger):
    return bool((ledger.get("auto") or {}).get("paused"))


def switch_update(ledger, shadow_bets, strategy, now=None, judge_from=""):
    """Pause or resume `strategy` in place. Returns a message on a change, else None.
    Pausing looks at the real ledger (only bets opened at or after `judge_from`,
    so results from a since-fixed bug don't count); resuming looks at the shadow
    bets the strategy kept recording while paused (reason "auto_paused")."""
    now = now or now_iso()
    auto = ledger.get("auto") or {}
    if not auto.get("paused"):
        real = sorted((b for b in ledger["bets"] if b.get("status") in SETTLED
                       and b.get("opened", "") >= (judge_from or "")),
                      key=lambda b: b.get("closed", ""))[-SWITCH_WINDOW:]
        n, mean, se = roi_stats(real)
        if n >= SWITCH_MIN_N and mean + PAUSE_Z * se < 0:
            why = f"last {n} settled bets: ROI {mean:+.1%} per bet (±{se:.1%})"
            ledger["auto"] = {"paused": True, "since": now, "why": why,
                              "history": auto.get("history", [])}
            return (f"🤖 Auto-paused {strategy}: {why}. Still tracking it in shadow; "
                    f"it resumes by itself if those results turn convincingly positive.")
        return None
    since = auto.get("since", "")
    sh = [b for b in shadow_bets if b.get("strategy") == strategy and b.get("kind") == "skipped"
          and b.get("reason") == "auto_paused" and b.get("created", "") >= since]
    n, mean, se = roi_stats(sh)
    if n >= SWITCH_MIN_N and mean - RESUME_Z * se > 0:
        why = f"{n} shadow bets since the pause: ROI {mean:+.1%} per bet (±{se:.1%})"
        hist = auto.get("history", []) + [{"paused": since, "resumed": now, "why": auto.get("why")}]
        ledger["auto"] = {"paused": False, "resumed": now, "why": why, "history": hist}
        return f"🤖 Auto-resumed {strategy}: {why}."
    return None
