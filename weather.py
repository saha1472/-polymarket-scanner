"""Weather desk: three paper strategies on Polymarket's daily temperature markets.

  DEAD  - buy NO on buckets the day's airport readings have already ruled out
          (the high can only go up, the low only down). Near-certain, small gain.
  SHIFT - (OFF, see settings) when a new forecast moves a city's high/low by 0.5C+, buy YES on the
          bucket next to the market favourite, in the direction of the move,
          while it is still cheap (5-60c).
  LATE  - riskier DEAD: late in the local day, buy NO at 85-97c on buckets
          2+ degrees beyond the day's reading so far (could still be reached by
          a late swing - this is the tail risk the +$51k/30-day bot carried).
  MORNING   - buy YES on today's favourite bucket of a LOWEST-temperature
          market (C only) between 08:00 and 12:00 local, at 0.70-0.95. The low
          has usually happened by dawn, but the price is slow to catch up.
          Backtest Oct 5-27 2026: 424 bets, 96% won, +11% per bet after a 1c
          spread, positive every week.
  AFTERNOON - same idea for HIGHEST-temperature markets, 15:00-19:00 local.
          Backtest: +2-5% per bet, weaker; tracked separately.
  (FADE - buying NO on morning favourites - was dropped: the backtest lost 31%.)

Each strategy has its own $50 paper ledger. Markets resolve on the airport
station named in each market's rules (METAR, rounded to whole degrees).
"""
import json
import math
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests

GAMMA_API = "https://gamma-api.polymarket.com"
BOOK_URL = "https://clob.polymarket.com/book"
METAR_URL = "https://aviationweather.gov/api/data/metar"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
STATE_FILE = "wx_state.json"

# ---------- Settings ----------
START_BANKROLL = 50.00
BET_SIZE = 1.00
FEE_RATE = 0.05              # weather taker fee rate
MAX_OPEN_BETS = 60

# DEAD: how far below the observed high (or above the observed low) a bucket
# must sit, in the market's unit, before we call it ruled out. 1 = a full
# degree of safety against data quirks.
DEAD_MARGIN = 1
DEAD_MAX_PRICE = 0.985       # pay at most this for NO (need ~1.5%+ to be worth it)
DEAD_MIN_RETURN = 0.01       # after fees
DEAD_PER_EVENT = 3

# SHIFT - OFF since 2026-10-29: backtest (Oct 5-27, 994 bets) lost ~33% per
# bet, no better than betting against the move or picking a random neighbour.
# Its ledger stays as a record; set True to re-enable.
SHIFT_ENABLED = False
SHIFT_MIN_MOVE_C = 0.5       # forecast must move at least this much (in C)
SHIFT_MIN_PRICE = 0.05
SHIFT_MAX_PRICE = 0.60
SHIFT_DAYS_AHEAD = (1, 2)    # tomorrow and the day after

# LATE (riskier dead-bucket variant)
LATE_GAP = 2                 # bucket must be this many degrees past the reading so far
LATE_MIN_PRICE = 0.85        # NO price band
LATE_MAX_PRICE = 0.97
LATE_HIGH_FROM = 17          # local hour after which today's high has usually happened
LATE_LOW_FROM = 21           # local hour after which a big further drop is unlikely
LATE_PER_EVENT = 2

# MORNING / AFTERNOON: buy the favourite once the day's low/high has
# probably happened. kind, local hours, units allowed.
FAV_RULES = {
    "MORNING":   {"kind": "lowest",  "hours": (8, 12),  "units": ("C",)},
    "AFTERNOON": {"kind": "highest", "hours": (15, 19), "units": ("C", "F")},
}
FAV_MIN_PRICE = 0.70
FAV_MAX_PRICE = 0.95
FAV_MAX_FILL = 0.96          # take a real seller's offer only up to this price
# Usually nobody is selling the favourite, so instead we post a resting bid one
# cent above the best bid (maker: no fee). It only counts as filled if a later
# run sees a seller offering at or below our price; it is cancelled when the
# time window ends.

STRATEGIES = ("DEAD", "LATE", "SHIFT", "MORNING", "AFTERNOON")
SESSION = requests.Session()


# ---------- small helpers ----------

