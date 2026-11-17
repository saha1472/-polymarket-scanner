"""AUTO-CALIBRATION for the weather ensemble model. SHADOW ONLY: it never trades
and never touches a ledger. It reads wx_shadow.jsonl (written by wx_ensemble.py)
and the official results, and decides - once a day - how much to trust the model.

Why: scored on settled results, the raw ensemble probabilities were worse than the
market's (overconfident). A blend of "model" and "market" can still help, but only
if it holds up on days it was NOT fitted on. This script checks that every day.

How it works
  1. Settled results: for every event past its settlement lag, look up the official
     payout of each bucket (same /v2/tokens source as simcore). The winner of each
     event is cached in wx_winners.json, so only newly finished events hit the API.
  2. Per group (highs@10, highs@15, lows@8) fit a small logistic blend
         logit(p) = bias + a * logit(model) + b * logit(market)
     pulled towards "just the market" (bias 0, a 0, b 1) so thin data can't swing it.
  3. Walk-forward test: fit on the older days, score on the last TEST_DAYS days.
  4. Trust gate. A group is TRUSTED only after passing ALL of these on the test days,
     STREAK_REQUIRED days in a row:
       - enough distinct events and days;
       - beats the market's log loss by MARGIN on buckets with a REAL two-sided
         book (bid and ask both present, spread <= TIGHT_SPREAD). A missing bid or ask
         makes the market's midpoint unreliable, so those rows can't earn trust;
       - hypothetical buys at the ACTUAL ask (blend - ask >= EDGE, fees in) made at
         least zero profit, over at least MIN_BET_EVENTS distinct events.
  5. Fail closed: if the file is missing, stale, unreadable or the group is not
     trusted, weights_for() returns the identity (= market price, model ignored).

Files: wx_calibration.json (result), wx_winners.json (cache of settled events).
Usage: python wx_calibrate.py [--force]
"""
import json
import math
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import simcore

SHADOW_LOG = "wx_shadow.jsonl"
CAL_FILE = "wx_calibration.json"
WINNERS_FILE = "wx_winners.json"

IDENTITY = (0.0, 0.0, 1.0)          # bias, model weight, market weight: "just the market"
PRIOR_STRENGTH = 25.0               # pseudo-observations pulling towards IDENTITY
EPS = 0.005                         # clip for logits
TEST_DAYS = 5                       # most recent settled days used as the out-of-sample test
MIN_TRAIN_ROWS = 800
MIN_TRAIN_DAYS = 6
MIN_TEST_EVENTS = 60                # distinct events among two-sided test rows
MIN_TEST_DAYS = 4
MARGIN = 0.01                       # log-loss the blend must beat the market by
TIGHT_SPREAD = 0.10                 # "real two-sided book"
EDGE = 0.08                         # hypothetical buy: blend - ask >= EDGE
MAX_ASK = 0.85
FEE_RATE = 0.05
MIN_BET_EVENTS = 30
STREAK_REQUIRED = 3                 # consecutive daily passes before a group is trusted
STALE_DAYS = 3                      # a calibration older than this is ignored
SETTLE_LAG_DAYS = 2                 # only events at least this many days old are scored
FETCH_BUDGET_S = 150                # wall-clock budget for result lookups per run
MAX_FETCH_CALLS = 150               # API calls per run; a big backlog is worked off over a few runs
TOKENS_PER_CALL = 40


# ---------- pure helpers (tested) ----------

def clip(p, eps=EPS):
    return min(max(p, eps), 1 - eps)


def logit(p):
    p = clip(p)
    return math.log(p / (1 - p))


def sigmoid(z):
    z = max(min(z, 30.0), -30.0)
    return 1.0 / (1.0 + math.exp(-z))


def market_probs(buckets):
    """Midpoint per bucket scaled to sum to 1. A missing bid counts as 0 and a
    missing ask as 1 (same convention as wx_ensemble_eval)."""
    raw = []
    for b in buckets:
        bid = b.get("bid") if b.get("bid") is not None else 0.0
        ask = b.get("ask") if b.get("ask") is not None else 1.0
        raw.append((bid + ask) / 2)
    total = sum(raw)
    return [x / total for x in raw] if total > 0 else [1 / len(raw)] * len(raw)


