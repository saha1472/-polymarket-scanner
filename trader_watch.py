"""Per-trader pause for the copy scans, judged on OUR OWN copies of each trader.

A trader gets into the pool on their own 30-day record, which is partly a hot
streak. The only record a streak cannot flatter is what copying them actually
made for us. So each scan keeps a small watch list inside its ledger
(ledger["trader_watch"]):

  flag     a trader with MIN_COPIES or more settled copies whose results are a
           loss even at the optimistic end of their range (same test as the
           strategy auto-pause in shadow.py).
  resume   their results since the flag turn convincingly positive.
  reset    after RESET_DAYS a flag expires and the trader is judged afresh on
           bets opened from then on, so nobody is locked out for ever.

SHADOW FIRST. While signals.TRADER_PAUSE_LIVE is False this only marks bets
(`watch: [names]`) and reports; it changes no bet. When switched on, a signal is
skipped only if EVERY trader behind it is flagged, and it is still recorded as
a shadow bet (reason "trader_paused"), which is what a resume is judged on.

If every trader in a scan ends up flagged the scan simply places no bets until
the pool refills (traders are re-scored every 6 hours) or a flag lifts. That is
the intended result: nobody proven worth copying means no bet.

Pure functions: no network, no files.
"""
from datetime import datetime, timedelta, timezone

import shadow

MIN_COPIES = 10             # never judge a trader on fewer settled copies than this
PAUSE_Z = shadow.PAUSE_Z    # flag only if even the optimistic end (90%, one-sided) is a loss
RESUME_Z = shadow.RESUME_Z  # resume when the cautious end (75%, one-sided) is a gain
RESET_DAYS = 30             # a flag older than this is dropped and the trader starts afresh
KEY = "trader_watch"


def _when(bet):
    return bet.get("opened") or bet.get("created") or ""


def copies(bets, trader, since=""):
    """Settled bets (real or shadow) that `trader` was behind, opened at or after `since`."""
    return [b for b in bets if b.get("status") in shadow.SETTLED
            and trader in (b.get("traders") or ()) and _when(b) >= (since or "")]


def flagged(ledger):
    """Names of the traders currently flagged in this ledger."""
    watch = ledger.get(KEY)
    return {t for t, st in watch.items() if isinstance(st, dict) and st.get("paused")} \
        if isinstance(watch, dict) else set()


def hits(signal, paused):
    """The flagged traders behind a signal (in the signal's own order)."""
    return [t for t in signal.get("traders") or () if t in paused]


def blocked(signal, paused):
    """True if every trader behind the signal is flagged (so nobody unflagged vouches for it)."""
    traders = signal.get("traders") or ()
    return bool(traders) and all(t in paused for t in traders)


def _age_days(since, now):
    try:
        return (datetime.fromisoformat(now) - datetime.fromisoformat(since)) / timedelta(days=1)
    except (TypeError, ValueError):
        return 0.0


def update(ledger, shadow_bets, live=False, now=None):
    """Flag, resume or reset traders in place. Returns one message per change."""
    now = now or datetime.now(timezone.utc).isoformat()
    watch = ledger.get(KEY)
    if not isinstance(watch, dict):
        watch = {}
    real = ledger.get("bets", [])
    skipped = [b for b in shadow_bets if b.get("kind") == "skipped"
               and b.get("reason") == "trader_paused"]
    names = {t for b in real for t in (b.get("traders") or ())} | set(watch)
    tag = "" if live else " (shadow only: still being copied)"
    msgs = []
    for t in sorted(names):
        st = watch.get(t) if isinstance(watch.get(t), dict) else {}
        hist = st.get("history", [])
        if not st.get("paused"):
            n, mean, se = shadow.roi_stats(copies(real, t, st.get("judge_from", "")))
            if n >= MIN_COPIES and mean + PAUSE_Z * se < 0:
                why = f"our last {n} copies: {mean:+.0%} per bet (±{se:.0%})"
                watch[t] = {"paused": True, "since": now, "why": why, "history": hist}
                msgs.append(f"👀 Trader flagged{tag}: {t}, {why}.")
            continue
        since = st.get("since", "")
        n, mean, se = shadow.roi_stats(copies(real, t, since) + copies(skipped, t, since))
        if n >= MIN_COPIES and mean - RESUME_Z * se > 0:
            why = f"{n} bets since the flag: {mean:+.0%} per bet (±{se:.0%})"
            watch[t] = {"paused": False, "judge_from": now, "why": why,
                        "history": hist + [{"flagged": since, "ended": now, "how": "resumed",
                                            "why": st.get("why")}]}
            msgs.append(f"👀 Trader flag lifted{tag}: {t}, {why}.")
        elif _age_days(since, now) >= RESET_DAYS:
            watch[t] = {"paused": False, "judge_from": now,
                        "why": f"flag expired after {RESET_DAYS} days",
                        "history": hist + [{"flagged": since, "ended": now, "how": "expired",
                                            "why": st.get("why")}]}
            msgs.append(f"👀 Trader flag expired{tag}: {t} starts afresh after {RESET_DAYS} days.")
    if watch or KEY in ledger:
        ledger[KEY] = watch
    return msgs


def report(ledger, name):
    """Every copied trader's record with us, worst first, and who is flagged."""
    real = ledger.get("bets", [])
    paused = flagged(ledger)
    rows = []
    for t in {t for b in real for t in (b.get("traders") or ())}:
        n, mean, se = shadow.roi_stats(copies(real, t))
        if n:
            rows.append((mean, t, n, se))
    if not rows:
        return f"{name}: no settled copies yet."
    lines = [f"{name}: our own results per copied trader (flag needs {MIN_COPIES}+ copies)",
             f"  {'trader':<26} {'copies':>6} {'per bet':>8} {'+/-':>6}"]
    for mean, t, n, se in sorted(rows):
        err = f"{se:.0%}" if se != float("inf") else "n/a"
        mark = "  FLAGGED" if t in paused else ("" if n >= MIN_COPIES else "  (too few to judge)")
        lines.append(f"  {t[:26]:<26} {n:>6} {mean:>+8.0%} {err:>6}{mark}")
    return "\n".join(lines)


def main(argv=None):
    import sys

    import simcore
    scans = [a.upper() for a in (sys.argv[1:] if argv is None else argv)] or \
        ["GENERAL", "SPORTS", "ESPORTS", "TECH", "WEATHER", "GEOPOLITICS"]
    for scan in scans:
        path = "ledger.json" if scan == "GENERAL" else f"ledger_{scan.lower()}.json"
        led = simcore.read_json(path, None)
        if isinstance(led, dict):
            print(report(led, scan))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
