"""Is the scanner alive?  READ-ONLY: it looks at the scan workflow's recent runs and says whether to raise the alarm.

The scan runs every 15 minutes (measured: median gap 15 minutes, longest 20 in three days). If no run has
completed successfully for an hour, something is wrong that the scan itself cannot report (GitHub's schedule
stopped, the trigger stopped, the workflow got disabled). The hourly Watchdog workflow runs this and posts
the text it prints to Discord.

Usage:  python watchdog.py runs.json      (the "workflow_runs" list from the GitHub API)
Prints the alert text, or nothing when all is well. Never fails the run.
"""
import json
import sys
from datetime import datetime, timezone

STALE_MIN = 60              # no successful run for this long: alarm
RUNNING_GRACE_MIN = 45      # a run that started this recently and has not finished still counts as alive
# The check runs hourly. Alarm in the first hour of silence, then remind once after each of these many hours
# of silence, so a long outage is not one message every hour.
REMIND_AT_HOURS = (6, 24, 48, 72)


def parse(ts):
    try:
        t = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def in_alarm_window(age_min):
    """True in the first hour after STALE_MIN, and in the hour after each reminder time."""
    if STALE_MIN <= age_min < 2 * STALE_MIN:
        return True
    return any(h * 60 <= age_min < h * 60 + 60 for h in REMIND_AT_HOURS)


def check(runs, now=None):
    """None when all is well (or this is not a time to speak), else the alert text."""
    now = now or datetime.now(timezone.utc)
    runs = [r for r in runs if isinstance(r, dict)]
    for r in runs:                                          # something is running right now: alive
        if r.get("status") in ("in_progress", "queued", "waiting", "pending", "requested"):
            started = parse(r.get("created_at"))
            if started and (now - started).total_seconds() / 60 < RUNNING_GRACE_MIN:
                return None
    done = [parse(r.get("updated_at")) for r in runs if r.get("conclusion") == "success"]
    done = [t for t in done if t]
    if not done:
        return ("⚠️ The scanner watchdog found no successful scan run among the last "
                f"{len(runs)} runs. Check the Actions page.")
    age = (now - max(done)).total_seconds() / 60
    if age < STALE_MIN or not in_alarm_window(age):
        return None
    hours, minutes = divmod(int(age), 60)
    return (f"⚠️ The scanner has not completed a run for {hours} h {minutes:02d} min "
            f"(last good run finished {max(done):%Y-%m-%d %H:%M} UTC). The 15-minute trigger or the workflow may have "
            "stopped: check the Actions page.")


def main(argv):
    try:
        with open(argv[1], encoding="utf-8") as fh:
            runs = json.load(fh)
        runs = runs if isinstance(runs, list) else []
    except (IndexError, OSError, ValueError):
        runs = None
    if runs is None:
        print("⚠️ The scanner watchdog could not read the list of scan runs from GitHub.")
        return 0
    msg = check(runs)
    if msg:
        print(msg)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
