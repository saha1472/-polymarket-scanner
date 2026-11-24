"""READ-ONLY scorecard: is any paper strategy actually proven, one way or the other?

For every ledger*.json it prints the settled bets, the return per dollar put at
risk, its likely range, and a plain verdict. Four things make it stricter than
the daily heartbeat:

  1. Many strategies are being tried at once, so a few will look good by luck.
     The bar for "proven" is therefore raised for the number of strategies
     compared (a Bonferroni correction). It counts EVERY ledger, not only the ones
     with enough bets: any of them could have been the lucky one.
  2. Bets are not independent: several bets can sit on one event, one trader can
     drive many copies, and a day's bets share the weather or the news. The error
     bar is the widest of: every bet on its own, bets grouped by event, bets
     grouped by the day they were opened, and (copy ledgers) bets grouped by trader.
  3. The return is total profit over total dollars at risk, so a $5 bet counts
     five times a $1 bet. (It was an unweighted average of per-bet returns.)
  4. "Bets to detect" no longer assumes the true return equals what was seen (a
     lucky streak made that look easy). It is the total settled bets needed to tell
     a true +3% per dollar from zero, with 80% power, at the bar above.

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
TARGET_ROI = 0.03      # the true return per dollar we want to be able to detect
POWER_Z = 0.8416       # normal quantile for 80% power


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


def bets_to_detect(sd_per_bet, z_bar, effect=TARGET_ROI, power_z=POWER_Z):
    """Independent bets needed so that a TRUE return of `effect` per dollar would clear a bar of
    `z_bar` standard errors with the chosen power, when one bet's return has spread `sd_per_bet`."""
    if not math.isfinite(sd_per_bet) or sd_per_bet <= 0 or effect <= 0:
        return None
    return math.ceil(((z_bar + power_z) * sd_per_bet / effect) ** 2)


def design_effect(se, se_iid):
    """How much wider the error bar is than if every bet were independent, squared (never below 1)."""
    if not math.isfinite(se_iid) or se_iid <= 0 or not math.isfinite(se):
        return 1.0
    return max((se / se_iid) ** 2, 1.0)


def _cluster_se(bets, key, roi, total):
    """Standard error of the pooled return when bets that share `key` move together.
    A bet whose key is None is a group of its own. Fewer than two groups: no error bar (inf)."""
    groups = {}
    for i, b in enumerate(bets):
        k = key(b) if key is not None else None
        g = groups.setdefault(("own", i) if k is None else ("key", k), [0.0, 0.0])
        g[0] += b["pnl"]
        g[1] += b["cost"]
    count = len(groups)
    if count < 2 or total <= 0:
        return float("inf")
    return math.sqrt(count / (count - 1) * sum((p - roi * c) ** 2 for p, c in groups.values())) / total


def cluster_stats(bets, keys):
    """bets: dicts with pnl and cost (dollars at risk). keys: {name: bet -> group or None}.
    The return is pooled (total profit / total dollars at risk); the error bar is the widest of
    'every bet on its own' and each grouping in `keys`."""
    n = len(bets)
    if n == 0:
        return {"n": 0, "pnl": 0.0, "cost": 0.0, "roi": 0.0, "se_iid": float("inf"), "se": float("inf"), "se_by": {}}
    pnl, cost = sum(b["pnl"] for b in bets), sum(b["cost"] for b in bets)
    roi = pnl / cost
    se_iid = _cluster_se(bets, None, roi, cost)
    se_by = {name: _cluster_se(bets, fn, roi, cost) for name, fn in keys.items()}
    return {"n": n, "pnl": pnl, "cost": cost, "roi": roi, "se_iid": se_iid,
            "se": max([se_iid] + list(se_by.values())), "se_by": se_by}


def _first_trader(b):
    t = b.get("traders")
    if isinstance(t, (list, tuple)):
        return str(t[0]) if t else None
    return str(t) if t else None


KEYS = {
    "event": lambda b: b.get("event") or b.get("condition"),
    "day": lambda b: (b.get("opened") or "")[:10] or None,
    "trader": _first_trader,
}


def _settled(ledger):
    out = []
    for b in ledger.get("bets", []):
        if b.get("status") not in shadow.SETTLED or not simcore.finite(b.get("pnl")):
            continue
        cost = (b.get("stake") or 0) + (b.get("fee") or 0)
        if simcore.finite(cost) and cost > 0:
            out.append(dict(b, cost=cost))
    return out


def score(name, ledger, z):
    """One row for a ledger: counts, return per dollar, range and verdict."""
    st = cluster_stats(_settled(ledger), KEYS)
    n, mean, se = st["n"], st["roi"], st["se"]
    row = {"name": name, "n": n, "pnl": st["pnl"], "mean": mean, "se": se, "se_iid": st["se_iid"],
           "se_by": st["se_by"], "lo": None, "hi": None, "need": None}
    if n < MIN_BETS:
        row["verdict"] = f"too few bets (need {MIN_BETS})"
        return row
    if not math.isfinite(se):
        row["verdict"] = "too clustered to judge (all on one event or one day)"
        return row
    row["lo"], row["hi"] = mean - RANGE_Z * se, mean + RANGE_Z * se
    sd = st["se_iid"] * math.sqrt(n)
    need = bets_to_detect(sd, z)
    row["need"] = None if need is None else math.ceil(need * design_effect(se, st["se_iid"]))
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
    """Rows for {name: ledger}, worst first, plus the bar that was used. The bar counts every ledger
    (any of them could have been the lucky one), not only those with enough bets to be judged."""
    judged = sum(1 for led in ledgers.values() if len(_settled(led)) >= MIN_BETS)
    z = z_needed(len(ledgers))
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


NOTE = ("Per $ = total profit / total dollars at risk. Range = middle 90%, from the widest of: bets on their own, "
        "grouped by event, grouped by opening day, grouped by trader. Bets to detect = settled bets in total needed to tell "
        f"a true {TARGET_ROI:+.0%} per dollar from zero (80% power) at that bar. The ~45 shadow variants are exploratory "
        "and are never called proven here.")


def render(rows, z, judged, markdown=False):
    head = (f"Scorecard: {judged} of {len(rows)} strategies have {MIN_BETS}+ settled bets. 'Proven' needs a "
            f"result {z:.1f} standard errors from zero (the bar counts all {len(rows)} strategies).")
    cols = ("Ledger", "Settled", "P&L", "Per $", "Range", f"Bets to detect {TARGET_ROI:+.0%}", "Verdict")
    table = [(r["name"], str(r["n"]), f"${r['pnl']:+.2f}", _pct(r["mean"]) if r["n"] else "n/a",
              "n/a" if r["lo"] is None else f"{_pct(r['lo'])} to {_pct(r['hi'])}",
              _need(r), r["verdict"]) for r in rows]
    if markdown:
        lines = ["### Scorecard", "", head, "", "| " + " | ".join(cols) + " |",
                 "|" + "---|" * len(cols)]
        return "\n".join(lines + ["| " + " | ".join(t) + " |" for t in table] + ["", NOTE])
    width = [max(len(x[i]) for x in [cols] + table) for i in range(len(cols))]
    fmt = "  ".join(f"{{:<{w}}}" for w in width)
    return "\n".join([head, "", fmt.format(*cols)] + [fmt.format(*t) for t in table] + ["", NOTE])


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
