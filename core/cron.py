"""Cron expression matching and schedule conditions.

Extracted verbatim from the retired dashboard scheduler (2026-08-21, the
:3457 NiceGUI dashboard is retired; see CLAUDE.md Runtime Surfaces). The
SQLite `scheduled_tasks` execution loop that lived beside these helpers had
already lost its last runtime caller (core/heartbeat.py retired the
dynamic-task path; the `schedule_task` bot action was replaced) and was
deleted with the dashboard. What remains here is the live cron primitive
used by core.intentions (next-fire computation, catch-up, conditions) and
core.routines (trigger validation).
"""

import json
from datetime import datetime, timedelta

from core.timeutil import now_local


def _coerce(dt: datetime) -> datetime:
    """Make `dt` comparable to now_local() regardless of tz-awareness.

    Stored timestamps (last_run_at, date-trigger datetime) are local-time
    strings written WITHOUT an offset, so datetime.fromisoformat() returns a
    *naive* datetime — while now_local() is tz-aware. Comparing the two raises
    TypeError, which killed the entire due-check. Mirrors core/intentions.py.
    """
    ref = now_local()
    if ref.tzinfo is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=ref.tzinfo)
    if ref.tzinfo is None and dt.tzinfo is not None:
        return dt.replace(tzinfo=None)
    return dt


def _parse_cron_field(field_str: str, min_val: int, max_val: int) -> set[int]:
    """Parse a single cron field into a set of valid values.

    Out-of-range values raise ValueError instead of parsing into a set that
    can never match — "60 * * * *" used to register fine and then silently
    never fire, the exact silent-no-op class trigger validation exists to
    stop.
    """
    def _bounded(raw: str) -> int:
        v = int(raw)
        if not min_val <= v <= max_val:
            raise ValueError(f"cron value {v} outside {min_val}-{max_val}")
        return v

    values = set()
    for part in field_str.split(","):
        if part == "*":
            values.update(range(min_val, max_val + 1))
        elif "/" in part:
            base, step = part.split("/")
            step = int(step)
            if step <= 0:
                raise ValueError(f"cron step must be positive, got {step}")
            start = min_val if base == "*" else _bounded(base)
            values.update(range(start, max_val + 1, step))
        elif "-" in part:
            lo, hi = part.split("-")
            values.update(range(_bounded(lo), _bounded(hi) + 1))
        else:
            values.add(_bounded(part))
    return values


def validate_trigger(trigger_type: str, trigger_config) -> str | None:
    """Validate a trigger at registration time. Returns error message or None.

    Keeps malformed rows (bad JSON, bad cron, unparseable datetime) out of
    the `scheduled_tasks` table so a poison row is never evaluated (and
    skipped, loudly) on every due-check forever.
    """
    if isinstance(trigger_config, str):
        try:
            trigger_config = json.loads(trigger_config)
        except (json.JSONDecodeError, ValueError):
            return "trigger_config is not valid JSON"
    if not isinstance(trigger_config, dict):
        return "trigger_config must be a JSON object"

    if trigger_type == "cron":
        expr = trigger_config.get("expression", "")
        parts = str(expr).strip().split()
        if len(parts) != 5:
            return f"cron expression must have 5 fields, got {len(parts)}: {expr!r}"
        bounds = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 7)]
        for field, (lo, hi) in zip(parts, bounds):
            try:
                values = _parse_cron_field(field, lo, hi)
            except ValueError:
                return f"malformed cron field {field!r} in {expr!r}"
            if not values:
                return f"cron field {field!r} matches nothing in {expr!r}"
    elif trigger_type == "interval":
        seconds = trigger_config.get("seconds", 600)
        if not isinstance(seconds, (int, float)) or seconds <= 0:
            return f"interval seconds must be a positive number, got {seconds!r}"
    elif trigger_type == "date":
        target = trigger_config.get("datetime", "")
        try:
            datetime.fromisoformat(target)
        except (TypeError, ValueError):
            return f"date trigger needs an ISO datetime, got {target!r}"
    else:
        return f"unknown trigger_type {trigger_type!r}"
    return None


