import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import requests

DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
BOOK_URL = "https://clob.polymarket.com/book"

# ---------- Which scan is this? ----------
# python signals.py           -> general scan (all categories), ledger.json
# python signals.py SPORTS    -> sports-only scan, ledger_sports.json
# Each category: leaderboard filter, Gamma tag slug used to recognise its
# markets, taker fee rate, and the price band to buy in.
CATEGORIES = {
    "SPORTS":  {"tag": "sports",  "fee": 0.05, "min_price": 0.40, "max_price": 0.90},
    "ESPORTS": {"tag": "esports", "fee": 0.05, "min_price": 0.40, "max_price": 0.90},
    "TECH":    {"tag": "tech",    "fee": 0.04, "min_price": 0.40, "max_price": 0.90},
    "WEATHER": {"tag": "weather", "fee": 0.05, "min_price": 0.40, "max_price": 0.90},
}
SCAN = (sys.argv[1] if len(sys.argv) > 1 else "GENERAL").upper()
if SCAN != "GENERAL" and SCAN not in CATEGORIES:
    sys.exit(f"Unknown scan {SCAN}. Use one of: {', '.join(CATEGORIES)}")
CAT = CATEGORIES.get(SCAN)
LEDGER_FILE = "ledger.json" if SCAN == "GENERAL" else f"ledger_{SCAN.lower()}.json"
TRADERS_FILE = f"traders_{SCAN.lower()}.json"   # cached trader scores
TAGS_FILE = "tags.json"                         # event -> tags cache (not committed)

# ---------- Your settings ----------
START_BANKROLL = 50.00
MAX_BET = 1.00             # never stake more than this on one bet
MARKET_ORDER_MIN_USD = 1.00
DEFAULT_MIN_SHARES = 5     # used if a market doesn't report its minimum
MAX_OPEN_BETS = 40
HOURS_MIN = 0.5
HOURS_MAX = 36             # set to None to ignore time to resolution
MIN_PRICE = CAT["min_price"] if CAT else 0.55
MAX_PRICE = CAT["max_price"] if CAT else 0.95
FEE_RATE = CAT["fee"] if CAT else 0.07   # general scan uses the worst case
MIN_RETURN_IF_WIN = 0.03   # need at least 3% profit on the stake if it wins

# ---------- Trader filter ----------
LEADERBOARD_SIZE = 100 if SCAN == "GENERAL" else 50
HISTORY_DAYS = 30          # judge traders on bets resolved in the last 30 days
MIN_RESOLVED = 15          # need at least this many resolved bets to judge
MIN_WIN_RATE = 0.55
MIN_ROI = 0.02
SCORE_REFRESH_HOURS = 6    # re-score traders this often (slow step)
MIN_PNL_TO_VOLUME = 0.01   # weeds out market makers
MIN_POSITION_USD = 50      # ignore tiny positions
MIN_TRADERS_AGREE = 1
MAX_CHASE = 0.10           # skip if price ran 10c+ past their entry

# ---------- Exit tracking (shadow only: bets still ride to resolution) ----------
EXIT_CUT = 0.50            # a trader "exited" if they cut their size by 50%+
EXIT_MAJORITY = 0.50       # shadow-sell once this share of a bet's traders exited

# ---------- Stop-loss test (shadow only) ----------
# Record the first time the best bid falls this far below the entry price.
STOP_LEVELS = (0.25, 0.40, 0.60)

SESSION = requests.Session()


def get(url, params=None):
    for attempt in range(3):
        try:
            resp = SESSION.get(url, params=params, timeout=20)
        except requests.RequestException as err:
            print(f"Request failed for {url}: {err}")
            time.sleep(1 + attempt)
            continue
        if resp.status_code == 429 or resp.status_code >= 500:
            time.sleep(2 + attempt * 2)
            continue
        if resp.status_code != 200:
            print(f"{url} returned {resp.status_code}: {resp.text[:200]}")
            return None
        time.sleep(0.15)
        data = resp.json()
        if isinstance(data, dict) and "data" in data:
            data = data["data"]
        return data
    return None


def pmap(fn, items, workers=8):
    """Run fn over items in parallel threads (the APIs are slow, not busy)."""
    items = list(items)
    if not items:
        return []
    with ThreadPoolExecutor(workers) as pool:
        return list(pool.map(fn, items))


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


def read_json(path, default):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def write_json(path, data, indent=None):
    with open(path, "w") as fh:
        json.dump(data, fh, indent=indent)


# ---------- Category tags (which markets belong to which category) ----------

TAGS = read_json(TAGS_FILE, {})


def event_tags(slug):
    if not slug:
        return []
    if slug not in TAGS:
        rows = get(f"{GAMMA_API}/events", {"slug": slug})
        if rows is None:
            return []  # don't cache API failures
        TAGS[slug] = [t.get("slug") for t in ((rows[0].get("tags") or [])
                                              if rows else [])]
    return TAGS[slug]


