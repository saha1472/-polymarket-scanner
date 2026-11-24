"""How much to stake, for a small bankroll that must never reach zero.

Nothing in the bot calls this yet. `sizing_report.py` uses it to show what a rule would have done on past
bets. The rule, in plain words:

* A reserve is never spent (default $2), so the bankroll cannot reach zero by betting.
* Stakes are a share of what is left above the reserve, so they shrink as the bankroll shrinks.
* No stake at all unless the LOW end of the strategy's return range is above zero (no proven edge, no
  evidence-based stake). Money for learning is a separate fixed-size bucket (`tuition_stake`).
* Size = a fraction of Kelly (a quarter by default) on that low-end return, never above `max_frac` of what
  is spendable. If that is below the smallest order the market accepts, bet the smallest order only while
  it is at most `floor_frac` of what is spendable; otherwise skip.

Kelly in one line: buying a share for all-in cost c that pays 1 with chance p, the best long-run share of
the bankroll to stake is (p - c) / (1 - c). A strategy returning r per dollar at cost c has p = c * (1 + r).
"""
import math

RESERVE = 2.0          # dollars never spent
KELLY_MULT = 0.25      # quarter Kelly: a full Kelly stake is far too jumpy for an edge we can only estimate
MAX_FRAC = 0.05        # never more than this share of the spendable bankroll on one bet (before the minimum-order floor)
MIN_SHARES = 5         # the smallest limit order the exchange accepts
FLOOR_FRAC = 0.25      # a minimum-size order may use at most this share of the spendable bankroll
TUITION_BUDGET = 10.0  # dollars for experiments and execution tests, spent at the minimum order size
RANGE_Z = 1.645        # same middle-90% range as scorecard.py


def _finite(x):
    """A real, finite number (not None, not a bool, not NaN or infinity)."""
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def cost_per_share(price, fee_rate):
    """All-in cost of one share: price plus the taker fee (rate * price * (1 - price))."""
    return price + fee_rate * price * (1 - price)


def kelly_fraction(p, cost):
    """Best share of the bankroll to stake on a share that costs `cost` and pays 1 with chance `p`."""
    if not (_finite(p) and _finite(cost)) or not 0 < cost < 1 or p <= cost:
        return 0.0
    return (min(p, 1.0) - cost) / (1 - cost)


def edge_lower_bound(roi, se, z=RANGE_Z):
    """The return per dollar we are willing to believe: the low end of the range, never below zero.
    No usable error bar means no belief."""
    if not (_finite(roi) and _finite(se) and _finite(z)) or se < 0:
        return 0.0
    return max(roi - z * se, 0.0)


def min_order(price, fee_rate, min_shares=MIN_SHARES):
    """Dollars (fee included) for the smallest order."""
    return min_shares * cost_per_share(price, fee_rate)


def stake(bankroll, price, fee_rate, edge_lb, kelly_mult=KELLY_MULT, max_frac=MAX_FRAC, reserve=RESERVE,
          min_shares=MIN_SHARES, floor_frac=FLOOR_FRAC):
    """Dollars to spend (fee included) on a share at `price`, given the believed return per dollar `edge_lb`.
    Returns (dollars, reason). Never more than the bankroll above the reserve, whatever the settings."""
    if not all(_finite(x) for x in (bankroll, price, fee_rate, edge_lb, kelly_mult, max_frac, reserve, min_shares, floor_frac)):
        return 0.0, "bad input"
    if kelly_mult < 0 or reserve < 0 or min_shares <= 0 or not 0 <= max_frac <= 1 or not 0 <= floor_frac <= 1:
        return 0.0, "bad settings"
    spendable = bankroll - reserve
    if spendable <= 0:
        return 0.0, "only the reserve is left"
    cost = cost_per_share(price, fee_rate)
    if not 0 < cost < 1:
        return 0.0, "price out of range"
    if edge_lb <= 0:
        return 0.0, "no proven edge"
    p = min(cost * (1 + edge_lb), 1.0)
    share = min(kelly_mult * kelly_fraction(p, cost), max_frac)
    want = spendable * share
    smallest = min_shares * cost
    if want >= smallest:
        return want, "fraction of Kelly"
    if smallest <= floor_frac * spendable:
        return smallest, "minimum order (Kelly is below it)"
    return 0.0, "smallest order is too big for what is left"


def tuition_stake(spent, price, fee_rate, budget=TUITION_BUDGET, min_shares=MIN_SHARES, bankroll=None, reserve=RESERVE):
    """The smallest order for an experiment, only while the learning budget has room for it.
    `spent` is what the learning bucket has already used. If the learning money comes out of the same
    `bankroll`, the order must also leave the reserve untouched. Returns (dollars, reason)."""
    if not all(_finite(x) for x in (spent, price, fee_rate, budget, min_shares, reserve)) or (bankroll is not None and not _finite(bankroll)):
        return 0.0, "bad input"
    if min_shares <= 0 or reserve < 0:
        return 0.0, "bad settings"
    cost = cost_per_share(price, fee_rate)
    if not 0 < cost < 1:
        return 0.0, "price out of range"
    smallest = min_shares * cost
    if spent + smallest > budget:
        return 0.0, "learning budget used up"
    if bankroll is not None and smallest > bankroll - reserve:
        return 0.0, "only the reserve is left"
    return smallest, "learning bet at the minimum order"
