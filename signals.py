import json
import os
import time
from datetime import datetime, timezone

import requests

DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
LEDGER_FILE = "ledger.json"

# ---------- Your settings ----------
START_BANKROLL = 5.00
BET_SIZE = 1.00           # $1 is Polymarket's market-order minimum
MAX_OPEN_BETS = 4
HOURS_MIN = 0
HOURS_MAX = 48            # set to None to ignore time to resolution
MIN_PRICE = 0.55
MAX_PRICE = 0.95
FEE_RATE = 0.07           # worst-case taker rate; most categories are lower
MIN_PROFIT_IF_WIN = 0.03

# ---------- Trader filter ----------
LEADERBOARD_SIZE = 50
MIN_CLOSED = 20
MIN_WIN_RATE = 0.55
MIN_ROI = 0.02
MIN_PNL_TO_VOLUME = 0.01  # weeds out market makers
MIN_POSITION_USD = 50     # ignore tiny positions
MIN_TRADERS_AGREE = 2
MAX_CHASE = 0.10          # skip if price ran 10c+ past their entry


def get(url, params=None):
    try:
        resp = requests.get(url, params=params, timeout=20)
    except requests.RequestException as err:
        print(f"Request failed for {url}: {err}")
        return None
    if resp.status_code != 200:
        print(f"{url} returned {resp.status_code}: {resp.text[:200]}")
        return None
    time.sleep(0.15)
    data = resp.json()
    if isinstance(data, dict) and "data" in data:
        data = data["data"]
    return data


