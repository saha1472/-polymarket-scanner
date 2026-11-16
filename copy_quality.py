"""READ-ONLY report: how good is the copy trading, trader by trader?

For every copied bet that stored its trader's entry price (`holders[...].entry`), it measures
the PRICE GAP = what we paid minus what the trader paid (positive = we chased). Then it asks:
  - which traders' copies actually made money for us, and how does that compare with the win
    rate and ROI the trader had when we picked them (selection decay)?
  - does paying more than the trader did hurt? (results by size of the gap)

It never writes anything and never touches the network.
Usage:  python copy_quality.py                 (all ledger*.json that have copied bets)
        python copy_quality.py ledger.json ledger_sports.json
"""
import glob
import json
import math
import os
import sys

SETTLED = ("won", "lost", "split", "stopped")
GAP_BUCKETS = ((-1.0, 0.0, "paid no more than the trader"), (0.0, 0.02, "up to 2c more"),
               (0.02, 0.05, "2c to 5c more"), (0.05, 1.0, "more than 5c more"))
MIN_BETS = 5            # traders with fewer copied bets are listed but not ranked


def num(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def trader_entry(bet):
    """(size-weighted trader entry price, primary trader name, claimed win rate, claimed ROI)
    for a bet, or None when no holder stored a usable entry price."""
    rows = []
    for addr, h in (bet.get("holders") or {}).items():
        e, s = num(h.get("entry")), num(h.get("size")) or 0.0
        if e is not None and 0 < e < 1:
            rows.append((e, max(s, 0.0), h.get("name") or addr[:8], num(h.get("win_rate")), num(h.get("roi"))))
    if not rows:
        return None
    total = sum(r[1] for r in rows)
    entry = sum(r[0] * r[1] for r in rows) / total if total > 0 else sum(r[0] for r in rows) / len(rows)
    main = max(rows, key=lambda r: r[1])
    return entry, main[2], main[3], main[4]


def copied_bets(ledger):
    """Settled bets with a usable trader entry price: dicts with gap, trader, pnl, stake, won."""
    out = []
    for b in ledger.get("bets", []):
        if b.get("status") not in SETTLED or num(b.get("pnl")) is None or not num(b.get("stake")):
            continue
        te = trader_entry(b)
        price = num(b.get("price"))
        if te is None or price is None:
            continue
        entry, name, win_rate, roi = te
        out.append({"gap": price - entry, "trader": name, "pnl": num(b["pnl"]), "stake": num(b["stake"]),
                    "won": b["status"] == "won", "claimed_win": win_rate, "claimed_roi": roi,
                    "price": price, "entry": entry})
    return out


def mean_se(xs):
    n = len(xs)
    if n == 0:
        return 0.0, 0.0
    m = sum(xs) / n
    if n < 2:
        return m, 0.0
    return m, math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1) / n)


def summarize(rows):
    """One group's numbers. ROI is profit per dollar staked, computed per bet."""
    if not rows:
        return {"n": 0}
    rois = [r["pnl"] / r["stake"] for r in rows]
    roi, roi_se = mean_se(rois)
    gap, _ = mean_se([r["gap"] for r in rows])
    claimed = [r["claimed_roi"] for r in rows if r["claimed_roi"] is not None]
    cw = [r["claimed_win"] for r in rows if r["claimed_win"] is not None]
    return {"n": len(rows), "wins": sum(1 for r in rows if r["won"]), "pnl": sum(r["pnl"] for r in rows),
            "roi": roi, "roi_se": roi_se, "gap": gap,
            "win_rate": sum(1 for r in rows if r["won"]) / len(rows),
            "claimed_win": sum(cw) / len(cw) if cw else None,
            "claimed_roi": sum(claimed) / len(claimed) if claimed else None}


def analyse(ledgers):
    """ledgers: {name: ledger dict} -> {"by_ledger", "by_trader", "by_gap", "all"}."""
    by_ledger, by_trader, allrows = {}, {}, []
    for name, led in ledgers.items():
        rows = copied_bets(led)
        if rows:
            by_ledger[name] = summarize(rows)
        for r in rows:
            by_trader.setdefault(r["trader"], []).append(r)
        allrows += rows
    gaps = []
    for lo, hi, label in GAP_BUCKETS:
        gaps.append((label, summarize([r for r in allrows if lo < r["gap"] <= hi])))
    return {"by_ledger": by_ledger, "by_trader": {k: summarize(v) for k, v in by_trader.items()},
            "by_gap": gaps, "all": summarize(allrows)}


def render(rep):
    a = rep["all"]
    if not a["n"]:
        return "No settled copied bets with a stored trader entry price yet."
    out = [f"Copied bets with a known trader entry price: {a['n']} "
           f"(P&L ${a['pnl']:+.2f}, ROI {a['roi']:+.1%} per bet +-{a['roi_se']:.1%}, "
           f"we paid on average {a['gap'] * 100:+.1f}c more than the trader)", ""]
    out.append("By ledger")
    for name, s in sorted(rep["by_ledger"].items()):
        out.append(f"  {name:22} n={s['n']:3} win {s['win_rate']:.0%}  P&L ${s['pnl']:+7.2f}  "
                   f"ROI {s['roi']:+.1%} (+-{s['roi_se']:.1%})  gap {s['gap'] * 100:+.1f}c")
    out += ["", "Does paying more than the trader hurt?"]
    for label, s in rep["by_gap"]:
        if s["n"]:
            out.append(f"  {label:30} n={s['n']:3}  ROI {s['roi']:+.1%} (+-{s['roi_se']:.1%})  win {s['win_rate']:.0%}")
        else:
            out.append(f"  {label:30} n=  0")
    out += ["", f"By trader (at least {MIN_BETS} copied bets; 'claimed' = what the trader's record said when picked)"]
    ranked = sorted(((k, v) for k, v in rep["by_trader"].items() if v["n"] >= MIN_BETS), key=lambda kv: -kv[1]["roi"])
    for name, s in ranked:
        cw = "n/a" if s["claimed_win"] is None else f"{s['claimed_win']:.0%}"
        cr = "n/a" if s["claimed_roi"] is None else f"{s['claimed_roi']:+.0%}"
        out.append(f"  {name[:22]:22} n={s['n']:3} P&L ${s['pnl']:+6.2f}  ROI {s['roi']:+.1%} (+-{s['roi_se']:.1%})  "
                   f"win {s['win_rate']:.0%} vs claimed {cw}, ROI claimed {cr}  gap {s['gap'] * 100:+.1f}c")
    few = [k for k, v in rep["by_trader"].items() if v["n"] < MIN_BETS]
    if few:
        out.append(f"  ({len(few)} more traders with fewer than {MIN_BETS} copied bets are not ranked)")
    out += ["", "Read this carefully: with this few bets per trader the standard errors are large. A trader",
            "whose claimed ROI is far above ours is the usual pattern (a hot streak fading), not proof of a bad trader."]
    return "\n".join(out)


def load(path):
    try:
        with open(path, encoding="utf-8") as fh:
            led = json.load(fh)
    except (OSError, ValueError):
        return None
    return led if isinstance(led, dict) and isinstance(led.get("bets"), list) else None


def main(paths=None):
    paths = paths or sorted(glob.glob("ledger*.json"))
    ledgers = {}
    for p in paths:
        led = load(p)
        if led is not None:
            ledgers[os.path.basename(p)[:-5]] = led
    print(render(analyse(ledgers)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:] or None))
