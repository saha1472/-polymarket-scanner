"""Daily Discord heartbeat: one message a day (first run after 08:00 JST) with
every paper ledger's record and P&L, so a quiet Discord never leaves you
guessing whether the bot is alive."""
import glob
import json
import os
from datetime import datetime, timedelta, timezone

import requests

JST = timezone(timedelta(hours=9))
SEND_AFTER_HOUR = 8
STATE_FILE = "heartbeat.json"

NAMES = {
    "ledger.json": "General copy",
    "ledger_sports.json": "Sports copy",
    "ledger_esports.json": "Esports copy",
    "ledger_tech.json": "Tech copy",
    "ledger_weather.json": "Weather copy",
    "ledger_wx_dead.json": "WX dead buckets",
    "ledger_wx_late.json": "WX late (risky)",
    "ledger_wx_shift.json": "WX forecast shift",
    "ledger_wx_morning.json": "WX morning low (maker)",
    "ledger_wx_afternoon.json": "WX afternoon high (maker)",
}


def read_json(path, default):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def line(path, since):
    led = read_json(path, None)
    if not led:
        return None
    bets = led.get("bets", [])
    done = [b for b in bets if b.get("status") in ("won", "lost", "stopped")]
    open_n = sum(1 for b in bets if b.get("status") == "open")
    resting = sum(1 for b in bets if b.get("status") == "resting")
    wins = sum(1 for b in done if b.get("status") == "won")
    stopped = sum(1 for b in done if b.get("status") == "stopped")
    pnl = sum(b.get("pnl", 0) for b in done)
    day = [b for b in done if b.get("closed", "") >= since]
    day_pnl = sum(b.get("pnl", 0) for b in day)
    staked = sum(b.get("stake", 0) + b.get("fee", 0) for b in done)
    roi = f"{pnl / staked:+.0%}" if staked else "n/a"
    rec = f"{wins}-{len(done) - wins - stopped}" + (f" (+{stopped} stopped)" if stopped else "")
    name = NAMES.get(path, path.replace("ledger_", "").replace(".json", ""))
    return (pnl, f"{'🟢' if pnl > 0 else '🔴' if pnl < 0 else '⚪'} **{name}**: {rec} | "
                 f"P&L ${pnl:+.2f} (ROI {roi}) | 24h {len(day)} settled ${day_pnl:+.2f} | "
                 f"open {open_n}" + (f" (+{resting} resting bids)" if resting else "")
                 + f" | cash ${led.get('cash', 0):.2f}"
                 + (" | ⏸️ paused" if led.get("paused") else ""))


def main():
    now = datetime.now(JST)
    state = read_json(STATE_FILE, {})
    if now.hour < SEND_AFTER_HOUR or state.get("last") == now.date().isoformat():
        print("Heartbeat already sent today (or too early).")
        return
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    rows = [r for r in (line(p, since) for p in sorted(glob.glob("ledger*.json"))) if r]
    total = sum(r[0] for r in rows)
    msg = [f"🫀 **Daily heartbeat {now:%a %d %b, %H:%M} JST** | all paper ledgers "
           f"${total:+.2f}"] + [r[1] for r in sorted(rows, key=lambda r: -r[0])]
    text = "\n".join(msg)
    print(text)
    url = os.environ.get("DISCORD_WEBHOOK_URL")
    if url:
        for i in range(0, len(text), 1900):
            requests.post(url, json={"content": text[i:i + 1900]}, timeout=10)
    state["last"] = now.date().isoformat()
    with open(STATE_FILE, "w") as fh:
        json.dump(state, fh)


if __name__ == "__main__":
    main()
