"""READ-ONLY summary of the shadow evidence and the automatic strategy switch.

For each strategy: the real ledger (settled bets, ROI with its error range,
auto-pause state) next to every kind of shadow record, broken down by why
the bet wasn't placed. Use it to answer questions like "do the per-trader
caps help?", "would the unfilled weather bids have won?", "is bidding at
the trader's entry better than buying at the ask?".

The wide-pool lines test a looser trader bar (see signals.wide_band); they are
shadow only, so a good result there is a reason to loosen the bar, not a profit.

Usage: python shadow_report.py
"""
import glob
import json
import sys

import shadow

LEDGERS = {"ledger.json": "GENERAL"}

# The wide-pool test: profitable traders the strict bar leaves out, copied in
# shadow only. Compare each band with the real ledger (which copies the strict
# pool, with its caps), and the "all bands" line for the whole pool.
WIDE_LABELS = {"wide_low_roi": "wide pool: ROI under 10%",
               "wide_few_bets": "wide pool: 15-29 bets only",
               "wide_low_win": "wide pool: win rate under 58%"}
WIDE_ALL = "wide pool: all bands"
CHEAP_LABEL = "cheap buckets 15-39c"
# The new trader bar (shadow only, from 10 Nov): bets whose traders pass it,
# real and shadow together, next to real bets whose traders don't.
ALT_YES = "new bar: traders pass"
ALT_NO = "new bar: real bets, traders fail"


def alt_flags(bet):
    """True/False flags of the traders behind a bet (empty for older bets)."""
    hs = list((bet.get("holders") or {}).values()) + list(bet.get("wide") or [])
    return [h["alt"] for h in hs if isinstance(h, dict) and isinstance(h.get("alt"), bool)]


def strategy_of(path):
    if path in LEDGERS:
        return LEDGERS[path]
    name = path[len("ledger_"):-len(".json")].upper()
    return name[3:] if name.startswith("WX_") else name


def line(label, st):
    if not st["settled"] and not (st["open"] or st["resting"] or st["expired"]):
        return None
    lo, hi = st["roi"] - 1.645 * st["se"], st["roi"] + 1.645 * st["se"]
    rng = f" (90%: {lo:+.0%} to {hi:+.0%})" if st["settled"] >= 2 and st["se"] < 10 else ""
    extra = []
    if st["open"]:
        extra.append(f"{st['open']} open")
    if st["resting"]:
        extra.append(f"{st['resting']} resting")
    if st["expired"]:
        extra.append(f"{st['expired']} never filled")
    return (f"  {label:28} {st['won']:4}-{st['settled'] - st['won']:<4} P&L ${st['pnl']:+7.2f}  "
            f"ROI {st['roi']:+6.1%}{rng}" + (f"  [{', '.join(extra)}]" if extra else ""))


def main():
    ledgers = {}
    for p in sorted(glob.glob("ledger*.json")):
        try:
            with open(p) as fh:
                ledgers[strategy_of(p)] = json.load(fh)
        except (OSError, ValueError):
            print(f"{p}: can't be read")
    books = []
    for p in sorted(glob.glob("shadow_*.json")):
        books += shadow.Book(p).bets
    if not books:
        print("No shadow records yet.")
    out = []
    for strat in sorted(set(ledgers) | {b["strategy"] for b in books}):
        led = ledgers.get(strat)
        auto = (led or {}).get("auto") or {}
        state = ("AUTO-PAUSED since " + auto.get("since", "?")[:16] + f" ({auto.get('why')})"
                 if auto.get("paused") else "active")
        if any(b["strategy"] == strat and b["reason"] == "manual_pause" for b in books):
            state = "PAUSED BY HAND (shadow only, no new paper bets)" + (
                "" if state == "active" else f"; also {state}")
        out.append(f"\n{strat}: {state}")
        if led:
            r = line("real ledger", shadow.summarize(led["bets"]))
            if r:
                out.append(r)
        mine = [b for b in books if b["strategy"] == strat]
        groups = {}
        for b in mine:
            key = (f"limit bid at traders' entry" if b["kind"] == "limit" else
                   "unfilled resting bids" if b["kind"] == "unfilled" else
                   CHEAP_LABEL if b["reason"] == "cheap_band" else
                   WIDE_LABELS.get(b["reason"], f"skipped: {b['reason']}"))
            groups.setdefault(key, []).append(b)
        for key in sorted(groups):
            r = line(key, shadow.summarize(groups[key]))
            if r:
                out.append(r)
        wide = [b for key, g in groups.items() if key in WIDE_LABELS.values() for b in g]
        if wide:
            r = line(WIDE_ALL, shadow.summarize(wide))
            if r:
                out.append(r)
        real = (led or {}).get("bets", [])
        wide_recs = [b for b in mine if b["reason"] in WIDE_LABELS]
        yes = [b for b in real + wide_recs if any(alt_flags(b))]
        no = [b for b in real if alt_flags(b) and not any(alt_flags(b))]
        for label, bets in ((ALT_YES, yes), (ALT_NO, no)):
            r = line(label, shadow.summarize(bets)) if bets else None
            if r:
                out.append(r)
        lim = groups.get("limit bid at traders' entry", [])
        if lim:
            filled = sum(1 for b in lim if "fill" in b)
            done = sum(1 for b in lim if b["status"] != "resting")
            if done:
                out.append(f"  {'':28} limit fill rate {filled}/{done} = {filled / done:.0%}")
    print("Shadow evidence (never traded) next to each real paper ledger."
          "\nROI is per $1 bet; the 90% range shows how much is still luck.")
    print("\n".join(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