def two_sided(bid, ask):
    """A real, tight two-sided book."""
    return (bid is not None and ask is not None and 0 < bid < ask < 1
            and ask - bid <= TIGHT_SPREAD + 1e-9)


def blend_prob(w, p_model, p_market):
    return sigmoid(w[0] + w[1] * logit(p_model) + w[2] * logit(p_market))


def log_loss(p, y):
    p = min(max(p, 1e-4), 1 - 1e-4)
    return -(y * math.log(p) + (1 - y) * math.log(1 - p))


def hypo_pnl(ask, won, fee_rate=FEE_RATE):
    """P&L of a $1 YES buy at `ask`, fee included."""
    shares = 1.0 / ask
    fee = shares * fee_rate * ask * (1 - ask)
    return (shares if won else 0.0) - 1.0 - fee


def solve3(a, b):
    """Solve the 3x3 system a.x = b (Gaussian elimination, partial pivoting)."""
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for c in range(3):
        piv = max(range(c, 3), key=lambda r: abs(m[r][c]))
        m[c], m[piv] = m[piv], m[c]
        if abs(m[c][c]) < 1e-12:
            raise ValueError("singular system")
        for r in range(c + 1, 3):
            f = m[r][c] / m[c][c]
            for k in range(c, 4):
                m[r][k] -= f * m[c][k]
    x = [0.0] * 3
    for r in (2, 1, 0):
        x[r] = (m[r][3] - sum(m[r][k] * x[k] for k in range(r + 1, 3))) / m[r][r]
    return x


def fit_blend(rows, strength=PRIOR_STRENGTH, iters=40):
    """Logistic blend weights [bias, model, market], pulled towards IDENTITY.
    rows: dicts with pm (model prob), pk (market prob), y (0/1)."""
    xs = [(1.0, logit(r["pm"]), logit(r["pk"])) for r in rows]
    ys = [float(r["y"]) for r in rows]
    n = len(xs)
    if n == 0:
        return list(IDENTITY)
    prior = list(IDENTITY)
    w = list(prior)

    def objective(v):
        tot = sum(log_loss(sigmoid(sum(v[j] * x[j] for j in range(3))), y)
                  for x, y in zip(xs, ys))
        return tot + 0.5 * strength * sum((v[j] - prior[j]) ** 2 for j in range(3))

    cur = objective(w)
    for _ in range(iters):
        g = [strength * (w[j] - prior[j]) for j in range(3)]
        h = [[strength if i == j else 0.0 for j in range(3)] for i in range(3)]
        for x, y in zip(xs, ys):
            p = sigmoid(sum(w[j] * x[j] for j in range(3)))
            e, s = p - y, p * (1 - p)
            for i in range(3):
                g[i] += e * x[i]
                for j in range(3):
                    h[i][j] += s * x[i] * x[j]
        try:
            step = solve3(h, g)
        except ValueError:
            break
        t, new = 1.0, None
        while t > 1e-4:                      # damped step: never get worse
            cand = [w[j] - t * step[j] for j in range(3)]
            val = objective(cand)
            if val <= cur + 1e-12:
                new, cur = cand, val
                break
            t /= 2
        if new is None:
            break
        moved = max(abs(new[j] - w[j]) for j in range(3))
        w = new
        if moved < 1e-7:
            break
    return w


def event_winner(tokens, payouts):
    """('settled', winning token or None) once EVERY bucket token has a final
    payout (None = a bucket outside the list won); otherwise None (not final yet)."""
    if not tokens or any(t not in payouts for t in tokens):
        return None
    wins = [t for t in tokens if payouts[t] >= 0.99]
    if len(wins) == 1:
        return ("settled", wins[0])
    if not wins and all(payouts[t] <= 0.01 for t in tokens):
        return ("settled", None)
    return None                               # split / odd payouts: don't guess


