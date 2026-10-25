import os
import time
import requests

GAMMA_URL = "https://gamma-api.polymarket.com/events"
MIN_EDGE = 0.01  # ignore anything under 1 cent of profit per set


def fetch_events():
    """Page through all active, open events."""
    offset, limit = 0, 100
    while True:
        resp = requests.get(
            GAMMA_URL,
            params={"active": "true", "closed": "false",
                    "limit": limit, "offset": offset},
            timeout=20,
        )
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        yield from batch
        offset += limit
        time.sleep(0.2)


def to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def scan():
    hits = []
    for event in fetch_events():
        if not event.get("negRisk"):
            continue

        markets = [m for m in event.get("markets", [])
                   if m.get("active") and not m.get("closed")]
        if len(markets) < 2:
            continue

        asks = [to_float(m.get("bestAsk")) for m in markets]
        bids = [to_float(m.get("bestBid")) for m in markets]
        if any(a is None or a <= 0 for a in asks) or None in bids:
            continue

        n = len(markets)
        title = event.get("title", "?")

        yes_cost = sum(asks)
        yes_profit = 1 - yes_cost
        if yes_profit >= MIN_EDGE:
            hits.append(("BUY ALL YES", title, n, yes_cost, yes_profit))

        no_cost = sum(1 - b for b in bids)
        no_profit = (n - 1) - no_cost
        if no_profit >= MIN_EDGE:
            hits.append(("BUY ALL NO", title, n, no_cost, no_profit))

    return sorted(hits, key=lambda h: h[4], reverse=True)


def notify(lines):
    url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not url or not lines:
        return
    message = "\n".join(lines)[:1900]
    requests.post(url, json={"content": message}, timeout=10)


if __name__ == "__main__":
    results = scan()
    lines = [
        f"{s} | {n} outcomes | cost ${c:.3f} | profit ${p:.3f} ({p / c:.1%}) | {t}"
        for s, t, n, c, p in results
    ]
    print("\n".join(lines) or "No opportunities above the threshold right now.")
    notify(lines)
