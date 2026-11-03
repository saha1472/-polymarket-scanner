import os
import sys
import time
import requests

import simcore

GAMMA_URL = "https://gamma-api.polymarket.com/events/keyset"
MAX_PAGES = 300  # 100 events a page
MIN_EDGE = 0.01  # ignore anything under 1 cent of profit per set


def fetch_events():
    """Page through all open events, stopping cleanly on errors.
    Gamma's plain /events stops at offset ~2000 (it answers 422 beyond that),
    so this uses the keyset endpoint and its cursor."""
    cursor = None
    for page in range(MAX_PAGES):
        params = {"closed": "false", "limit": 100}
        if cursor:
            params["after_cursor"] = cursor
        try:
            resp = requests.get(GAMMA_URL, params=params, timeout=20)
        except requests.RequestException as err:
            print(f"Network error on page {page}: {err}")
            break
        if resp.status_code != 200:
            print(f"API returned {resp.status_code} on page {page}: {resp.text[:200]}")
            break
        body = resp.json()
        yield from body.get("events") or []
        cursor = body.get("next_cursor")
        if not cursor:
            break
        time.sleep(0.1)


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
        if event.get("negRiskAugmented"):
            continue  # open-ended ("who will X fight next"): the listed outcomes
                      # aren't the full set, so buying them all is not a sure $1

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
    """True if delivered (or nothing to send / no webhook configured)."""
    url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not url or not lines:
        return True
    return simcore.post_discord(url, "\n".join(lines))


if __name__ == "__main__":
    results = scan()
    lines = [
        f"{s} | {n} outcomes | cost ${c:.3f} | profit ${p:.3f} ({p / c:.1%}) | {t}"
        for s, t, n, c, p in results
    ]
    print("\n".join(lines) or "No opportunities above the threshold right now.")
    if not notify(lines):
        sys.exit("Discord delivery failed. Failing the run so it's visible.")