def in_scan(slug):
    return CAT is None or CAT["tag"] in event_tags(slug)


def warm_tags(slugs):
    todo = [s for s in set(slugs) if s and s not in TAGS]
    with ThreadPoolExecutor(6) as pool:
        list(pool.map(event_tags, todo))


# ---------- Step 1: find good traders ----------

def top_traders():
    wallets = {}
    for period in ("MONTH", "ALL"):
        params = {"timePeriod": period, "orderBy": "PNL",
                  "limit": LEADERBOARD_SIZE}
        if CAT:
            params["category"] = SCAN
        for row in get(f"{DATA_API}/v1/leaderboard", params) or []:
            wallet = row.get("proxyWallet")
            if not wallet:
                continue
            pnl, vol = num(row.get("pnl")), num(row.get("vol"))
            if vol > 0 and pnl / vol < MIN_PNL_TO_VOLUME:
                continue
            wallets[wallet] = row.get("userName") or wallet[:10]
    return wallets


def trader_history(wallet):
    """Every bet this trader had resolved in the last HISTORY_DAYS, wins AND
    losses, as (event slug, pnl, cost). Closed positions alone miss most
    losses: a losing bet resolves to $0 and is usually never redeemed, so it
    stays in /positions as 'redeemable'."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=HISTORY_DAYS)
    out = []
    for offset in range(0, 500, 50):
        rows = get(f"{DATA_API}/closed-positions",
                   {"user": wallet, "limit": 50, "offset": offset,
                    "sortBy": "TIMESTAMP", "sortDirection": "DESC"})
        if rows is None:
            return None
        for r in rows:
            if num(r.get("timestamp")) < cutoff.timestamp():
                break
            cost = num(r.get("totalBought")) * num(r.get("avgPrice"))
            out.append((r.get("eventSlug"), num(r.get("realizedPnl")), cost))
        else:
            if len(rows) == 50:
                continue
        break
    for offset in range(0, 3000, 500):
        rows = get(f"{DATA_API}/positions",
                   {"user": wallet, "limit": 500, "offset": offset,
                    "sizeThreshold": 0, "redeemable": "true"})
        if rows is None:
            return None
        for p in rows:
            if str(p.get("endDate", ""))[:10] < cutoff.date().isoformat():
                continue
            cost = num(p.get("initialValue"))
            out.append((p.get("eventSlug"),
                        num(p.get("currentValue")) - cost, cost))
        if len(rows) < 500:
            break
    return [x for x in out if x[2] > 0]


def score_traders(traders):
    """Win rate and ROI on recent resolved bets (in this scan's category)."""
    with ThreadPoolExecutor(6) as pool:
        histories = dict(zip(traders, pool.map(trader_history, traders)))
    if CAT:
        warm_tags(s for h in histories.values() if h for s, _, _ in h)
    scores = {}
    for wallet, hist in histories.items():
        if not hist:
            continue
        bets = [x for x in hist if in_scan(x[0])]
        if len(bets) < MIN_RESOLVED:
            continue
        wins = sum(1 for _, pnl, _ in bets if pnl > 0)
        cost = sum(c for _, _, c in bets)
        scores[wallet] = {"name": traders[wallet], "n": len(bets),
                          "win_rate": wins / len(bets),
                          "roi": sum(p for _, p, _ in bets) / cost}
    return scores


def good_traders():
    cache = read_json(TRADERS_FILE, {})
    age = hours_until(cache.get("scored_at"))
    if age is not None and -age < SCORE_REFRESH_HOURS:
        scores = cache["scores"]
        print(f"Using trader scores from {-age:.1f}h ago.")
    else:
        traders = top_traders()
        print(f"{len(traders)} traders after the leaderboard filter. Scoring...")
        scores = score_traders(traders)
        write_json(TRADERS_FILE, {
            "scored_at": datetime.now(timezone.utc).isoformat(),
            "scores": scores}, indent=1)
    good = {w: s for w, s in scores.items()
            if s["win_rate"] >= MIN_WIN_RATE and s["roi"] >= MIN_ROI}
    print(f"{len(good)} of {len(scores)} scored traders pass: " + ", ".join(
        f"{s['name']} ({s['win_rate']:.0%}, ROI {s['roi']:+.0%}, n={s['n']})"
        for s in good.values()))
    return good


# ---------- Step 2: find what they agree on ----------

def find_candidates(good):
    by_asset, assets_by_condition = {}, {}
    wallets = list(good)
    all_rows = pmap(lambda w: get(f"{DATA_API}/positions",
                                  {"user": w, "sizeThreshold": 1,
                                   "limit": 500}) or [], wallets)
    if CAT:
        warm_tags(p.get("eventSlug") for rows in all_rows for p in rows
                  if not p.get("redeemable")
                  and HOURS_MIN <= (hours_until(p.get("endDate")) or -1) <= (HOURS_MAX or 1e9))
    for wallet, rows in zip(wallets, all_rows):
        name = good[wallet]["name"]
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
            if not in_scan(p.get("eventSlug")):
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
    return read_json(LEDGER_FILE, {"cash": START_BANKROLL, "bets": []})


def save_ledger(ledger):
    write_json(LEDGER_FILE, ledger, indent=2)


def settle(ledger):
    msgs = []
    open_bets = [b for b in ledger["bets"] if b["status"] == "open"]
    markets = pmap(lambda b: get(f"{GAMMA_API}/markets",
                                 {"condition_ids": b["condition"],
                                  "closed": "true"}) or [], open_bets)
    for bet, rows in zip(open_bets, markets):
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
        watch = [(w, h) for w, h in bet["holders"].items() if not h.get("exited")]
        found = pmap(lambda wh: get(f"{DATA_API}/positions",
                                    {"user": wh[0], "market": bet["condition"]}),
                     watch)
        for (wallet, h), rows in zip(watch, found):
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


def check_stops(ledger):
    """Shadow stop-losses: for each open bet, note the first run where the
    best bid sits 25% / 40% / 60% below entry, and what selling there would
    have locked in. The bet itself still rides to resolution."""
    msgs = []
    watch = [b for b in ledger["bets"] if b["status"] == "open"
             and not (b.get("ends") and hours_until(b["ends"]) is not None
                      and hours_until(b["ends"]) <= 0)
             and any(str(lvl) not in b.get("stops", {}) for lvl in STOP_LEVELS)]
    books = pmap(lambda b: get(BOOK_URL, {"token_id": b["asset"]}), watch)
    for bet, book in zip(watch, books):
        stops = bet.setdefault("stops", {})
        todo = [lvl for lvl in STOP_LEVELS if str(lvl) not in stops]
        bids = [num(b.get("price")) for b in (book or {}).get("bids", [])]
        bid = max(bids) if bids else 0.0
        if bid <= 0:
            continue  # empty book: nothing to sell into
        for lvl in todo:
            if bid > bet["price"] * (1 - lvl):
                continue
            sell_fee = bet["shares"] * FEE_RATE * bid * (1 - bid)
            pnl = bet["shares"] * bid - sell_fee - bet["stake"] - bet["fee"]
            stops[str(lvl)] = {"time": datetime.now(timezone.utc).isoformat(),
                               "bid": bid, "pnl": pnl}
            msgs.append(f"🛑 Stop -{lvl:.0%} hit: {bet['outcome']} @ {bid:.2f} "
                        f"(entry {bet['price']:.2f}) → would lock ${pnl:+.2f} "
                        f"| {bet['title']}")
    return msgs


def notify(lines):
    url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not url or not lines:
        return
    text = "\n".join(lines)
    for i in range(0, len(text), 1900):   # Discord caps messages at 2000 chars
        requests.post(url, json={"content": text[i:i + 1900]}, timeout=10)


if __name__ == "__main__":
    print(f"=== {SCAN} scan | ledger {LEDGER_FILE} | price {MIN_PRICE}-{MAX_PRICE} "
          f"| fee rate {FEE_RATE} ===")
    ledger = load_ledger()
    exit_msgs = check_exits(ledger)  # before settling, so bogus shadows get cleaned
    exit_msgs += check_stops(ledger)
    msgs = settle(ledger) + exit_msgs

    good = good_traders()
    candidates = find_candidates(good)
    print(f"{len(candidates)} candidate(s) before order book checks.")
    signals = [s for s in pmap(size_bet, candidates) if s]
    print(f"{len(signals)} signal(s) after sizing and fee checks.")

    msgs += open_paper_bets(ledger, signals)
    save_ledger(ledger)
    if len(TAGS) > 30000:
        TAGS.clear()   # keep the cache from growing forever
    tmp = f"{TAGS_FILE}.{SCAN}.tmp"
    write_json(tmp, TAGS)
    os.replace(tmp, TAGS_FILE)   # atomic: safe if scans run side by side

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
        if done:
            parts = []
            for lvl in STOP_LEVELS:
                hit = [b for b in done if str(lvl) in b.get("stops", {})]
                total = sum(b["stops"][str(lvl)]["pnl"] if b in hit else b["pnl"]
                            for b in done)
                parts.append(f"-{lvl:.0%}: ${total:+.2f} ({len(hit)} hit)")
            msgs.append("🛑 With stop-loss " + " | ".join(parts))
        msgs = [f"**[{SCAN}]**"] + msgs
    print("\n".join(msgs))
    notify(msgs)
