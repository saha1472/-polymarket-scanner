"""READ-ONLY report: did the price move our way after we bought? (a "markout" version of closing-line value)

A strategy with a real information edge buys BEFORE the price improves. So for every settled bet
this compares our entry price with where the same token traded 1 hour, 6 hours and 24 hours later,
and with the final payout. After a few dozen bets that is a far less noisy sign of edge than the
win/loss count, and it works on bets with no audit record.

   markout = price later - price we paid   (per share, before fees; positive = it moved our way)

Two honesty rules. (1) Only price points recorded AFTER the entry count: a stale point from before
the bet would show a price we never traded against; if no point exists inside (entry, entry+h] that
horizon is "unknown" and is left out. (2) Thin markets have wide spreads (some weather markets show
30 cents between bid and ask), and the price history is last-trade prices near the bid side, so a
buy at the ask starts far "behind". The report therefore also shows what was paid OVER THE MID at
entry (from the stored order book) and the +6h markout measured from that mid, which separates the
cost of crossing the spread from any real information edge.

Reads ledger*.json and Polymarket's public price history (read-only GET). Writes nothing unless you
pass --cache FILE (a local cache so reruns do not refetch; do not commit it).
Usage:  python clv_report.py [ledger.json ...] [--cache clv_cache.json]
"""
import argparse
import glob
import json
import math
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

HISTORY_URL = "https://clob.polymarket.com/prices-history"
HORIZONS_H = (1, 6, 24)
SETTLED = ("won", "lost", "split", "stopped")
PAYOUT = {"won": 1.0, "lost": 0.0, "split": 0.5}      # a stopped bet was sold early: no final payout for it


def num(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def parse_iso(s):
    """ISO time -> epoch seconds, or None."""
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def clean_history(rows):
    """[[t, p], ...] sorted by time from the API's {"history": [{"t","p"}]} rows; junk dropped."""
    out = []
    for r in rows or []:
        t, p = num(r.get("t")) if isinstance(r, dict) else None, num(r.get("p")) if isinstance(r, dict) else None
        if t is not None and p is not None and 0 <= p <= 1:
            out.append([t, p])
    return sorted(out)


def price_at(history, t):
    """Last known price at or before t, or None if the history starts later."""
    lo, hi = 0, len(history)
    while lo < hi:
        mid = (lo + hi) // 2
        if history[mid][0] <= t:
            lo = mid + 1
        else:
            hi = mid
    return history[lo - 1][1] if lo else None


def price_after(history, t0, t):
    """Last price recorded strictly after t0 and at or before t, or None. Points from before the
    entry are never used."""
    p = price_at(history, t)
    if p is None:
        return None
    lo, hi = 0, len(history)
    while lo < hi:
        mid = (lo + hi) // 2
        if history[mid][0] <= t:
            lo = mid + 1
        else:
            hi = mid
    return history[lo - 1][1] if history[lo - 1][0] > t0 else None


def entry_mid(bet):
    """Midpoint of the stored order book at entry (best bid and best ask), or None."""
    book = ((bet.get("audit") or {}).get("book")) or {}
    bids = [num(x[0]) for x in book.get("bids") or [] if isinstance(x, (list, tuple)) and x]
    asks = [num(x[0]) for x in book.get("asks") or [] if isinstance(x, (list, tuple)) and x]
    bids, asks = [b for b in bids if b is not None], [a for a in asks if a is not None]
    if not bids or not asks or max(bids) >= min(asks):
        return None
    return (max(bids) + min(asks)) / 2


def markouts(bet, history):
    """{1: m1, 6: m6, 24: m24, "res": m, "spread": paid over mid, "mid6": +6h from the mid} per
    share, None where it cannot be known. The final payout stands in for the price at a horizon only
    when the bet's own end time (`ends`) is at or before that horizon, so a short or late price
    history can never leak the result into an earlier horizon. A bet with no end time gets no
    payout stand-in. Only points after the entry are used."""
    entry_t, p0 = parse_iso(bet.get("opened")), num(bet.get("price"))
    out = {h: None for h in HORIZONS_H}
    out.update({"res": None, "spread": None, "mid6": None})
    if entry_t is None or p0 is None or not history:
        return out
    payout = PAYOUT.get(bet.get("status"))
    last_t = history[-1][0]
    ends_t = parse_iso(bet.get("ends")) if bet.get("ends") else None
    later_by = {}
    for h in HORIZONS_H:
        t = entry_t + h * 3600
        if payout is not None and ends_t is not None and t >= ends_t and t >= last_t:
            later = payout                                                   # the market was already over at this horizon
        elif t <= last_t:
            later = price_after(history, entry_t, t)                         # a recorded price after the entry
        else:
            later = None                                                     # beyond the recorded prices: unknown
        later_by[h] = later
        out[h] = None if later is None else later - p0
    if payout is not None:
        out["res"] = payout - p0
    mid = entry_mid(bet)
    if mid is not None:
        out["spread"] = p0 - mid
        if later_by.get(6) is not None:
            out["mid6"] = later_by[6] - mid
    return out


def mean_se(xs):
    n = len(xs)
    if not n:
        return 0.0, 0.0
    m = sum(xs) / n
    return m, (math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1) / n) if n > 1 else 0.0)


