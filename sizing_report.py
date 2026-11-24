"""What different stake rules would have done on the bets settled so far, and how a $50 bankroll would fare.
A report only: it reads the ledgers and changes nothing.

For every ledger with enough settled bets it shows, starting from $50:
  * a replay of the real history in order of opening, with flat stakes ($1 as run, $2.50, $5) and with the
    rule in sizing.py, which only stakes once the low end of the ledger's own return range (using bets
    settled before that bet) is above zero;
  * the chance of getting stuck (too little left above the reserve to place another bet) within a number
    of future bets, by resampling whole days of past returns, under three assumptions about the true
    return: as observed, zero, and +3%.

Past returns scale with the stake here; real fills at larger sizes, the 5-share minimum order and the
book depth are not modelled in the first two sections; the last section does model them for the bets that
stored an order book. The replay settles each bet before the next one opens (cash is credited at
once); the rule's edge estimate only counts bets that had settled before a bet opened when the ledger records
when (`closed`), otherwise it counts every earlier bet. The "true return" scenarios shift every resampled
return by the same amount until the pooled average, after the -100% floor, equals the stated figure. Run:  python sizing_report.py [--start 50] [--paths 2000] [--bets 300] [--seed 1]
"""
import argparse
import glob
import os
import random
import statistics

import scorecard
import simcore
import sizing

FLAT_STAKES = (1.0, 2.5, 5.0)
MIN_BOOKS = 10         # fewer settled bets with a stored order book than this: no what-if table
MIN_DAYS = 5           # fewer days of history than this cannot be resampled in a way that means anything
SCENARIOS = (("as observed", None), ("true return 0%", 0.0), ("true return +3%", 0.03))


def history(ledger):
    """Settled bets in the order they opened: (opened, return per dollar at risk, bet)."""
    out = []
    for b in scorecard._settled(ledger):
        out.append((b.get("opened") or "", b["pnl"] / b["cost"], b))      # _settled keeps only finite profit and positive cost
    out.sort(key=lambda t: t[0])
    return out


def bet_fee_rate(b):
    """The fee rate a bet was charged: the recorded one, else worked back from the fee paid, else 0."""
    r = b.get("fee_rate")
    if simcore.finite(r):
        return r
    p, sh, fee = b.get("price"), b.get("shares"), b.get("fee")
    if all(simcore.finite(x) for x in (p, sh, fee)) and 0 < p < 1 and sh > 0:
        return fee / (sh * p * (1 - p))
    return 0.0


def replay(hist, start, decide, reserve=sizing.RESERVE):
    """Walk the history. decide(bankroll, i, hist) -> dollars to stake (0 skips). A stake that would take the
    bankroll below the reserve is skipped, not cut. Returns final, lowest and deepest drawdown of the bankroll and the bets placed."""
    bank, low, peak, dd, placed = start, start, start, 0.0, 0
    for i, (_, ret, _b) in enumerate(hist):
        s = decide(bank, i, hist)
        if not s > 0 or s > bank - reserve:
            continue
        placed += 1
        bank += s * ret
        low = min(low, bank)
        peak = max(peak, bank)
        dd = max(dd, (peak - bank) / peak)
    return {"final": bank, "low": low, "drawdown": dd, "placed": placed, "of": len(hist)}


def flat(dollars):
    return lambda bank, i, hist: dollars


def rule(bank, i, hist):
    """The sizing.py rule, walk-forward: the believed edge comes only from bets before this one that had
    already settled (by the ledger's `closed` time when it has one)."""
    opened = hist[i][0]
    before = [b for _, _, b in hist[:i] if not b.get("closed") or b["closed"] <= opened]
    if len(before) < scorecard.MIN_BETS:
        return 0.0
    st = scorecard.cluster_stats(before, scorecard.KEYS)
    lb = sizing.edge_lower_bound(st["roi"], st["se"])
    b = hist[i][2]
    p = b.get("price")
    if not simcore.finite(p):
        return 0.0
    return sizing.stake(bank, p, bet_fee_rate(b), lb)[0]


def usable_level(price, size):
    return simcore.finite(price) and simcore.finite(size) and 0 < price < 1 and size > 0


def levels(asks):
    """The usable (price, size) levels of a stored ask list, best first. Numbers stored as text are accepted;
    anything else is dropped."""
    out = []
    for lv in asks if isinstance(asks, list) else []:
        try:
            price, size = float(lv[0]), float(lv[1])
        except (TypeError, ValueError, IndexError, KeyError):
            continue
        if usable_level(price, size):
            out.append((price, size))
    return sorted(out)