def cron_matches(expression: str, dt: datetime | None = None) -> bool:
    """Check if a cron expression matches the given datetime.

    dow uses STANDARD cron semantics: 0=Sunday…6=Saturday, 7 also Sunday.
    (Previously compared against dt.weekday() where 0=Monday, shifting every
    weekly schedule one day late — live misfire int_fb4fcab91d '30 14 * * 2'
    executed on a Wednesday and self-diagnosed it in last_error.)
    """
    if dt is None:
        dt = now_local()
    parts = expression.strip().split()
    if len(parts) != 5:
        return False
    minute, hour, dom, month, dow = parts
    checks = [
        (minute, dt.minute, 0, 59, False),
        (hour, dt.hour, 0, 23, False),
        (dom, dt.day, 1, 31, False),
        (month, dt.month, 1, 12, False),
        (dow, (dt.weekday() + 1) % 7, 0, 7, True),  # standard cron: 0/7=Sunday
    ]
    for field_str, current, min_v, max_v, is_dow in checks:
        allowed = _parse_cron_field(field_str, min_v, max_v)
        if is_dow and 7 in allowed:
            allowed = allowed | {0}  # cron tolerance: 7 == Sunday == 0
        if current not in allowed:
            return False
    return True


def cron_next(expression: str, after: datetime | None = None,
              horizon_days: int = 366) -> datetime | None:
    """Next datetime strictly after `after` that matches the cron expression.

    Minute-resolution forward scan. Cron has minute granularity and our live
    expressions are sparse (daily/weekly), so the scan exits quickly; the
    horizon bounds pathological expressions. Returns None on malformed input
    or no match within the horizon.

    This is the catch-up primitive (REQ-32): intent firing compares
    now >= next_fire_at, so a missed minute fires on the NEXT check instead
    of silently losing the whole occurrence.
    """
    if after is None:
        after = now_local()
    if len(expression.strip().split()) != 5:
        return None
    probe = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
    end = after + timedelta(days=horizon_days)
    while probe <= end:
        if cron_matches(expression, probe):
            return probe
        probe += timedelta(minutes=1)
    return None


def check_conditions(conditions: list[dict], task: dict) -> bool:
    """Evaluate all conditions for a task. Returns True if all pass."""
    now = now_local()
    for cond in conditions:
        ctype = cond.get("type", "")

        if ctype == "time_window":
            start_h, start_m = map(int, cond["start"].split(":"))
            end_h, end_m = map(int, cond["end"].split(":"))
            start_min = start_h * 60 + start_m
            end_min = end_h * 60 + end_m
            current_min = now.hour * 60 + now.minute
            if start_min <= end_min:
                if not (start_min <= current_min <= end_min):
                    return False
            else:  # wraps midnight
                if end_min < current_min < start_min:
                    return False

        elif ctype == "not_already_done":
            window = cond.get("window", "today")
            last_run = task.get("last_run_at")
            if last_run:
                last_dt = _coerce(datetime.fromisoformat(last_run))
                if window == "today" and last_dt.date() == now.date():
                    return False
                elif window.endswith("h"):
                    hours = int(window[:-1])
                    if (now - last_dt) < timedelta(hours=hours):
                        return False
                elif window.endswith("m"):
                    minutes = int(window[:-1])
                    if (now - last_dt) < timedelta(minutes=minutes):
                        return False

        elif ctype == "weekday":
            days = cond.get("days", [])
            day_map = {"mon": 0, "tue": 1, "wed": 2, "thu": 3,
                       "fri": 4, "sat": 5, "sun": 6}
            allowed = {day_map[d.lower()] for d in days if d.lower() in day_map}
            if now.weekday() not in allowed:
                return False

        elif ctype == "user_awake":
            wake = cond.get("wake", "08:00")
            sleep = cond.get("sleep", "23:30")
            wake_min = int(wake.split(":")[0]) * 60 + int(wake.split(":")[1])
            sleep_min = int(sleep.split(":")[0]) * 60 + int(sleep.split(":")[1])
            current_min = now.hour * 60 + now.minute
            if not (wake_min <= current_min <= sleep_min):
                return False

    return True