def summarize(rows):
    """rows: markout dicts -> per-horizon {n, mean, se, positive share}."""
    out = {}
    for key in list(HORIZONS_H) + ["res", "spread", "mid6"]:
        xs = [r[key] for r in rows if r.get(key) is not None]
        m, se = mean_se(xs)
        out[key] = {"n": len(xs), "mean": m, "se": se, "pos": sum(1 for x in xs if x > 0) / len(xs) if xs else 0.0}
    return out


def group_name(name, bet):
    return f"{name}/{bet['strategy']}" if bet.get("strategy") else name


def analyse(ledgers, histories):
    """ledgers {name: ledger}; histories {asset: [[t, p], ...]} -> {group: summary}."""
    groups = {}
    for name, led in ledgers.items():
        for b in led.get("bets", []):
            if b.get("status") not in SETTLED or not b.get("asset"):
                continue
            h = histories.get(b["asset"])
            if not h:
                continue
            groups.setdefault(group_name(name, b), []).append(markouts(b, h))
            groups.setdefault("ALL", []).append(markouts(b, h))
    return {g: summarize(rows) for g, rows in groups.items()}


def render(report):
    if not report:
        return "No settled bets with price history yet."
    head = (f"{'group':28} {'n':>4} " + " ".join(f"{'+%dh' % h:>16}" for h in HORIZONS_H)
            + f" {'at result':>16} {'paid over mid':>14} {'+6h vs mid':>16}")
    out = ["Markout per share in cents (+- standard error), and the share of bets where the price moved our way.",
           "Positive = we bought before the price improved. Before fees. n = bets with a +1h value; the other",
           "columns have their own counts (a horizon with no price after the entry is unknown and left out).",
           "", head]
    for g in sorted(report, key=lambda k: (k != "ALL", k)):
        s = report[g]
        cells = []
        for key in list(HORIZONS_H) + ["res", "mid6"]:
            c = s[key]
            cells.append("          n/a    " if not c["n"] else
                         f"{c['mean'] * 100:+6.2f}+-{c['se'] * 100:4.2f} {c['pos']:3.0%}")
        sp = s["spread"]
        paid = "           n/a" if not sp["n"] else f"{sp['mean'] * 100:+8.2f}c n={sp['n']:<3}"
        out.append(f"{g[:28]:28} {s[HORIZONS_H[0]]['n']:4} " + " ".join(f"{c:>16}" for c in cells[:4])
                   + f" {paid:>14} {cells[4]:>16}")
    out += ["", "How to read it: a real edge shows a positive markout at +1h and +6h that is bigger than its",
            "error bar across dozens of bets. 'At result' is simply the realised result per share and",
            "mostly reflects luck at small samples. Compare strategies with each other, not with zero alone.",
            "",
            "Limits of this measurement: Polymarket's price history is hourly last-trade prices, so a later",
            "price can be up to an hour stale; a bet bought by taking the ask starts about half a spread behind;",
            "and once a bet's market has ended the final payout stands in for the price. Treat it as a direction,",
            "not a precise number."]
    return "\n".join(out)


# ---------- network and files ----------

def http_get_json(url, params, timeout=20, attempts=3):
    full = url + "?" + urllib.parse.urlencode(params)
    for a in range(attempts):
        try:
            req = urllib.request.Request(full, headers={"User-Agent": "clv-report"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except Exception:
            time.sleep(0.5 + a)
    return None


def fetch_history(asset, get=http_get_json):
    data = get(HISTORY_URL, {"market": asset, "interval": "max", "fidelity": "60"})
    return clean_history(data.get("history")) if isinstance(data, dict) else []


def load_ledger(path):
    try:
        with open(path, encoding="utf-8") as fh:
            led = json.load(fh)
    except (OSError, ValueError):
        return None
    return led if isinstance(led, dict) and isinstance(led.get("bets"), list) else None


def load_cache(path):
    try:
        with open(path, encoding="utf-8") as fh:
            c = json.load(fh)
        return c if isinstance(c, dict) else {}
    except (OSError, ValueError):
        return {}


def main(argv=None, get=http_get_json, out=print):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("ledgers", nargs="*")
    ap.add_argument("--cache", default=None, help="local file to keep downloaded price histories in")
    a = ap.parse_args(argv)
    paths = a.ledgers or sorted(glob.glob("ledger*.json"))
    ledgers = {}
    for p in paths:
        led = load_ledger(p)
        if led is not None:
            ledgers[os.path.basename(p)[:-5]] = led
    cache = load_cache(a.cache) if a.cache else {}
    assets = sorted({b["asset"] for led in ledgers.values() for b in led["bets"]
                     if b.get("status") in SETTLED and b.get("asset")})
    fetched = 0
    for asset in assets:
        if asset not in cache:
            cache[asset] = fetch_history(asset, get)
            fetched += 1
    if a.cache and fetched:
        tmp = a.cache + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(cache, fh)
        os.replace(tmp, a.cache)
    out(f"{len(assets)} settled tokens ({fetched} downloaded now).\n")
    out(render(analyse(ledgers, cache)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
