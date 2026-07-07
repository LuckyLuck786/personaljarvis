"""Tiny schedule spec parser + next-run math.

Two forms, deliberately minimal (no cron dependency):
    daily@HH:MM   — every day at local HH:MM
    everyN:<sec>  — every <sec> seconds from last run

Kept trivial on purpose; the hub doesn't need full cron and every dependency
is RAM we don't spend.
"""

from __future__ import annotations

from datetime import datetime, timedelta


def next_run_after(schedule: str, after: float) -> float:
    after_dt = datetime.fromtimestamp(after)
    if schedule.startswith("daily@"):
        hh, _, mm = schedule.removeprefix("daily@").partition(":")
        target = after_dt.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
        if target <= after_dt:
            target += timedelta(days=1)
        return target.timestamp()
    if schedule.startswith("everyN:"):
        return after + float(schedule.removeprefix("everyN:"))
    raise ValueError(f"unknown schedule spec: {schedule!r}")


def initial_next_run(schedule: str, now: float) -> float:
    if schedule.startswith("everyN:"):
        return now  # run once promptly, then on interval
    return next_run_after(schedule, now)