def num(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def hours_until(end):
    if not end:
        return None
    try:
        dt = datetime.fromisoformat(str(end).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt - datetime.now(timezone.utc)).total_seconds() / 3600


# ---------- Step 1: find good traders ----------

def top_traders():
    wallets = {}
    for period in ("MONTH", "ALL"):
        rows = get(f"{DATA_API}/v1/leaderboard",
                   {"timePeriod": period, "orderBy": "PNL",
                    "limit": LEADERBOARD_SIZE}) or []
        for row in rows:
            wallet = row.get("proxyWallet")
            if not wallet:
                continue
            pnl, vol = num(row.get("pnl")), num(row.get("vol"))
            if vol > 0 and pnl / vol < MIN_PNL_TO_VOLUME:
                continue
            wallets[wallet] = row.get("userName") or wallet[:10]
    return wallets


def trader_quality(wallet):
    rows = get(f"{DATA_API}/closed-positions",
               {"user": wallet, "limit": 50}) or []
    if len(rows) < MIN_CLOSED:
        return None
    wins = sum(1 for r in rows if num(r.get("realizedPnl")) > 0)
    pnl = sum(num(r.get("realizedPnl")) for r in rows)
    cost = sum(num(r.get("totalBought")) * num(r.get("avgPrice"))
               for r in rows)
    if cost <= 0:
        return None
    win_rate, roi = wins / len(rows), pnl / cost
    if win_rate < MIN_WIN_RATE or roi < MIN_ROI:
        return None
    return win_rate, roi


# ---------- Step 2: find what they agree on ----------

def find_signals(good):
    by_asset, assets_by_condition = {}, {}
    for wallet, (name, _wr, _roi) in good.items():
        rows = get(f"{DATA_API}/positions",
                   {"user": wallet, "sizeThreshold": 1, "limit": 500}) or []
        for p in rows:
            if p.get("redeemable"):
                continue
            price = num(p.get("curPrice"))
            if num(p.get("size")) * price < MIN_POSITION_USD:
                continue
            hrs = hours_until(p.get("endDate"))
            if HOURS_MAX is not None and (
                    hrs is None or hrs < HOURS_MIN or hrs > HOURS_MAX):
                continue
            asset, cond = p.get("asset"), p.get("conditionId")
            if not asset:
                continue
            s = by_asset.setdefault(asset, {
                "asset": asset, "condition": cond,
                "title": p.get("title", "?"),
                "outcome": p.get("outcome", "?"),
                "price": price, "hours": hrs,
                "traders": [], "entries": [],
            })
            s["traders"].append(name)
            s["entries"].append(num(p.get("avgPrice")))
            assets_by_condition.setdefault(cond, set()).add(asset)

    signals = []
    for s in by_asset.values():
        if len(assets_by_condition.get(s["condition"], ())) > 1:
            continue  # good traders are on opposite sides
        if len(s["traders"]) < MIN_TRADERS_AGREE:
            continue
        p = s["price"]
        if not MIN_PRICE <= p <= MAX_PRICE:
            continue
        avg_entry = sum(s["entries"]) / len(s["entries"])
        if p - avg_entry > MAX_CHASE:
            continue
        fee = BET_SIZE * FEE_RATE * (1 - p)
        profit = BET_SIZE * (1 / p - 1) - fee
        if profit < MIN_PROFIT_IF_WIN:
            continue
        s.update(avg_entry=avg_entry, fee=fee, profit_if_win=profit,
                 breakeven=p * (1 + FEE_RATE * (1 - p)))
        signals.append(s)
    return sorted(signals, key=lambda s: len(s["traders"]), reverse=True)


# ---------- Step 3: paper trading ledger ----------

def load_ledger():
    if os.path.exists(LEDGER_FILE):
        with open(LEDGER_FILE) as fh:
            return json.load(fh)
    return {"cash": START_BANKROLL, "bets": []}


def save_ledger(ledger):
    with open(LEDGER_FILE, "w") as fh:
        json.dump(ledger, fh, indent=2)


def settle(ledger):
    msgs = []
    for bet in ledger["bets"]:
        if bet["status"] != "open":
            continue
        rows = get(f"{GAMMA_API}/markets",
                   {"condition_ids": bet["condition"]}) or []
        if not rows or not rows[0].get("closed"):
            continue
        try:
            ids = json.loads(rows[0].get("clobTokenIds") or "[]")
            prices = [num(x) for x in
                      json.loads(rows[0].get("outcomePrices") or "[]")]
            if max(prices) < 0.99:
                continue  # closed but not resolved yet
            won = prices[ids.index(bet["asset"])] > 0.99
        except (ValueError, IndexError):
            continue
        payout = bet["shares"] if won else 0.0
        ledger["cash"] += payout
        bet["status"] = "won" if won else "lost"
        bet["pnl"] = payout - bet["stake"] - bet["fee"]
        msgs.append(f"{'✅ WON' if won else '❌ LOST'} {bet['outcome']} | "
                    f"{bet['title']} | P&L ${bet['pnl']:+.2f}")
    return msgs


def open_paper_bets(ledger, signals):
    msgs = []
    held = {b["asset"] for b in ledger["bets"] if b["status"] == "open"}
    for s in signals:
        if len(held) >= MAX_OPEN_BETS:
            break
        if s["asset"] in held:
            continue
        if ledger["cash"] < BET_SIZE + s["fee"]:
            break
        ledger["cash"] -= BET_SIZE + s["fee"]
        ledger["bets"].append({
            "asset": s["asset"], "condition": s["condition"],
            "title": s["title"], "outcome": s["outcome"],
            "price": s["price"], "shares": BET_SIZE / s["price"],
            "stake": BET_SIZE, "fee": s["fee"], "status": "open",
            "traders": s["traders"],
            "opened": datetime.now(timezone.utc).isoformat(),
        })
        held.add(s["asset"])
        ends = f"ends in {s['hours']:.0f}h | " if s["hours"] else ""
        msgs.append(
            f"📝 Paper bet ${BET_SIZE:.2f} on {s['outcome']} @ "
            f"{s['price']:.2f} | {s['title']} | {len(s['traders'])} traders "
            f"(avg entry {s['avg_entry']:.2f}) | {ends}win +"
            f"${s['profit_if_win']:.2f}, needs >{s['breakeven']:.1%} to "
            f"break even"
        )
    return msgs


def notify(lines):
    url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not url or not lines:
        return
    requests.post(url, json={"content": "\n".join(lines)[:1900]}, timeout=10)


if __name__ == "__main__":
    ledger = load_ledger()
    msgs = settle(ledger)

    traders = top_traders()
    print(f"{len(traders)} traders after the leaderboard filter.")
    good = {}
    for wallet, name in traders.items():
        quality = trader_quality(wallet)
        if quality:
            good[wallet] = (name, *quality)
    print(f"{len(good)} passed the quality filter: "
          f"{', '.join(v[0] for v in good.values())}")

    signals = find_signals(good)
    print(f"{len(signals)} signal(s) found.")
    msgs += open_paper_bets(ledger, signals)
    save_ledger(ledger)

    if msgs:
        done = [b for b in ledger["bets"] if b["status"] != "open"]
        wins = sum(1 for b in done if b["status"] == "won")
        open_n = sum(1 for b in ledger["bets"] if b["status"] == "open")
        msgs.append(
            f"💰 Cash ${ledger['cash']:.2f} | open bets {open_n} | "
            f"record {wins}-{len(done) - wins} | realized P&L "
            f"${sum(b['pnl'] for b in done):+.2f}"
        )
    print("\n".join(msgs))
    notify(msgs)
