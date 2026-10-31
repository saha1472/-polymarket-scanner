"""SHADOW EXPERIMENT - never trades, never touches a ledger.

At a few fixed local times each day, work out an ensemble-model probability
for every bucket of today's temperature markets, and log it next to the
market's prices at that moment. wx_ensemble_eval.py later scores the model
against the market on the official results.

How a probability is made:
  - Open-Meteo ensemble forecasts at the airport: ECMWF (51 members) and
    NOAA GEFS (31 members), hourly 2 m temperature.
  - For each member: the day's extreme = the airport's routine METAR readings
    so far today, combined with that member's forecast for the remaining
    hours of the local day.
  - Convert to the market unit and round half up, like the market does, then
    count members per bucket.

This is a model estimate, not a "true" probability: members share biases, the
grid point isn't the runway, and hourly values can miss a peak. That's exactly
what the evaluation is for. Thresholds below (8c edge, 0.85 cap) are Argon's
suggestions, recorded only as a hypothesis to test.
"""
import json
import os
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import simcore
import weather as wx

ENSEMBLE_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"
MODELS = {"ecmwf_ifs025": "ecmwf", "gfs025": "gefs"}
# how each model's member columns end in the response
SUFFIX = {"ecmwf": "_ecmwf_ifs025_ensemble", "gefs": "_ncep_gefs025"}
LOG_FILE = "wx_shadow.jsonl"
STATE_FILE = "wx_state.json"            # written by weather.py earlier in the run
CHECKPOINTS = {"highest": (10, 15), "lowest": (8,)}   # local hours
CHECKPOINT_WINDOW_H = 3                 # a checkpoint stays due this long
BATCH = 10                              # locations per ensemble request
HYPO_EDGE = 0.08                        # hypothesis only, never acted on
HYPO_MAX_ASK = 0.85


# ---------- pure helpers (tested) ----------

def logged_keys(lines):
    """(slug, checkpoint) pairs already in the log."""
    keys = set()
    for line in lines:
        try:
            r = json.loads(line)
            keys.add((r["slug"], r["checkpoint"]))
        except (ValueError, KeyError, TypeError):
            continue
    return keys


def due_checkpoints(events, tzs, done, now):
    """[(event, checkpoint hour)] that should be logged on this run."""
    out = []
    for e in events:
        tz = tzs.get(e["station"])
        if not tz:
            continue
        local = (now or wx.now_utc()).astimezone(ZoneInfo(tz))
        if local.date() != e["date"]:
            continue
        for cp in CHECKPOINTS.get(e["kind"], ()):
            if cp <= local.hour < cp + CHECKPOINT_WINDOW_H and (e["slug"], cp) not in done:
                out.append((e, cp))
                break                      # one snapshot per event per run
    return out


def observed_raw(event, obs, tz):
    """Today's extreme so far in raw degrees C (not rounded), or None. Same
    whole-day coverage rule as weather.observed_extreme."""
    days = [(datetime.fromtimestamp(ts, timezone.utc).astimezone(ZoneInfo(tz)), t)
            for ts, t in obs.get(event["station"], [])]
    today = [(lt, t) for lt, t in days if lt.date() == event["date"]]
    if not today or min(lt for lt, _ in today).hour >= wx.DAY_START_SLACK_H:
        return None
    vals = [t for _, t in today]
    return max(vals) if event["kind"] == "highest" else min(vals)


def member_extremes(times, members, date, local_now_naive, kind, observed):
    """Each member's day extreme in C: readings so far + its remaining hours.
    `times` are local ISO hours ('2026-10-31T14:00'); members is a list of
    value lists aligned with `times`."""
    pick = max if kind == "highest" else min
    day = date.isoformat()
    later = [i for i, t in enumerate(times)
             if t[:10] == day and datetime.fromisoformat(t) > local_now_naive]
    out = []
    for vals in members:
        future = [vals[i] for i in later if vals[i] is not None]
        pool = future + ([observed] if observed is not None else [])
        if pool:
            out.append(pick(pool))
    return out


def bucket_of(value, buckets):
    for b in buckets:
        if (b["lo"] is None or value >= b["lo"]) and (b["hi"] is None or value <= b["hi"]):
            return b["label"]
    return None


def bucket_probs(extremes_c, buckets, unit):
    """{label: share of members}, plus None -> share outside the listed buckets."""
    counts = {}
    for x in extremes_c:
        lab = bucket_of(wx.to_unit(x, unit), buckets)
        counts[lab] = counts.get(lab, 0) + 1
    n = len(extremes_c)
    return {k: v / n for k, v in counts.items()} if n else {}


