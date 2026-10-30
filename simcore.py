"""Shared rules for the paper-trading simulator, used by signals.py and
weather.py so both settle, sell and validate ledgers the same way.

Settlement source (see docs.polymarket.com, Data API "GET /v2/tokens"):
each token record has `resolved` (bool) and `final_price` ("the settled
payout"): 1.0 winner, 0.0 loser, 0.5 for a 50/50 resolution. That is the
primary source. Gamma is only a fallback, and only when it explicitly says
`umaResolutionStatus == "resolved"` with prices of exactly 1/0 or 0.5/0.5.
A closed market without that status is NOT treated as final, however close
its prices are to 1 - it simply waits.
"""
import json
import math
import os

V2_TOKENS = "https://data-api.polymarket.com/v2/tokens"
GAMMA_MARKETS = "https://gamma-api.polymarket.com/markets"
VALID_PAYOUTS = (0.0, 0.5, 1.0)
BET_STATUSES = {"open", "resting", "won", "lost", "split", "stopped", "cancelled"}


def num(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def finite(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


# ---------- ledgers ----------

def validate_ledger(led, path="ledger"):
    """Raise SystemExit with a clear message if the ledger isn't sane."""
    def bad(msg):
        raise SystemExit(f"{path}: {msg}. Not trading on a broken ledger; fix or restore it.")
    if not isinstance(led, dict):
        bad("top level is not an object")
    if not finite(led.get("cash")):
        bad(f"cash is not a finite number ({led.get('cash')!r})")
    bets = led.get("bets")
    if not isinstance(bets, list):
        bad("bets is not a list")
    for i, b in enumerate(bets):
        if not isinstance(b, dict):
            bad(f"bet #{i} is not an object")
        if b.get("status") not in BET_STATUSES:
            bad(f"bet #{i} has unknown status {b.get('status')!r}")
        if not isinstance(b.get("asset"), str) or not b["asset"]:
            bad(f"bet #{i} has no asset id")
        for key in ("stake", "fee", "shares", "price"):
            if not finite(b.get(key)):
                bad(f"bet #{i} field {key} is not a finite number ({b.get(key)!r})")
            if b[key] < 0:
                bad(f"bet #{i} field {key} is negative")
        for key in ("pnl", "hold_pnl", "recovered", "orig_shares"):
            if key in b and not finite(b[key]):
                bad(f"bet #{i} field {key} is not a finite number ({b[key]!r})")
    return led


def load_ledger(path, start_cash):
    """Missing -> a fresh account. Present but unreadable or invalid -> stop."""
    if not os.path.exists(path):
        return {"cash": start_cash, "bets": []}
    try:
        with open(path) as fh:
            led = json.load(fh)        # note: Python accepts NaN/Infinity here,
    except (OSError, ValueError) as err:   # validate_ledger rejects them below
        raise SystemExit(f"{path} exists but can't be read ({err}). Not trading.")
    return validate_ledger(led, path)


def write_json_atomic(path, data, indent=None):
    text = json.dumps(data, indent=indent, allow_nan=False)   # never write NaN
    tmp = f"{path}.{os.getpid()}.tmp"                           # (fails before touching disk)
    with open(tmp, "w") as fh:
        fh.write(text)
    os.replace(tmp, path)


# ---------- selling ----------

def sell_fill(book, shares, fee_rate):
    """Walk the bids like a real sell order: (shares sold, cash after fees,
    average price). May sell fewer shares than asked."""
    bids = sorted(((num(b.get("price")), num(b.get("size")))
                   for b in (book or {}).get("bids", [])), reverse=True)
    left, sold, cash = shares, 0.0, 0.0
    for price, size in bids:
        if price <= 0 or size <= 0 or left <= 1e-9:
            continue
        take = min(size, left)
        cash += take * price - take * fee_rate * price * (1 - price)
        sold += take
        left -= take
    return sold, cash, (cash / sold if sold else 0.0)


# ---------- settlement ----------

def payout_from_v2(rec):
    """Settled payout per share from a /v2/tokens record, or None."""
    if not isinstance(rec, dict) or rec.get("resolved") is not True:
        return None
    p = rec.get("final_price")
    if not finite(p):
        return None
    for v in VALID_PAYOUTS:
        if abs(p - v) < 1e-6:
            return v
    return None      # unexpected payout value: don't guess


def payout_from_gamma(row, asset):
    """Fallback only: explicit 'resolved' status and exact 1/0 or 0.5/0.5."""
    if not isinstance(row, dict) or not row.get("closed"):
        return None
    if row.get("umaResolutionStatus") != "resolved":
        return None      # missing / proposed / disputed: not proof of finality
    try:
        ids = json.loads(row.get("clobTokenIds") or "[]")
        prices = [num(x, None) for x in json.loads(row.get("outcomePrices") or "[]")]
    except ValueError:
        return None
    if asset not in ids or len(prices) != len(ids) or None in prices:
        return None
    p = prices[ids.index(asset)]
    if sorted(prices) not in ([0.0, 1.0], [0.5, 0.5]):
        return None
    return p


def final_payouts(get, bets):
    """{asset: payout per share} for bets whose market is finally settled.
    `get(url, params)` is the caller's HTTP helper (returns parsed JSON or None)."""
    out = {}
    assets = sorted({b["asset"] for b in bets})
    for i in range(0, len(assets), 40):
        chunk = assets[i:i + 40]
        data = get(V2_TOKENS, {"token_id": ",".join(chunk)})
        if isinstance(data, dict):
            data = data.get("data")
        for rec in data or []:
            p = payout_from_v2(rec)
            if p is not None and rec.get("token_id") in chunk:
                out[rec["token_id"]] = p
    # fallback for anything the v2 lookup didn't settle (e.g. API hiccup)
    for b in bets:
        if b["asset"] in out:
            continue
        rows = get(GAMMA_MARKETS, {"condition_ids": b.get("condition"), "closed": "true"})
        p = payout_from_gamma(rows[0] if rows else None, b["asset"])
        if p is not None:
            out[b["asset"]] = p
    return out


def outcome_label(payout):
    return "won" if payout >= 0.99 else "lost" if payout <= 0.01 else "split"


# ---------- audit trail (what we knew when a bet was made) ----------

import subprocess                      # noqa: E402
from datetime import datetime, timezone  # noqa: E402

_CODE_VERSION = None


def code_version():
    """The commit actually checked out and running (`git rev-parse HEAD`).
    Falls back to GITHUB_SHA only if git isn't available."""
    global _CODE_VERSION
    if _CODE_VERSION is None:
        sha = ""
        try:
            sha = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True,
                                 text=True, timeout=5).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            sha = ""
        _CODE_VERSION = sha[:12] if sha else (os.environ.get("GITHUB_SHA", "")[:12] or "unknown")
    return _CODE_VERSION


def trigger_sha():
    """The commit that TRIGGERED the run (GITHUB_SHA). It can differ from the
    code that ran when the branch moved on while the run waited in the queue."""
    return os.environ.get("GITHUB_SHA", "")[:12] or None


def book_snapshot(book, levels=5):
    """Top `levels` of each side, best first, as [[price, size], ...]."""
    if not isinstance(book, dict):
        return None
    asks = sorted(([num(a.get("price")), num(a.get("size"))] for a in book.get("asks", [])),
                  key=lambda x: x[0])[:levels]
    bids = sorted(([num(b.get("price")), num(b.get("size"))] for b in book.get("bids", [])),
                  key=lambda x: -x[0])[:levels]
    snap = {"asks": asks, "bids": bids}
    for key in ("min_order_size", "tick_size", "timestamp", "hash"):
        if book.get(key) is not None:
            snap[key] = book.get(key)
    return snap


def audit(scan, settings, book=None, quote=None):
    """Everything needed to re-check a decision later. `quote` is used when
    there was no order book fetch (e.g. a resting bid placed from a quote)."""
    rec = {"code": code_version(), "scan": scan, "settings": dict(settings),
           "time": datetime.now(timezone.utc).isoformat()}
    trig = trigger_sha()
    if trig and trig != rec["code"]:
        rec["trigger_sha"] = trig          # only stored when it differs
    if book is not None:
        rec["book"] = book_snapshot(book)
    if quote is not None:
        rec["quote"] = quote
    return rec
