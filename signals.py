import json
import os
import time
from datetime import datetime, timedelta, timezone

import requests

DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
BOOK_URL = "https://clob.polymarket.com/book"
LEDGER_FILE = "ledger.json"

# ---------- Your settings ----------
START_BANKROLL = 50.00
MAX_BET = 1.00             # never stake more than this on one bet
MARKET_ORDER_MIN_USD = 1.00
DEFAULT_MIN_SHARES = 5     # used if a market doesn't report its minimum
MAX_OPEN_BETS = 40
HOURS_MIN = 0.5
HOURS_MAX = 36             # set to None to ignore time to resolution
MIN_PRICE = 0.55
MAX_PRICE = 0.95
FEE_RATE = 0.07            # worst-case taker rate; most categories are lower
MIN_RETURN_IF_WIN = 0.03   # need at least 3% profit on the stake if it wins

# ---------- Trader filter ----------
LEADERBOARD_SIZE = 100
MIN_CLOSED = 20
MIN_WIN_RATE = 0.55
MIN_ROI = 0.02
MIN_PNL_TO_VOLUME = 0.01   # weeds out market makers
MIN_POSITION_USD = 50      # ignore tiny positions
MIN_TRADERS_AGREE = 1
MAX_CHASE = 0.10           # skip if price ran 10c+ past their entry

# ---------- Exit tracking (shadow only: bets still ride to resolution) ----------
EXIT_CUT = 0.50            # a trader "exited" if they cut their size by 50%+
EXIT_MAJORITY = 0.50       # shadow-sell once this share of a bet's traders exited


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

def find_candidates(good):
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
                "traders": [], "entries": [], "holders": {},
            })
            s["traders"].append(name)
            s["entries"].append(num(p.get("avgPrice")))
            s["holders"][wallet] = {"name": name, "size": num(p.get("size"))}
            assets_by_condition.setdefault(cond, set()).add(asset)

    candidates = []
    for s in by_asset.values():
        if len(assets_by_condition.get(s["condition"], ())) > 1:
            continue  # good traders are on opposite sides
        if len(s["traders"]) < MIN_TRADERS_AGREE:
            continue
        if not MIN_PRICE <= s["price"] <= MAX_PRICE:
            continue
        s["avg_entry"] = sum(s["entries"]) / len(s["entries"])
        candidates.append(s)
    return sorted(candidates, key=lambda s: len(s["traders"]), reverse=True)


# ---------- Step 3: size each bet from the live order book ----------

def size_bet(s):
    """Use the real best ask and the market's minimum order size to find
    the cheapest legal bet. Returns the updated signal, or None."""
    book = get(BOOK_URL, {"token_id": s["asset"]})
    if not book:
        return None
    asks = sorted((num(a.get("price")), num(a.get("size")))
                  for a in book.get("asks", []))
    if not asks:
        return None
    ask, ask_size = asks[0]

    if not MIN_PRICE <= ask <= MAX_PRICE:
        return None
    if ask - s["avg_entry"] > MAX_CHASE:
        return None

    min_shares = num(book.get("min_order_size"),
                     DEFAULT_MIN_SHARES) or DEFAULT_MIN_SHARES
    limit_cost = min_shares * ask
    if limit_cost < MARKET_ORDER_MIN_USD:
        order, shares, stake = "limit", min_shares, limit_cost
    else:
        order = "market"
        shares = MARKET_ORDER_MIN_USD / ask
        stake = MARKET_ORDER_MIN_USD

    if stake > MAX_BET or ask_size < shares:
        return None

    fee = shares * FEE_RATE * ask * (1 - ask)
    profit = shares - stake - fee
    if profit / stake < MIN_RETURN_IF_WIN:
        return None

    s.update(price=ask, order=order, shares=shares, stake=stake, fee=fee,
             profit_if_win=profit, breakeven=(stake + fee) / shares)
    return s


# ---------- Step 4: paper trading ledger ----------

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
                   {"condition_ids": bet["condition"], "closed": "true"}) or []
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
        if ledger["cash"] < s["stake"] + s["fee"]:
            continue  # try cheaper signals further down the list
        ledger["cash"] -= s["stake"] + s["fee"]
        ledger["bets"].append({
            "asset": s["asset"], "condition": s["condition"],
            "title": s["title"], "outcome": s["outcome"],
            "price": s["price"], "shares": s["shares"],
            "stake": s["stake"], "fee": s["fee"], "order": s["order"],
            "status": "open", "traders": s["traders"],
            "holders": s["holders"],
            "opened": datetime.now(timezone.utc).isoformat(),
            "ends": ((datetime.now(timezone.utc) + timedelta(hours=s["hours"])).isoformat()
                     if s["hours"] else None),
        })
        held.add(s["asset"])
        ends = f"ends in {s['hours']:.0f}h | " if s["hours"] else ""
        msgs.append(
            f"📝 Paper bet ${s['stake']:.2f} ({s['shares']:.2f} shares, "
            f"{s['order']} order) on {s['outcome']} @ {s['price']:.2f} | "
            f"{s['title']} | {len(s['traders'])} traders (avg entry "
            f"{s['avg_entry']:.2f}) | {ends}win +${s['profit_if_win']:.2f}, "
            f"needs >{s['breakeven']:.1%} to break even"
        )
    return msgs


