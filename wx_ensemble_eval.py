"""Score the ensemble shadow log (wx_shadow.jsonl) on official results.
READ-ONLY: it never writes anything and never trades.

For every logged snapshot whose market has settled:
  - accuracy: Brier score and log loss for the model (pooled, ECMWF, GEFS)
    and for the market (bid/ask midpoints, scaled to sum to 1). Lower is better.
  - calibration: when the model says ~X%, how often did it happen?
  - hypothetical P&L of "buy YES when model - ask >= edge", $1 per bet with
    the 0.05 weather fee. Uses the TOP-OF-BOOK ask from the snapshot only (no
    depth, and that seller may not have stayed), so treat it as optimistic.

Usage: python wx_ensemble_eval.py [wx_shadow.jsonl]
"""
import json
import math
import sys
from datetime import datetime, timedelta, timezone

import simcore

FEE_RATE = 0.05
EDGES = (0.04, 0.08, 0.12, 0.20)
MAX_ASK = 0.85
EPS = 0.01


def load(path):
    try:
        with open(path) as fh:
            lines = fh.read().splitlines()
    except OSError:
        return None
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def market_probs(buckets):
    """Midpoint per bucket, scaled to sum to 1. A missing bid counts as 0 and a
    missing ask as 1, so a lone offer on an empty book isn't read as a favourite."""
    raw = []
    for b in buckets:
        bid = b.get("bid") if b.get("bid") is not None else 0.0
        ask = b.get("ask") if b.get("ask") is not None else 1.0
        raw.append((bid + ask) / 2)
    total = sum(raw)
    return [x / total for x in raw] if total > 0 else [1 / len(raw)] * len(raw)


def brier(probs, winner, p_other=0.0):
    """Multi-bucket Brier score. `winner` is a bucket index, or None when a
    bucket outside the logged list won; p_other is the chance given to that."""
    listed = sum((p - (1.0 if i == winner else 0.0)) ** 2 for i, p in enumerate(probs))
    return listed + (p_other - (1.0 if winner is None else 0.0)) ** 2


def log_loss(probs, winner, p_unlisted=0.0):
    p = probs[winner] if winner is not None else p_unlisted
    return -math.log(min(max(p, EPS), 1 - EPS))


def winner_index(rec, payouts):
    """Index of the bucket that paid 1, None if an unlisted one won, or 'pending'."""
    pays = [payouts.get(b["yes"]) for b in rec["buckets"]]
    if 1.0 in pays:
        return pays.index(1.0)
    if all(p == 0.0 for p in pays):
        return None
    return "pending"


def hypo_bets(rec, winner, edge, max_ask=MAX_ASK, prob_key="p"):
    """[(stake, pnl)] for buying YES at the snapshot ask where model - ask >= edge."""
    out = []
    for i, b in enumerate(rec["buckets"]):
        ask = b.get("ask")
        if ask is None or not 0 < ask <= max_ask or b.get(prob_key, 0) - ask < edge:
            continue
        shares = 1.0 / ask
        fee = shares * FEE_RATE * ask * (1 - ask)
        out.append((1.0, (shares if i == winner else 0.0) - 1.0 - fee))
    return out


def evaluate(records, payouts):
    groups, calib, bets, pending = {}, {}, {}, 0
    for r in records:
        w = winner_index(r, payouts)
        if w == "pending":
            pending += 1
            continue
        key = f"{r['kind']}@{r['checkpoint']:02d}"
        g = groups.setdefault(key, {"n": 0, "model": [0, 0], "ecmwf": [0, 0],
                                    "gefs": [0, 0], "market": [0, 0], "fav_hit": [0, 0]})
        g["n"] += 1
        sets = {"model": [b["p"] for b in r["buckets"]],
                "ecmwf": [b.get("p_ecmwf", 0) for b in r["buckets"]],
                "gefs": [b.get("p_gefs", 0) for b in r["buckets"]],
                "market": market_probs(r["buckets"])}
        for name, probs in sets.items():
            other = r.get("p_unlisted", 0.0) if name == "model" else 0.0
            g[name][0] += brier(probs, w, other)
            g[name][1] += log_loss(probs, w, other)
        mfav = max(range(len(sets["model"])), key=lambda i: sets["model"][i])
        kfav = max(range(len(sets["market"])), key=lambda i: sets["market"][i])
        g["fav_hit"][0] += mfav == w
        g["fav_hit"][1] += kfav == w
        for i, b in enumerate(r["buckets"]):
            k = min(int(b["p"] * 10), 9)
            c = calib.setdefault(k, [0, 0.0, 0])
            c[0] += 1
            c[1] += b["p"]
            c[2] += i == w
        for edge in EDGES:
            bets.setdefault(edge, []).extend(hypo_bets(r, w, edge))
    return groups, calib, bets, pending


def report(groups, calib, bets, pending):
    lines = [f"Settled snapshots: {sum(g['n'] for g in groups.values())} "
             f"(+{pending} awaiting results)", "",
             "Accuracy (lower is better): Brier / log loss",
             f"{'checkpoint':12} {'n':>4} {'model':>14} {'ECMWF':>14} {'GEFS':>14} {'market':>14}"
             f" {'fav right: model / market':>27}"]
    for key in sorted(groups):
        g = groups[key]
        n = g["n"]
        cell = lambda s: f"{g[s][0] / n:.3f} / {g[s][1] / n:.2f}"   # noqa: E731
        lines.append(f"{key:12} {n:4} {cell('model'):>14} {cell('ecmwf'):>14} "
                     f"{cell('gefs'):>14} {cell('market'):>14} "
                     f"{g['fav_hit'][0] / n:>13.0%} / {g['fav_hit'][1] / n:.0%}")
    lines += ["", "Calibration (model probability vs how often it happened)"]
    for k in sorted(calib):
        n, psum, hits = calib[k]
        lines.append(f"  {k * 10:3d}-{k * 10 + 10:3d}%  n={n:5}  said {psum / n:6.1%}  "
                     f"happened {hits / n:6.1%}")
    lines += ["", f"Hypothetical: buy YES when model - ask >= edge (ask <= {MAX_ASK}, "
                  "$1 bets, top-of-book ask, fees included; optimistic)"]
    for edge in sorted(bets):
        b = bets[edge]
        pnl = sum(x[1] for x in b)
        wins = sum(1 for x in b if x[1] > 0)
        lines.append(f"  edge >= {edge:.2f}: {len(b):4} bets, {wins:4} won, P&L ${pnl:+.2f}"
                     + (f" (ROI {pnl / len(b):+.1%})" if b else ""))
    return "\n".join(lines)


def main(path="wx_shadow.jsonl", get=None):
    records = load(path)
    if not records:
        print(f"No snapshots in {path} yet.")
        return 2
    if get is None:
        import weather
        get = weather.get
    # only markets whose day is over (anything newer is simply pending)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat()
    tokens = {b["yes"]: {"asset": b["yes"], "condition": b["condition"]}
              for r in records if r["date"] < cutoff for b in r["buckets"]}
    payouts = simcore.final_payouts(get, list(tokens.values())) if tokens else {}
    print(report(*evaluate(records, payouts)))
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:2]))