def event_tokens(records):
    """{slug: (date, [yes tokens])} from the shadow log."""
    out = {}
    for r in records:
        slug, date = r.get("slug"), r.get("date")
        if not slug or not date:
            continue
        toks = [b.get("yes") for b in r.get("buckets", []) if b.get("yes")]
        if toks:
            cur = out.setdefault(slug, (date, []))
            for t in toks:
                if t not in cur[1]:
                    cur[1].append(t)
    return out


def due_events(records, winners, today):
    """[(slug, tokens)] past the settlement lag and not cached yet, oldest first."""
    cutoff = (today - timedelta(days=SETTLE_LAG_DAYS)).isoformat()
    ev = event_tokens(records)
    return [(s, toks) for s, (d, toks) in sorted(ev.items(), key=lambda kv: kv[1][0])
            if d <= cutoff and s not in winners]


def fetch_winners(records, winners, get, today, max_calls=MAX_FETCH_CALLS,
                  budget_s=FETCH_BUDGET_S, clock=time.monotonic, stats=None):
    """Fill `winners` ({slug: winning token or None}) for newly settled events.
    `get(url, params)` returns parsed JSON or None. Stops after `max_calls` calls or
    `budget_s` seconds (an API outage must never eat the whole run); the rest waits for
    the next run and stats["stopped_early"] says so. Returns the number of calls."""
    pending = due_events(records, winners, today)
    tokens = list(dict.fromkeys(t for _, toks in pending for t in toks))
    payouts, calls, t0 = {}, 0, clock()
    for i in range(0, len(tokens), TOKENS_PER_CALL):
        if calls >= max_calls or clock() - t0 >= budget_s:
            if stats is not None:
                stats["stopped_early"] = True
            break
        chunk = tokens[i:i + TOKENS_PER_CALL]
        data = get(simcore.V2_TOKENS, {"token_id": ",".join(chunk)})
        calls += 1
        if isinstance(data, dict):
            data = data.get("data")
        for rec in data or []:
            p = simcore.payout_from_v2(rec)
            if p is not None and rec.get("token_id") in chunk:
                payouts[rec["token_id"]] = p
    for slug, toks in pending:
        res = event_winner(toks, payouts)
        if res is not None:
            winners[slug] = res[1]
    return calls


def build_rows(records, winners):
    """One row per bucket per snapshot, for settled events only."""
    rows = []
    for r in records:
        slug = r.get("slug")
        if slug not in winners:
            continue
        bks = r.get("buckets") or []
        if not bks:
            continue
        win = winners[slug]
        pk = market_probs(bks)
        g = f"{r['kind']}@{r['checkpoint']}"
        for i, b in enumerate(bks):
            rows.append({"g": g, "date": r["date"], "slug": slug,
                         "y": 1 if (win is not None and b.get("yes") == win) else 0,
                         "pm": float(b.get("p", 0.0)), "pk": pk[i],
                         "ask": b.get("ask"), "bid": b.get("bid"),
                         "two": two_sided(b.get("bid"), b.get("ask"))})
    return rows


def split_days(rows):
    """(train, test) with the last TEST_DAYS days as test; None if too little history."""
    days = sorted({r["date"] for r in rows})
    if len(days) < MIN_TRAIN_DAYS + TEST_DAYS:
        return None
    cut = days[-TEST_DAYS]
    return ([r for r in rows if r["date"] < cut], [r for r in rows if r["date"] >= cut])


def roi_with_se(pnls_by_event):
    """(bets, events, pnl, roi per bet, standard error) grouping bets by event."""
    n = sum(len(v) for v in pnls_by_event.values())
    if not n:
        return 0, 0, 0.0, 0.0, 0.0
    total = sum(sum(v) for v in pnls_by_event.values())
    roi = total / n
    ss = sum((sum(v) - roi * len(v)) ** 2 for v in pnls_by_event.values())
    return n, len(pnls_by_event), total, roi, math.sqrt(ss) / n