def get(url, params=None, timeout=25):
    for attempt in range(4):
        try:
            r = SESSION.get(url, params=params, timeout=timeout)
        except requests.RequestException as err:
            print(f"Request failed {url}: {err}")
            time.sleep(1 + attempt)
            continue
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(2 + 2 * attempt)
            continue
        if r.status_code != 200:
            print(f"{url} returned {r.status_code}: {r.text[:150]}")
            return None
        return r.json()
    return None


def pmap(fn, items, workers=8):
    items = list(items)
    if not items:
        return []
    with ThreadPoolExecutor(workers) as pool:
        return list(pool.map(fn, items))


def num(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def read_json(path, default):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(data, fh, indent=1)
    os.replace(tmp, path)


def round_half_up(x):
    return math.floor(x + 0.5)


def to_unit(celsius, unit):
    """Market rule: convert (if F) then round to whole degrees, halves up."""
    return round_half_up(celsius * 9 / 5 + 32 if unit == "F" else celsius)


def now_utc():
    return datetime.now(timezone.utc)


# ---------- markets ----------

TITLE_RE = re.compile(r"^(Highest|Lowest) temperature in (.+) on (\w+ \d+)\?$")
BUCKET_RE = re.compile(r"^(-?\d+)(?:-(-?\d+))?°([CF])(?: or (below|higher))?$")


def parse_bucket(label):
    """'6°C' -> (6, 6), '74-75°F' -> (74, 75), '0°C or below' -> (None, 0)."""
    m = BUCKET_RE.match((label or "").strip())
    if not m:
        return None
    a, b, unit, tail = m.groups()
    lo, hi = int(a), int(b) if b else int(a)
    if tail == "below":
        lo = None
    elif tail == "higher":
        hi = None
    return lo, hi, unit


def load_events():
    events = []
    for offset in range(0, 1500, 100):
        rows = get(f"{GAMMA_API}/events", {"tag_slug": "weather", "active": "true",
                                           "closed": "false", "limit": 100,
                                           "offset": offset})
        if not rows:
            break
        events += rows
        if len(rows) < 100:
            break
    out = []
    for e in events:
        m = TITLE_RE.match(e.get("title", ""))
        if not m or not e.get("markets"):
            continue
        desc = e["markets"][0].get("description", "")
        station = re.search(r"\(([A-Z]{4})\)", desc)
        if "METAR" not in desc or not station:
            continue  # e.g. Hong Kong resolves on the Observatory, skip
        try:
            year = int(e["endDate"][:4])
            day = datetime.strptime(f"{m.group(3)} {year}", "%B %d %Y").date()
        except (ValueError, KeyError, TypeError):
            continue
        buckets = []
        for mk in e["markets"]:
            b = parse_bucket(mk.get("groupItemTitle"))
            ids = json.loads(mk.get("clobTokenIds") or "[]")
            if not b or len(ids) != 2 or mk.get("closed"):
                continue
            buckets.append({
                "label": mk["groupItemTitle"], "lo": b[0], "hi": b[1], "unit": b[2],
                "condition": mk.get("conditionId"), "yes": ids[0], "no": ids[1],
                "yes_ask": num(mk.get("bestAsk"), None), "yes_bid": num(mk.get("bestBid"), None),
                "question": mk.get("question", e["title"]),
            })
        if buckets:
            out.append({"kind": m.group(1).lower(), "city": m.group(2), "date": day,
                        "station": station.group(1), "title": e["title"],
                        "slug": e.get("slug"), "unit": buckets[0]["unit"],
                        "buckets": buckets})
    return out


# ---------- weather data ----------

def metar_obs(stations):
    """Routine METAR temperatures (C, tenths when the T-group is present) for
    the last 36h, plus each station's coordinates."""
    obs, coords = {}, {}
    st = sorted(stations)
    for i in range(0, len(st), 40):
        rows = get(METAR_URL, {"ids": ",".join(st[i:i + 40]), "hours": 36,
                               "format": "json"}) or []
        for r in rows:
            raw = r.get("rawOb") or ""
            if r.get("metarType") == "SPECI" or raw.startswith("SPECI"):
                continue  # special observations don't count
            temp = None
            t = re.search(r"\bT([01])(\d{3})[01]\d{3}\b", raw)
            if t:
                temp = (-1 if t.group(1) == "1" else 1) * int(t.group(2)) / 10
            else:
                b = re.search(r"\s(M?\d{2})/(M?\d{2})?\s", raw + " ")
                if b:
                    temp = -int(b.group(1)[1:]) if b.group(1).startswith("M") else int(b.group(1))
            if temp is None:
                continue
            sid = r.get("icaoId")
            obs.setdefault(sid, []).append((int(r.get("obsTime") or 0), temp))
            coords[sid] = (r.get("lat"), r.get("lon"))
    return obs, coords


def forecasts(stations, coords):
    """Daily max/min from GFS and ECMWF at each station, plus its time zone."""
    st = [s for s in sorted(stations) if coords.get(s) and None not in coords[s]]
    out = {}
    for i in range(0, len(st), 12):
        chunk = st[i:i + 12]
        rows = get(FORECAST_URL, {
            "latitude": ",".join(str(coords[s][0]) for s in chunk),
            "longitude": ",".join(str(coords[s][1]) for s in chunk),
            "daily": "temperature_2m_max,temperature_2m_min",
            "models": "gfs_seamless,ecmwf_ifs025",
            "timezone": "auto", "forecast_days": 4}, timeout=40)
        if isinstance(rows, dict):
            rows = [rows]
        for s, r in zip(chunk, rows or []):
            d = r.get("daily") or {}
            days = {}
            for k, day in enumerate(d.get("time", [])):
                vals = {}
                for kind, key in (("highest", "max"), ("lowest", "min")):
                    v = [d.get(f"temperature_2m_{key}_{m}", [None] * 9)[k]
                         for m in ("gfs_seamless", "ecmwf_ifs025")]
                    v = [x for x in v if x is not None]
                    if v:
                        vals[kind] = sum(v) / len(v)
                days[day] = vals
            out[s] = {"tz": r.get("timezone"), "days": days}
    return out


# ---------- order book ----------

def fill(token, dollars):
    """Walk the asks like a real market order. Returns (avg price, shares,
    fee) for spending `dollars`, or None if the book can't fill it."""
    book = get(BOOK_URL, {"token_id": token})
    if not book:
        return None
    asks = sorted((num(a.get("price")), num(a.get("size"))) for a in book.get("asks", []))
    left, shares, fee = dollars, 0.0, 0.0
    for price, size in asks:
        if price <= 0 or price >= 1:
            continue
        take = min(size, left / price)
        shares += take
        fee += take * FEE_RATE * price * (1 - price)
        left -= take * price
        if left <= 1e-9:
            return dollars / shares, shares, fee
    return None


def make_bet(strategy, event, bucket, side, reason):
    token = bucket["yes"] if side == "YES" else bucket["no"]
    got = fill(token, BET_SIZE)
    if not got:
        return None
    ask, shares, fee = got
    return {"strategy": strategy, "asset": token, "condition": bucket["condition"],
            "event": event["slug"], "title": bucket["question"], "outcome": side,
            "bucket": bucket["label"], "price": ask, "shares": shares,
            "stake": BET_SIZE, "fee": fee, "profit_if_win": shares - BET_SIZE - fee,
            "reason": reason}


# ---------- strategies ----------

def local_now(tz):
    try:
        return now_utc().astimezone(ZoneInfo(tz))
    except Exception:
        return None


def observed_extreme(event, obs, tz):
    """Highest/lowest reading so far on the event's local date (market unit)."""
    vals = []
    for ts, temp in obs.get(event["station"], []):
        t = datetime.fromtimestamp(ts, timezone.utc).astimezone(ZoneInfo(tz))
        if t.date() == event["date"]:
            vals.append(temp)
    if not vals:
        return None
    ext = max(vals) if event["kind"] == "highest" else min(vals)
    return to_unit(ext, event["unit"])


def dead_signals(events, obs, fc):
    out = []
    for e in events:
        tz = (fc.get(e["station"]) or {}).get("tz")
        now = local_now(tz) if tz else None
        if not now or now.date() != e["date"]:
            continue  # only today's markets, in the station's own time zone
        ext = observed_extreme(e, obs, tz)
        if ext is None:
            continue
        picks = []
        for b in e["buckets"]:
            if e["kind"] == "highest":
                dead = b["hi"] is not None and b["hi"] <= ext - DEAD_MARGIN
            else:
                dead = b["lo"] is not None and b["lo"] >= ext + DEAD_MARGIN
            if not dead or b["yes_bid"] is None:
                continue
            no_ask = 1 - b["yes_bid"]           # quick screen from Gamma
            if no_ask <= DEAD_MAX_PRICE:
                picks.append((no_ask, b))
        for _, b in sorted(picks, key=lambda x: x[0])[:DEAD_PER_EVENT]:
            out.append(("DEAD", e, b, "NO",
                        f"observed {e['kind'][:4]} so far {ext}°{e['unit']}"))
    return out


def late_signals(events, obs, fc):
    out = []
    for e in events:
        tz = (fc.get(e["station"]) or {}).get("tz")
        now = local_now(tz) if tz else None
        if not now or now.date() != e["date"]:
            continue
        start = LATE_HIGH_FROM if e["kind"] == "highest" else LATE_LOW_FROM
        if now.hour < start:
            continue
        ext = observed_extreme(e, obs, tz)
        if ext is None:
            continue
        picks = []
        for b in e["buckets"]:
            if e["kind"] == "highest":   # buckets ABOVE today's high so far
                far = b["lo"] is not None and b["lo"] >= ext + LATE_GAP
            else:                        # buckets BELOW today's low so far
                far = b["hi"] is not None and b["hi"] <= ext - LATE_GAP
            if not far or b["yes_bid"] is None:
                continue
            no_ask = 1 - b["yes_bid"]
            if LATE_MIN_PRICE <= no_ask <= LATE_MAX_PRICE:
                picks.append((no_ask, b))
        for _, b in sorted(picks, key=lambda x: x[0])[:LATE_PER_EVENT]:
            out.append(("LATE", e, b, "NO",
                        f"{e['kind'][:4]} so far {ext}°{e['unit']} at local {now:%H:%M}"))
    return out


def shift_signals(events, fc, state):
    out = []
    if not SHIFT_ENABLED:
        return out
    seen = state.setdefault("forecast", {})
    for e in events:
        f = fc.get(e["station"])
        if not f:
            continue
        now = local_now(f["tz"])
        if not now or (e["date"] - now.date()).days not in SHIFT_DAYS_AHEAD:
            continue
        new = (f["days"].get(e["date"].isoformat()) or {}).get(e["kind"])
        if new is None:
            continue
        key = f"{e['station']}|{e['date']}|{e['kind']}"
        old = seen.get(key)
        seen[key] = new
        if old is None or abs(new - old) < SHIFT_MIN_MOVE_C:
            continue
        # Forecasts are biased per station, so use only the CHANGE: take the
        # market's favourite bucket and step one bucket in the move's direction.
        ladder = sorted(e["buckets"], key=lambda b: (b["lo"] is not None,
                                                     b["lo"] if b["lo"] is not None else 0))
        priced = [b for b in ladder if b["yes_bid"] is not None]
        if not priced:
            continue
        fav = max(priced, key=lambda b: b["yes_bid"])
        i = ladder.index(fav) + (1 if new > old else -1)
        if not 0 <= i < len(ladder):
            continue
        b = ladder[i]
        if b["yes_ask"] is not None and SHIFT_MIN_PRICE <= b["yes_ask"] <= SHIFT_MAX_PRICE:
            out.append(("SHIFT", e, b, "YES",
                        f"forecast {old:.1f}→{new:.1f}°C, favourite {fav['label']}"))
    # forget dates that have passed
    today = now_utc().date().isoformat()
    for k in [k for k in seen if k.split("|")[1] < today]:
        del seen[k]
    return out


def favourite_signals(strategy, events, fc):
    rule = FAV_RULES[strategy]
    out = []
    for e in events:
        if e["kind"] != rule["kind"] or e["unit"] not in rule["units"]:
            continue
        tz = (fc.get(e["station"]) or {}).get("tz")
        now = local_now(tz) if tz else None
        if not now or now.date() != e["date"] or \
                not rule["hours"][0] <= now.hour < rule["hours"][1]:
            continue
        priced = [b for b in e["buckets"] if b["yes_bid"] is not None and b["yes_ask"] is not None]
        if not priced:
            continue
        fav = max(priced, key=lambda b: b["yes_bid"] + b["yes_ask"])
        mid = (fav["yes_bid"] + fav["yes_ask"]) / 2
        if FAV_MIN_PRICE <= mid <= FAV_MAX_PRICE:
            out.append((strategy, e, fav, "YES",
                        f"favourite {fav['label']} mid {mid:.2f}, local {now:%H:%M}"))
    return out


# ---------- ledgers ----------

def ledger_file(strategy):
    return f"ledger_wx_{strategy.lower()}.json"


def settle(ledger):
    msgs = []
    open_bets = [b for b in ledger["bets"] if b["status"] == "open"]
    rows = pmap(lambda b: get(f"{GAMMA_API}/markets", {"condition_ids": b["condition"],
                                                       "closed": "true"}) or [], open_bets)
    for bet, r in zip(open_bets, rows):
        if not r or not r[0].get("closed"):
            continue
        try:
            ids = json.loads(r[0].get("clobTokenIds") or "[]")
            prices = [num(x) for x in json.loads(r[0].get("outcomePrices") or "[]")]
            if max(prices) < 0.99:
                continue
            won = prices[ids.index(bet["asset"])] > 0.99
        except (ValueError, IndexError):
            continue
        payout = bet["shares"] if won else 0.0
        ledger["cash"] += payout
        bet["status"] = "won" if won else "lost"
        bet["pnl"] = payout - bet["stake"] - bet["fee"]
        bet["closed"] = now_utc().isoformat()
        msgs.append(f"{'✅ WON' if won else '❌ LOST'} {bet['outcome']} {bet['bucket']} | "
                    f"{bet['title']} | P&L ${bet['pnl']:+.2f}")
    return msgs


def open_bets(ledger, signals):
    msgs = []
    open_ = [b for b in ledger["bets"] if b["status"] in ("open", "resting")]
    held_markets = {b["condition"] for b in open_}
    for strategy, e, b, side, reason in signals:
        if len(open_) >= MAX_OPEN_BETS or ledger["cash"] < BET_SIZE + 0.05:
            break
        if b["condition"] in held_markets:
            continue
        if strategy in FAV_RULES and any(x["event"] == e["slug"] for x in ledger["bets"]):
            continue  # one favourite bet per event, ever
        bet = make_bet(strategy, e, b, side, reason)
        if strategy in FAV_RULES and (not bet or bet["price"] > FAV_MAX_FILL):
            rest = resting_bid(strategy, e, b, reason)
            if rest:
                ledger["cash"] -= rest["stake"]      # reserve the money
                ledger["bets"].append(rest)
                open_.append(rest)
                held_markets.add(b["condition"])
                msgs.append(f"⏳ Resting bid YES {b['label']} @ {rest['price']:.2f} | "
                            f"{rest['title']} | {reason}")
            continue
        if not bet:
            continue
        if strategy == "DEAD" and (bet["price"] > DEAD_MAX_PRICE or
                                   bet["profit_if_win"] / bet["stake"] < DEAD_MIN_RETURN):
            continue
        if strategy == "LATE" and not LATE_MIN_PRICE - 0.02 <= bet["price"] <= LATE_MAX_PRICE:
            continue
        if strategy == "SHIFT" and not SHIFT_MIN_PRICE <= bet["price"] <= SHIFT_MAX_PRICE:
            continue
        if strategy in FAV_RULES and bet["price"] > FAV_MAX_FILL:
            continue  # the real fill is too expensive for the edge
        bet.update(status="open", opened=now_utc().isoformat())
        ledger["cash"] -= bet["stake"] + bet["fee"]
        ledger["bets"].append(bet)
        open_.append(bet)
        held_markets.add(b["condition"])
        msgs.append(f"📝 {side} {b['label']} @ {bet['price']:.3f} | {bet['title']} | "
                    f"{reason} | win +${bet['profit_if_win']:.2f}")
    return msgs


def resting_bid(strategy, e, b, reason):
    if b["yes_bid"] is None:
        return None
    price = round(b["yes_bid"] + 0.01, 2)
    if not FAV_MIN_PRICE <= price <= FAV_MAX_PRICE:
        return None
    end_hour = FAV_RULES[strategy]["hours"][1]
    tz = ZoneInfo(e["tz"])
    expires = datetime(e["date"].year, e["date"].month, e["date"].day, end_hour, tzinfo=tz)
    shares = BET_SIZE / price
    return {"strategy": strategy, "asset": b["yes"], "condition": b["condition"],
            "event": e["slug"], "title": b["question"], "outcome": "YES",
            "bucket": b["label"], "price": price, "shares": shares, "stake": BET_SIZE,
            "fee": 0.0, "profit_if_win": shares - BET_SIZE, "reason": reason,
            "order": "maker", "status": "resting", "placed": now_utc().isoformat(),
            "expires": expires.astimezone(timezone.utc).isoformat()}


def check_resting(ledger, asks, active):
    """asks: YES token -> current best ask (only where a seller exists);
    active: every YES token still trading in this run's market scan."""
    msgs = []
    now = now_utc().isoformat()
    for bet in ledger["bets"]:
        if bet["status"] != "resting":
            continue
        ask = asks.get(bet["asset"])
        if ask is not None and ask <= bet["price"]:
            bet["status"] = "open"
            bet["opened"] = now
            msgs.append(f"✅ Bid filled: YES {bet['bucket']} @ {bet['price']:.2f} | {bet['title']}")
        elif now >= bet["expires"] or bet["asset"] not in active:
            bet["status"] = "cancelled"
            ledger["cash"] += bet["stake"]
    return msgs


def summary(ledger):
    done = [b for b in ledger["bets"] if b["status"] in ("won", "lost")]
    wins = sum(1 for b in done if b["status"] == "won")
    open_n = sum(1 for b in ledger["bets"] if b["status"] == "open")
    resting = sum(1 for b in ledger["bets"] if b["status"] == "resting")
    unfilled = sum(1 for b in ledger["bets"] if b["status"] == "cancelled")
    extra = f" | resting {resting}, unfilled {unfilled}" if resting or unfilled else ""
    return (f"💰 Cash ${ledger['cash']:.2f} | open {open_n} | record {wins}-{len(done) - wins}"
            f" | realized P&L ${sum(b['pnl'] for b in done):+.2f}{extra}")


def notify(lines):
    url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not url or not lines:
        return
    text = "\n".join(lines)
    for i in range(0, len(text), 1900):
        requests.post(url, json={"content": text[i:i + 1900]}, timeout=10)


if __name__ == "__main__":
    state = read_json(STATE_FILE, {})
    events = load_events()
    stations = {e["station"] for e in events}
    print(f"{len(events)} temperature events, {len(stations)} stations")

    obs, coords = metar_obs(stations)
    coords = {**state.get("coords", {}), **{k: list(v) for k, v in coords.items()}}
    state["coords"] = coords
    fc = forecasts(stations, coords)
    # Time zones never change: remember them, so a failed forecast batch
    # doesn't knock cities out of the strategies that need local time.
    tzs = state.setdefault("tz", {})
    for sid, f in fc.items():
        if f.get("tz"):
            tzs[sid] = f["tz"]
    for sid in stations:
        if sid not in fc and sid in tzs:
            fc[sid] = {"tz": tzs[sid], "days": {}}
    print(f"METAR for {len(obs)} stations, forecasts for {len(fc)}")

    for e in events:
        e["tz"] = (fc.get(e["station"]) or {}).get("tz") or "UTC"
    asks = {b["yes"]: b["yes_ask"] for e in events for b in e["buckets"]
            if b["yes_ask"] is not None}
    active = {b["yes"] for e in events for b in e["buckets"]}

    signals = {
        "DEAD": dead_signals(events, obs, fc),
        "LATE": late_signals(events, obs, fc),
        "SHIFT": shift_signals(events, fc, state),
        "MORNING": favourite_signals("MORNING", events, fc),
        "AFTERNOON": favourite_signals("AFTERNOON", events, fc),
    }
    out = []
    for strat in STRATEGIES:
        led = read_json(ledger_file(strat), {"cash": START_BANKROLL, "bets": []})
        msgs = check_resting(led, asks, active) + settle(led) + open_bets(led, signals[strat])
        write_json(ledger_file(strat), led)
        print(f"{strat}: {len(signals[strat])} signal(s) | {summary(led)}")
        if msgs:
            out += [f"**[WX-{strat}]**"] + msgs + [summary(led)]
    write_json(STATE_FILE, state)
    print("\n".join(out))
    notify(out)