def check_exits(ledger):
    """Watch the traders behind each open bet. If enough of them sell (or
    flip sides), record a shadow exit at the current best bid. The real
    paper bet still rides to resolution, so both strategies can be compared."""
    msgs = []
    for bet in ledger["bets"]:
        if bet.get("shadow_exit") and bet["shadow_exit"]["bid"] <= 0:
            del bet["shadow_exit"]  # recorded against an empty book: not real
        if bet["status"] != "open" or not bet.get("holders"):
            continue
        if bet.get("ends") and hours_until(bet["ends"]) is not None \
                and hours_until(bet["ends"]) <= 0:
            continue  # trading is over; traders cashing out isn't a signal
        if bet.get("shadow_exit"):
            continue  # already shadow-sold
        for wallet, h in bet["holders"].items():
            if h.get("exited"):
                continue
            rows = get(f"{DATA_API}/positions",
                       {"user": wallet, "market": bet["condition"]})
            if rows is None:
                continue  # API hiccup: don't count it as an exit
            now_size, flipped = 0.0, False
            for p in rows:
                if p.get("asset") == bet["asset"]:
                    now_size = num(p.get("size"))
                elif num(p.get("size")) * num(p.get("curPrice")) >= MIN_POSITION_USD:
                    flipped = True
            if flipped or now_size <= h["size"] * (1 - EXIT_CUT):
                h["exited"] = datetime.now(timezone.utc).isoformat()
                h["exit_reason"] = "flipped" if flipped else (
                    "sold all" if now_size <= 0 else
                    f"cut {1 - now_size / h['size']:.0%}")
                msgs.append(f"👀 {h['name']} {h['exit_reason']} on "
                            f"{bet['outcome']} | {bet['title']}")

        exited = sum(1 for h in bet["holders"].values() if h.get("exited"))
        if exited / len(bet["holders"]) < EXIT_MAJORITY:
            continue
        book = get(BOOK_URL, {"token_id": bet["asset"]})
        bids = sorted((num(b.get("price")) for b in (book or {}).get("bids", [])),
                      reverse=True)
        bid = bids[0] if bids else 0.0
        if bid <= 0:
            continue  # nothing to sell into; try again next run
        sell_fee = bet["shares"] * FEE_RATE * bid * (1 - bid)
        bet["shadow_exit"] = {
            "time": datetime.now(timezone.utc).isoformat(),
            "bid": bid,
            "pnl": bet["shares"] * bid - sell_fee - bet["stake"] - bet["fee"],
        }
        msgs.append(f"🚪 Shadow exit {bet['outcome']} @ {bid:.2f} "
                    f"(entry {bet['price']:.2f}) → would lock "
                    f"${bet['shadow_exit']['pnl']:+.2f} | {bet['title']}")
    return msgs


def notify(lines):
    url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not url or not lines:
        return
    requests.post(url, json={"content": "\n".join(lines)[:1900]}, timeout=10)


if __name__ == "__main__":
    ledger = load_ledger()
    exit_msgs = check_exits(ledger)  # before settling, so bogus shadows get cleaned
    msgs = settle(ledger) + exit_msgs

    traders = top_traders()
    print(f"{len(traders)} traders after the leaderboard filter.")
    good = {}
    for wallet, name in traders.items():
        quality = trader_quality(wallet)
        if quality:
            good[wallet] = (name, *quality)
    print(f"{len(good)} passed the quality filter: "
          f"{', '.join(v[0] for v in good.values())}")

    candidates = find_candidates(good)
    print(f"{len(candidates)} candidate(s) before order book checks.")
    signals = [s for s in (size_bet(c) for c in candidates) if s]
    print(f"{len(signals)} signal(s) after sizing and fee checks.")

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
        follow = sum(b["shadow_exit"]["pnl"] if b.get("shadow_exit")
                     else b["pnl"] for b in done)
        shadowed = sum(1 for b in done if b.get("shadow_exit"))
        if shadowed:
            msgs.append(f"🔁 Following exits instead: ${follow:+.2f} "
                        f"({shadowed} settled bet(s) had a shadow exit)")
    print("\n".join(msgs))
    notify(msgs)