def evaluate_group(rows):
    """Walk-forward evaluation of one group. Returns a dict with the test metrics,
    the pass/fail reasons and the weights fitted on ALL rows (used only if trusted)."""
    sp = split_days(rows)
    if sp is None:
        return {"ok": False, "reasons": ["not enough settled days yet"], "rows": len(rows)}
    train, test = sp
    out = {"rows": len(rows), "train_rows": len(train),
           "train_days": len({r["date"] for r in train})}
    w_tr = fit_blend(train) if len(train) >= MIN_TRAIN_ROWS else list(IDENTITY)
    two = [r for r in test if r["two"]]
    ev_two = {r["slug"] for r in two}
    out["test_two_sided_rows"] = len(two)
    out["test_events"] = len(ev_two)
    out["test_days"] = len({r["date"] for r in two})
    if two:
        out["ll_market"] = sum(log_loss(r["pk"], r["y"]) for r in two) / len(two)
        out["ll_model"] = sum(log_loss(clip(r["pm"]), r["y"]) for r in two) / len(two)
        out["ll_blend"] = sum(log_loss(blend_prob(w_tr, r["pm"], r["pk"]), r["y"])
                              for r in two) / len(two)
    by_event = {}
    for r in test:
        a = r["ask"]
        if a is None or not 0 < a <= MAX_ASK:
            continue
        if blend_prob(w_tr, r["pm"], r["pk"]) - a >= EDGE:
            by_event.setdefault(r["slug"], []).append(hypo_pnl(a, r["y"]))
    n, ev, pnl, roi, se = roi_with_se(by_event)
    out["bets"] = {"n": n, "events": ev, "pnl": round(pnl, 4), "roi": round(roi, 4),
                   "se": round(se, 4)}
    reasons = []
    if out["train_rows"] < MIN_TRAIN_ROWS or out["train_days"] < MIN_TRAIN_DAYS:
        reasons.append("not enough training data")
    if out["test_events"] < MIN_TEST_EVENTS or out["test_days"] < MIN_TEST_DAYS:
        reasons.append("not enough two-sided test events/days")
    if "ll_blend" not in out or out["ll_market"] - out["ll_blend"] < MARGIN:
        reasons.append(f"blend does not beat the market by {MARGIN} on real two-sided books")
    if ev < MIN_BET_EVENTS:
        reasons.append(f"only {ev} events with a qualifying buy (need {MIN_BET_EVENTS})")
    elif roi < 0:
        reasons.append("hypothetical buys at the real ask lost money")
    out["ok"] = not reasons
    out["reasons"] = reasons
    out["weights"] = [round(x, 4) for x in (fit_blend(rows) if out["ok"] else IDENTITY)]
    out["weights_train"] = [round(x, 4) for x in w_tr]
    return out


def update_state(prev, result, today_iso):
    """Streak bookkeeping for one group. `prev` is the previous entry (or None).
    The streak moves once per DAY: a second run on the same day recomputes today's
    result from the same starting point instead of counting twice."""
    prev = prev or {}
    base = prev.get("base_streak", 0) if prev.get("evaluated_on") == today_iso \
        else prev.get("streak", 0)
    streak = base + 1 if result.get("ok") else 0
    entry = dict(result, streak=streak, base_streak=base, trusted=streak >= STREAK_REQUIRED,
                 evaluated_on=today_iso)
    if not entry["trusted"]:
        entry["weights"] = list(IDENTITY)       # never expose weights of an untrusted group
    return entry


def weights_for(cal, kind, checkpoint, now=None):
    """[bias, model, market] to use. Fails closed to IDENTITY (= the market)."""
    try:
        now = now or datetime.now(timezone.utc)
        fitted = datetime.fromisoformat(cal["fitted_on"]).date()
        if (now.date() - fitted).days > STALE_DAYS:
            return list(IDENTITY)
        g = cal["groups"][f"{kind}@{checkpoint}"]
        if g.get("trusted") is not True:
            return list(IDENTITY)
        w = [float(x) for x in g["weights"]]
        if len(w) != 3 or not all(math.isfinite(x) for x in w):
            return list(IDENTITY)
        return w
    except (KeyError, TypeError, ValueError, AttributeError):
        return list(IDENTITY)