def record(event, cp, local, observed, obs_age, per_model):
    """One log line. per_model = {"ecmwf": [extremes], "gefs": [extremes]}."""
    pooled = [x for xs in per_model.values() for x in xs]
    p_all = bucket_probs(pooled, event["buckets"], event["unit"])
    p_mod = {m: bucket_probs(xs, event["buckets"], event["unit"]) for m, xs in per_model.items()}
    rows = []
    for b in event["buckets"]:
        p = p_all.get(b["label"], 0.0)
        ask = b["yes_ask"]
        rows.append({
            "label": b["label"], "yes": b["yes"], "condition": b["condition"],
            "p": round(p, 4),
            **{f"p_{m}": round(p_mod[m].get(b["label"], 0.0), 4) for m in p_mod},
            "bid": b["yes_bid"], "ask": ask,
            "hypo_buy": ask is not None and 0 < ask <= HYPO_MAX_ASK and p - ask >= HYPO_EDGE,
        })
    return {
        "time": wx.now_utc().isoformat(), "slug": event["slug"], "city": event["city"],
        "station": event["station"], "kind": event["kind"], "unit": event["unit"],
        "date": event["date"].isoformat(), "checkpoint": cp, "local": local.strftime("%H:%M"),
        "observed_c": observed, "obs_age_h": None if obs_age is None else round(obs_age, 2),
        "members": {m: len(xs) for m, xs in per_model.items()},
        "p_unlisted": round(p_all.get(None, 0.0), 4),
        "settings": {"edge": HYPO_EDGE, "max_ask": HYPO_MAX_ASK, "models": list(MODELS)},
        "code": simcore.code_version(),
        "buckets": rows,
    }


def split_members(hourly):
    """{'ecmwf': [values per member], 'gefs': [...]} from an ensemble response."""
    out = {name: [] for name in SUFFIX}
    for key, vals in hourly.items():
        if key.startswith("temperature_2m"):
            for name, suffix in SUFFIX.items():
                if key.endswith(suffix):
                    out[name].append(vals)
    return out


# ---------- network ----------

def fetch_ensembles(stations, coords):
    out = {}
    st = [s for s in sorted(stations) if coords.get(s) and None not in coords[s]]
    for i in range(0, len(st), BATCH):
        chunk = st[i:i + BATCH]
        rows = wx.get(ENSEMBLE_URL, {
            "latitude": ",".join(str(coords[s][0]) for s in chunk),
            "longitude": ",".join(str(coords[s][1]) for s in chunk),
            "hourly": "temperature_2m", "models": ",".join(MODELS),
            "timezone": "auto", "forecast_days": 2}, timeout=60)
        if isinstance(rows, dict):
            rows = [rows]
        for s, r in zip(chunk, rows or []):
            h = (r or {}).get("hourly") or {}
            if h.get("time"):
                out[s] = {"times": h["time"], "members": split_members(h)}
    return out


def append_lines(path, records):
    old = ""
    if os.path.exists(path):
        with open(path) as fh:
            old = fh.read()
    new = "".join(json.dumps(r, allow_nan=False) + "\n" for r in records)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w") as fh:
        fh.write(old + new)
    os.replace(tmp, path)


def main():
    state = wx.read_json(STATE_FILE, {})
    tzs, coords = state.get("tz", {}), state.get("coords", {})
    lines = open(LOG_FILE).read().splitlines() if os.path.exists(LOG_FILE) else []
    events = wx.load_events()
    due = due_checkpoints(events, tzs, logged_keys(lines), None)
    if not due:
        print(f"Ensemble shadow: nothing due ({len(events)} events, {len(lines)} logged so far)")
        return 0
    stations = {e["station"] for e, _ in due}
    obs, fresh_coords = wx.metar_obs(stations)
    coords = {**coords, **{k: list(v) for k, v in fresh_coords.items()}}
    ens = fetch_ensembles(stations, coords)
    recs, missing = [], 0
    for e, cp in due:
        f = ens.get(e["station"])
        if not f:
            missing += 1
            continue
        tz = tzs[e["station"]]
        local = wx.now_utc().astimezone(ZoneInfo(tz))
        observed = observed_raw(e, obs, tz)
        if observed is None:      # can't see the whole day so far: don't guess
            missing += 1
            continue
        per_model = {m: member_extremes(f["times"], vals, e["date"],
                                        local.replace(tzinfo=None), e["kind"], observed)
                     for m, vals in f["members"].items()}
        if not any(per_model.values()):
            missing += 1
            continue
        recs.append(record(e, cp, local, observed,
                           wx.obs_age_hours(e["station"], obs), per_model))
    if recs:
        append_lines(LOG_FILE, recs)
    hypo = sum(b["hypo_buy"] for r in recs for b in r["buckets"])
    print(f"Ensemble shadow: logged {len(recs)} snapshot(s), {missing} without data, "
          f"{hypo} hypothetical buy(s) at edge >= {HYPO_EDGE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
