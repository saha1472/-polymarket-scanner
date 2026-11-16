"""Per-market trading fees, from each market's OWN fee schedule (pure: no network, no files).

Polymarket publishes the fee on every market (Gamma API: `feesEnabled`, `feeSchedule`). Live data,
16 Nov 2026: the rate differs even inside one category (sports markets say 0.03, not the 0.05 in the
docs' table), and many politics and geopolitics markets are fee-free (`feesEnabled` false). A single
constant per scan therefore over- or under-charges. The fee a taker pays is
    fee = shares * rate * p * (1 - p)            (only takers pay; makers pay nothing)
"""
import math


def _num(x, default=None):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    return v if math.isfinite(v) else default


def market_fee(market):
    """{"rate", "exp", "enabled"} from a Gamma market dict, or None when the market does not say
    (or uses a fee formula this code does not model). Never guesses."""
    if not isinstance(market, dict):
        return None
    if market.get("feesEnabled") is False:
        return {"rate": 0.0, "exp": 1.0, "enabled": False}
    schedule = market.get("feeSchedule")
    if not isinstance(schedule, dict):
        return None
    rate, exp = _num(schedule.get("rate")), _num(schedule.get("exponent"), 1.0)
    if rate is None or rate < 0 or exp != 1.0:
        return None
    return {"rate": rate, "exp": 1.0, "enabled": True}


def fee_amount(shares, price, rate):
    """USDC fee for taking `shares` at `price`."""
    return shares * rate * price * (1.0 - price)