def walk_asks(asks, dollars, fee_rate):
    """Spend `dollars` (fee on top) on stored asks [[price, size], ...] best first.
    Returns (shares, spent, fee), or None if the stored levels cannot absorb it."""
    left, shares, spent, fee = dollars, 0.0, 0.0, 0.0
    tol = 1e-9 * dollars
    for price, size in levels(asks):
        if left <= tol:
            break
        take = min(size, left / price)
        shares += take
        spent += take * price
        fee += take * fee_rate * price * (1 - price)
        left -= take * price
    return (shares, spent, fee) if left <= tol and shares > 0 else None


def has_book(bet):
    """A plain win or loss that stored the order book it saw (a recorded empty book counts: it fills nothing)."""
    audit = bet.get("audit")
    book = audit.get("book") if isinstance(audit, dict) else None
    return isinstance(book, dict) and isinstance(book.get("asks"), list) and bet.get("status") in ("won", "lost")


def whatif(bet, dollars):
    """What this settled bet would have returned had it spent `dollars` into the order book it saw.
    Returns {"cost", "pnl", "slip"} (slip = average price paid minus the best ask), or None when there is no
    stored book, the bet did not simply win or lose, or the stored levels cannot absorb the order."""
    if not has_book(bet):
        return None
    asks = bet["audit"]["book"]["asks"]
    got = walk_asks(asks, dollars, bet_fee_rate(bet))
    if got is None:
        return None
    shares, spent, fee = got
    best = levels(asks)[0][0]
    return {"cost": spent + fee, "pnl": (shares if bet["status"] == "won" else 0.0) - spent - fee, "slip": max(spent / shares - best, 0.0)}


def whatif_rows(ledger, sizes=FLAT_STAKES):
    """Per size: bets with a stored book, how many it could fill, how many fill at every size, and the pooled return
    per dollar and average slippage (dollars) over those bets that fill at every size, so sizes compare like with like."""
    bets = [b for _, _, b in history(ledger) if has_book(b)]
    fills = {d: [whatif(b, d) for b in bets] for d in sizes}
    same = [i for i in range(len(bets)) if all(fills[d][i] for d in sizes)]
    rows = []
    for d in sizes:
        got = [fills[d][i] for i in same]
        cost = sum(w["cost"] for w in got)
        rows.append({"size": d, "books": len(bets), "filled": sum(1 for w in fills[d] if w), "same": len(same),
                     "roi": sum(w["pnl"] for w in got) / cost if cost else None,
                     "slip": sum(w["slip"] for w in got) / len(got) if got else None})
    return rows


def day_blocks(hist):
    """Returns grouped by opening day, so a resample keeps each day's bets together."""
    days = {}
    for opened, ret, _ in hist:
        days.setdefault(opened[:10], []).append(ret)
    return list(days.values())


def centring_shift(returns, target):
    """The amount to add to every return so that the average after the -100% floor equals `target`
    (found by bisection; the floored average only rises as the shift rises). None keeps the returns as they are."""
    if target is None or not returns:
        return 0.0
    lo, hi = -(1.0 + max(returns)), 2.0 + abs(target)
    for _ in range(80):
        mid = (lo + hi) / 2
        if sum(max(r + mid, -1.0) for r in returns) / len(returns) < target:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def simulate(blocks, dollars, start, n_bets, paths, rng, target=None, reserve=sizing.RESERVE):
    """Flat stake `dollars` over `n_bets` future bets, resampling whole days. `target` is the assumed pooled return
    per dollar (None keeps the observed one). Returns the chance of getting stuck (not enough above the reserve for
    the next bet, checked before every bet and after the last), the median and 5th percentile of the final
    bankroll, and the chance of ending above `start`."""
    blocks = [b for b in blocks if b]
    if paths < 1 or n_bets < 0 or (n_bets > 0 and not blocks):
        raise ValueError("need at least one path, a non-negative number of bets and some returns to resample")
    shift = centring_shift([r for blk in blocks for r in blk], target)
    finals, stuck = [], 0
    for _ in range(paths):
        bank, n, hit = start, 0, False
        while n < n_bets and not hit:
            for r in rng.choice(blocks):
                if bank - reserve < dollars:
                    hit = True
                    break
                bank += dollars * max(r + shift, -1.0)
                n += 1
                if n >= n_bets:
                    break
        hit = hit or bank - reserve < dollars
        stuck += hit
        finals.append(bank)
    finals.sort()
    return {"stuck": stuck / paths, "median": statistics.median(finals), "p5": finals[int(0.05 * (len(finals) - 1))],
            "above": sum(1 for f in finals if f > start) / paths}


def stake_table(bankroll, price, fee_rate, edges=(0.03, 0.10, 0.30, 0.60)):
    """Rows (edge, quarter-Kelly stake, half-Kelly stake, smallest order), to show where the minimum order takes over."""
    rows = []
    for e in edges:
        rows.append((e, sizing.stake(bankroll, price, fee_rate, e)[0],
                     sizing.stake(bankroll, price, fee_rate, e, kelly_mult=0.5)[0], sizing.min_order(price, fee_rate)))
    return rows