def calibrated_probs(cal, kind, checkpoint, buckets, p_model, now=None):
    """Calibrated probability per bucket (market-only when the group isn't trusted)."""
    w = weights_for(cal, kind, checkpoint, now)
    pk = market_probs(buckets)
    return [blend_prob(w, pm, k) for pm, k in zip(p_model, pk)]


def render_report(cal):
    lines = [f"Weather calibration fitted on {cal['fitted_on']} "
             f"({cal.get('events', 0)} settled events, code {cal.get('code', '?')})"]
    for name, g in sorted(cal["groups"].items()):
        head = f"  {name:12} {'TRUSTED' if g.get('trusted') else 'not trusted'} " \
               f"(pass streak {g.get('streak', 0)}/{STREAK_REQUIRED})"
        lines.append(head)
        if "ll_blend" in g:
            lines.append(f"      two-sided test: log loss market {g['ll_market']:.4f}, "
                         f"model {g['ll_model']:.4f}, blend {g['ll_blend']:.4f} "
                         f"({g['test_events']} events, {g['test_days']} days)")
            b = g["bets"]
            lines.append(f"      buys at real ask: {b['n']} bets / {b['events']} events, "
                         f"ROI {b['roi']:+.1%} (+-{b['se']:.1%}), P&L ${b['pnl']:+.2f}")
        for why in g.get("reasons", []):
            lines.append(f"      - {why}")
    return "\n".join(lines)


# ---------- network and files ----------

def http_get_json(url, params=None, timeout=15, attempts=2):
    full = url + ("?" + urllib.parse.urlencode(params) if params else "")
    for attempt in range(attempts):
        try:
            req = urllib.request.Request(full, headers={"User-Agent": "wx-calibrate"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read())
        except Exception as err:                       # network, HTTP error, bad JSON
            print(f"request failed ({err}); attempt {attempt + 1}")
            time.sleep(1 + attempt)
    return None


def read_json(path, default):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def load_records(path):
    """Every snapshot in the shadow log (the original file plus the monthly ones)."""
    return simcore.read_log(path)


def main(get=http_get_json, today=None, force=False):
    today = today or datetime.now(timezone.utc).date()
    cal = read_json(CAL_FILE, {})
    if not force and cal.get("fitted_on") == today.isoformat() and not cal.get("backlog"):
        print(f"Weather calibration: already fitted today ({today}).")
        return 0
    records = load_records(SHADOW_LOG)
    if not records:
        print("Weather calibration: no shadow snapshots yet.")
        return 0
    winners = read_json(WINNERS_FILE, {})
    before = len(winners)
    stats = {}
    calls = fetch_winners(records, winners, get, today, stats=stats)
    if len(winners) != before:
        simcore.write_json_atomic(WINNERS_FILE, winners)
    rows = build_rows(records, winners)
    groups, old = {}, cal.get("groups", {})
    for name in sorted({r["g"] for r in rows}):
        groups[name] = update_state(old.get(name), evaluate_group([r for r in rows if r["g"] == name]),
                                    today.isoformat())
    new = {"version": 1, "fitted_on": today.isoformat(), "code": simcore.code_version(),
           "events": len(winners), "groups": groups,
           "backlog": bool(stats.get("stopped_early")),   # more results to fetch on the next run
           "settings": {"test_days": TEST_DAYS, "margin": MARGIN, "edge": EDGE,
                        "streak_required": STREAK_REQUIRED, "tight_spread": TIGHT_SPREAD}}
    simcore.write_json_atomic(CAL_FILE, new, indent=1)
    print(f"Weather calibration: {len(winners) - before} new settled event(s), {calls} API call(s)")
    print(render_report(new))
    return 0


if __name__ == "__main__":
    sys.exit(main(force="--force" in sys.argv))
