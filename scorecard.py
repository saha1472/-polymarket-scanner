"""READ-ONLY scorecard: is any paper strategy actually proven, one way or the other?

For every ledger*.json it prints the settled bets, the average return per bet,
its likely range, and a plain verdict. Two things make it stricter than the
daily heartbeat:

  1. Many strategies are being tried at once, so a few will look good by luck.
     The bar for "proven" is therefore raised for the number of strategies
     compared (a Bonferroni correction): with 10 strategies a result has to be
     about 2.8 standard errors from zero, not the usual 2.
  2. For a strategy that is not proven it says roughly how many settled bets it
     would take to prove it, IF its true return equals what has been seen so
     far. Hundreds or thousands means "this will not be settled by waiting".

It never writes a file and never touches the network.
Usage:  python scorecard.py                 (all ledger*.json)
        python scorecard.py ledger.json ledger_sports.json
        python scorecard.py --markdown      (a table, for the GitHub run summary)
"""
import glob
import math
import os
import sys
from statistics import NormalDist

import shadow
import simcore

MIN_BETS = 20          # fewer settled bets than this: no verdict at all
ALPHA = 0.05           # overall chance of calling luck "proven", across all strategies
RANGE_Z = 1.645        # the range shown is the middle 90%


def z_needed(strategies):
    """Standard errors from zero needed to call a result proven, when
    `strategies` are being compared at once (two-sided, Bonferroni)."""
    k = max(int(strategies), 1)
    return NormalDist().inv_cdf(1 - ALPHA / (2 * k))


def bets_to_prove(n, mean, se, z):
    """Settled bets needed for a return of `mean` per bet to clear the bar,
    assuming the spread of results stays as seen. None if it can't be worked out."""
    if n < 2 or not math.isfinite(se) or mean == 0:
        return None
    sd = se * math.sqrt(n)
    return math.ceil((z * sd / abs(mean)) ** 2)


def score(name, ledger, z):
    """One row for a ledger: counts, return per bet, range and verdict."""
    bets = ledger.get("bets", [])
    n, mean, se = shadow.roi_stats(bets)
    pnl = sum(b["pnl"] for b in bets if b.get("status") in shadow.SETTLED
              and simcore.finite(b.get("pnl")))
    row = {"name": name, "n": n, "pnl": pnl, "mean": mean, "se": se,
           "lo": None, "hi": None, "need": None}
    if n < MIN_BETS or not math.isfinite(se):
        row["verdict"] = f"too few bets (need {MIN_BETS})"
        return row
    row["lo"], row["hi"] = mean - RANGE_Z * se, mean + RANGE_Z * se
    row["need"] = bets_to_prove(n, mean, se, z)
    if se == 0:
        row["verdict"] = "every bet returned the same: nothing to judge"
    elif mean / se <= -z:
        row["verdict"] = "PROVEN LOSER"
    elif mean / se >= z:
        row["verdict"] = "PROVEN WINNER"
    elif row["hi"] < 0:
        row["verdict"] = "probably losing"
    elif row["lo"] > 0:
        row["verdict"] = "promising, not proven"
    else:
        row["verdict"] = "no evidence either way"
    return row


def build(ledgers):
    """Rows for {name: ledger}, worst first, plus the bar that was used."""
    judged = sum(1 for led in ledgers.values()
                 if shadow.roi_stats(led.get("bets", []))[0] >= MIN_BETS)
    z = z_needed(judged)
    rows = [score(name, led, z) for name, led in ledgers.items()]
    return sorted(rows, key=lambda r: (r["n"] < MIN_BETS, r["mean"])), z, judged


def _pct(x):
    return "n/a" if x is None else f"{x:+.0%}"


def _need(row):
    if row["need"] is None:
        return "n/a"
    if row["verdict"].startswith("PROVEN"):
        return "done"
    return f"{row['need']:,}" if row["need"] < 100000 else "100,000+"


def render(rows, z, judged, markdown=False):
    head = (f"Scorecard: {judged} strategies with {MIN_BETS}+ settled bets, so 'proven' needs a "
            f"result {z:.1f} standard errors from zero. Range = middle 90%.")
    cols = ("Ledger", "Settled", "P&L", "Per bet", "Range", "Bets to prove", "Verdict")
    table = [(r["name"], str(r["n"]), f"${r['pnl']:+.2f}", _pct(r["mean"]) if r["n"] else "n/a",
              "n/a" if r["lo"] is None else f"{_pct(r['lo'])} to {_pct(r['hi'])}",
              _need(r), r["verdict"]) for r in rows]
    if markdown:
        lines = ["### Scorecard", "", head, "", "| " + " | ".join(cols) + " |",
                 "|" + "---|" * len(cols)]
        return "\n".join(lines + ["| " + " | ".join(t) + " |" for t in table])
    width = [max(len(x[i]) for x in [cols] + table) for i in range(len(cols))]
    fmt = "  ".join(f"{{:<{w}}}" for w in width)
    return "\n".join([head, "", fmt.format(*cols)] + [fmt.format(*t) for t in table])


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    markdown = "--markdown" in args
    paths = [a for a in args if not a.startswith("--")] or sorted(glob.glob("ledger*.json"))
    ledgers = {}
    for path in paths:
        led = simcore.read_json(path, None)
        if isinstance(led, dict) and isinstance(led.get("bets"), list):
            name = os.path.basename(path).replace(".json", "")
            ledgers["general" if name == "ledger" else name.replace("ledger_", "")] = led
        else:
            print(f"(skipped {path}: not a readable ledger)", file=sys.stderr)
    if not ledgers:
        print("No ledgers found.")
        return 0
    print(render(*build(ledgers), markdown=markdown))
    return 0


if __name__ == "__main__":
    sys.exit(main())