def load(root="."):
    out = {}
    for path in sorted(glob.glob(os.path.join(root, "ledger*.json"))):
        name = os.path.basename(path)[len("ledger"):-len(".json")].lstrip("_") or "copy"
        out[name] = simcore.read_json(path, {})
    return out


def render(ledgers, start=50.0, paths=2000, n_bets=300, seed=1):
    lines = [f"STAKE RULES ON PAST BETS, starting from ${start:g} (reserve ${sizing.RESERVE:g} never spent)", ""]
    rng = random.Random(seed)
    shown = 0
    for name, led in ledgers.items():
        hist = history(led)
        if len(hist) < scorecard.MIN_BETS:
            continue
        shown += 1
        blocks = day_blocks(hist)
        lines.append(f"{name}: {len(hist)} settled bets on {len(blocks)} days")
        lines.append(f"  {'replay of the real history':30}{'final':>9}{'lowest':>9}{'worst dip':>11}{'bets placed':>13}")
        for label, fn in [(f"flat ${d:g}", flat(d)) for d in FLAT_STAKES] + [("rule (sizing.py)", rule)]:
            r = replay(hist, start, fn)
            lines.append(f"  {label:30}{r['final']:>9.2f}{r['low']:>9.2f}{r['drawdown']:>10.0%}{r['placed']:>8} of {r['of']}")
        if len(blocks) < MIN_DAYS:
            lines.append(f"  next {n_bets} bets: only {len(blocks)} day(s) of history, too few to resample")
            lines.append("")
            continue
        lines.append(f"  {'next ' + str(n_bets) + ' bets (resampled days)':30}{'stuck':>9}{'median':>9}{'5th pct':>9}{'ends up':>9}")
        for scen, target in SCENARIOS:
            for d in FLAT_STAKES:
                s = simulate(blocks, d, start, n_bets, paths, rng, target)
                lines.append(f"  {scen + ', $' + format(d, 'g'):30}{s['stuck']:>9.0%}{s['median']:>9.2f}{s['p5']:>9.2f}{s['above']:>9.0%}")
        lines.append("")
    if not shown:
        lines.append(f"No ledger has {scorecard.MIN_BETS}+ settled bets yet.")
        lines.append("")
    lines.append("Estimated fills of bigger orders, from the top 5 asks each won or lost bet stored (spend before fees).")
    lines.append("'filled' = the stored levels could absorb the whole order; 'all sizes' = filled at every size shown.")
    lines.append("Return per dollar (after fees) and slip (cents per share above the best ask) cover the 'all sizes' bets only:")
    lines.append(f"  {'ledger':16}{'spend':>7}{'books':>7}{'filled':>8}{'all sizes':>11}{'return per $':>14}{'slip (cents)':>14}")
    shown_w = 0
    for name, led in ledgers.items():
        rows = whatif_rows(led)
        if rows[0]["books"] < MIN_BOOKS:
            continue
        shown_w += 1
        for r in rows:
            roi = "n/a" if r["roi"] is None else f"{r['roi']:+.1%}"
            slip = "n/a" if r["slip"] is None else f"{100 * r['slip']:.2f}"
            lines.append(f"  {name if r is rows[0] else '':16}{'$' + format(r['size'], 'g'):>7}{r['books']:>7}{r['filled']:>8}{r['same']:>11}{roi:>14}{slip:>14}")
    if not shown_w:
        lines.append(f"  No ledger has {MIN_BOOKS}+ settled bets with a stored order book yet.")
    lines.append("")
    lines.append("What the rule would stake (spendable bankroll after the reserve; price 0.70, fee 1%):")
    lines.append(f"  {'bankroll':>9}{'believed edge':>15}{'quarter Kelly':>15}{'half Kelly':>12}{'smallest order':>16}")
    for bank in (48.0, 30.0, 200.0):
        for e, q, h, m in stake_table(bank, 0.70, 0.01):
            lines.append(f"  {bank:>9.0f}{e:>15.0%}{q:>15.2f}{h:>12.2f}{m:>16.2f}")
    lines.append("")
    lines.append("Reading it: 'stuck' means too little above the reserve to place the next bet; the reserve itself is never spent.")
    lines.append("At about $50 the cap (5% of what is spendable) keeps a Kelly stake below the smallest order whatever the edge,")
    lines.append("so the rule here means: no proven edge -> no evidence stake; proven edge -> the smallest order.")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--start", type=float, default=50.0)
    ap.add_argument("--paths", type=int, default=2000)
    ap.add_argument("--bets", type=int, default=300)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--root", default=".")
    a = ap.parse_args(argv)
    if a.paths < 1 or a.bets < 0 or a.start <= 0:
        ap.error("--paths must be at least 1, --bets at least 0 and --start above 0")
    print(render(load(a.root), a.start, a.paths, a.bets, a.seed))


if __name__ == "__main__":
    main()
