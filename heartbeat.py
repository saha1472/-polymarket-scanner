"""Daily Discord heartbeat: one message a day (first run after 08:00 JST) with
every paper ledger's record and P&L, so a quiet Discord never leaves you
guessing whether the bot is alive."""
import glob
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import simcore

JST = timezone(timedelta(hours=9))
SEND_AFTER_HOUR = 8
STATE_FILE = "heartbeat.json"
REWARDS_LOG = "rewards_log.jsonl"      # written by rewards_tracker.py

NAMES = {
    "ledger.json": "General copy",
    "ledger_sports.json": "Sports copy",
    "ledger_esports.json": "Esports copy",
    "ledger_tech.json": "Tech copy",
    "ledger_weather.json": "Weather copy",
    "ledger_wx_dead.json": "WX dead buckets",
    "ledger_wx_late.json": "WX late (risky)",
    "ledger_wx_shift.json": "WX forecast shift",
    "ledger_wx_morning.json": "WX morning low (maker, proxy fills)",
    "ledger_wx_afternoon.json": "WX afternoon high (maker, proxy fills)",
}


def read_json(path, default):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def line(path, since):
    name = NAMES.get(path, path.replace("ledger_", "").replace(".json", ""))
    try:
        led = simcore.load_ledger(path, 50.0)   # same validation the bots use
    except SystemExit as err:
        # never hide a broken ledger from the daily summary
        return (float("-inf"), f"⚠️ **{name}**: ledger can't be read or is invalid ({err})")
    bets = led.get("bets", [])
    done = [b for b in bets if b.get("status") in ("won", "lost", "stopped", "split")]
    open_n = sum(1 for b in bets if b.get("status") == "open")
    resting = sum(1 for b in bets if b.get("status") == "resting")
    wins = sum(1 for b in done if b.get("status") == "won")
    stopped = sum(1 for b in done if b.get("status") == "stopped")
    pnl = sum(b.get("pnl", 0) for b in done)
    day = [b for b in done if b.get("closed", "") >= since]
    day_pnl = sum(b.get("pnl", 0) for b in day)
    staked = sum(b.get("stake", 0) + b.get("fee", 0) for b in done)
    roi = f"{pnl / staked:+.0%}" if staked else "n/a"
    split = sum(1 for b in done if b.get("status") == "split")
    rec = (f"{wins}-{len(done) - wins - stopped - split}"
           + (f" (+{stopped} stopped)" if stopped else "")
           + (f" (+{split} split)" if split else ""))
    return (pnl, f"{'🟢' if pnl > 0 else '🔴' if pnl < 0 else '⚪'} **{name}**: {rec} | "
                 f"P&L ${pnl:+.2f} (ROI {roi}) | 24h {len(day)} settled ${day_pnl:+.2f} | "
                 f"open {open_n}" + (f" (+{resting} resting bids)" if resting else "")
                 + f" | cash ${led.get('cash', 0):.2f}"
                 + (" | ⏸️ paused" if led.get("paused") else "")
                 + (" | 🤖 auto-paused" if (led.get("auto") or {}).get("paused") else ""))


def _f(value):
    """A finite float, else 0 (a damaged line must not break the daily summary)."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 0.0
    return v if v == v and v not in (float("inf"), float("-inf")) else 0.0


def _usd(x):
    return f"-${-x:.2f}" if x < 0 else f"+${x:.2f}"


def _totals(rows):
    """Sum a list of rewards-log lines: reward low/high, fill P&L, hits, hours."""
    t = {"lo": 0.0, "hi": 0.0, "fill": 0.0, "hits": 0, "hours": 0.0}
    for r in rows:
        acc = r.get("accrued") if isinstance(r.get("accrued"), list) else []
        t["lo"] += _f(acc[0]) if len(acc) > 0 else 0.0
        t["hi"] += _f(acc[1]) if len(acc) > 1 else 0.0
        t["fill"] += _f(r.get("fill_pnl"))
        t["hits"] += int(bool(r.get("bid_hit"))) + int(bool(r.get("ask_hit")))
        t["hours"] += _f(r.get("hours"))
    return t


def rewards_line(since, path=None):
    """One line on the hypothetical rewards tracker (it never places an order):
    liquidity rewards it would have earned against what its quotes would have
    lost when hit, for the last 24 hours and for the whole record. Rewards and
    fills are both modelled, so it says estimate. Returns None when there is no
    log yet, and never raises: this line is extra, the ledgers come first."""
    try:
        rows = []
        with open(path or REWARDS_LOG) as fh:
            for raw in fh:
                try:
                    r = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(r, dict) and isinstance(r.get("time"), str) and r.get("condition"):
                    rows.append(r)
        if not rows:
            return None
        day = [r for r in rows if r["time"] >= since]
        every, last = _totals(rows), _totals(day)
        markets = {r["condition"] for r in rows}
        capital = 0.0
        for m in markets:
            capital += max(_f(r.get("capital")) for r in rows if r["condition"] == m)
        days = every["hours"] / 24 / len(markets)
        head = "💸 **Rewards tracker** (hypothetical, places no orders, estimate)"
        if day:
            today = (f"last 24h reward ${last['lo']:.2f}-${last['hi']:.2f}, fills "
                     f"{_usd(last['fill'])} ({last['hits']} hits), net "
                     f"{_usd(last['lo'] + last['fill'])} to {_usd(last['hi'] + last['fill'])}")
        else:
            today = "last 24h: no quotes logged"
        whole = (f"whole record (~{days:.1f} days per market): net "
                 f"{_usd(every['lo'] + every['fill'])} to {_usd(every['hi'] + every['fill'])} "
                 f"on about ${capital:.0f} of capital")
        return f"{head}: {today} | {whole}"
    except Exception:                      # noqa: BLE001
        return None


def main():
    now = datetime.now(JST)
    state = read_json(STATE_FILE, {})
    if now.hour < SEND_AFTER_HOUR or state.get("last") == now.date().isoformat():
        print("Heartbeat already sent today (or too early).")
        return
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    rows = [r for r in (line(p, since) for p in sorted(glob.glob("ledger*.json"))) if r]
    total = sum(r[0] for r in rows if r[0] != float("-inf"))
    msg = [f"🫀 **Daily heartbeat {now:%a %d %b, %H:%M} JST** | all paper ledgers "
           f"${total:+.2f}"] + [r[1] for r in sorted(rows, key=lambda r: -r[0])]
    extra = rewards_line(since)
    if extra:
        msg.append(extra)
    text = "\n".join(msg)
    print(text)
    url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not url:
        print("No DISCORD_WEBHOOK_URL set; heartbeat not sent (and not marked as sent).")
        return 0
    if not simcore.post_discord(url, text):
        # don't mark it as sent: the next run tries again
        print("Heartbeat NOT delivered; will retry next run.")
        return 1
    state["last"] = now.date().isoformat()
    simcore.write_json_atomic(STATE_FILE, state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
