"""Dynamic task scheduler — Trigger/Condition/Action model.

Runs alongside (not replacing) the static HEARTBEAT.md tasks.
Reads from SQLite `scheduled_tasks` table. LLM can register tasks
via the bot action system: [ACTION:schedule_task|...]

Trigger types:
  - cron: standard cron expression (minute hour dom month dow)
  - interval: every N seconds
  - date: one-shot at specific ISO datetime

Condition types:
  - time_window: only run between HH:MM and HH:MM
  - not_already_done: skip if ran within window (today/1h/etc)
  - weekday: only on specific days (mon,tue,...)
  - user_awake: skip before wake time / after sleep time

Action types:
  - prompt: run a Claude prompt via heartbeat
  - script: run a shell script
  - notify: send a message to Lark
"""

import json
import logging
import re
import time
from datetime import datetime, timedelta
from pathlib import Path

from core.timeutil import now_local, now_local_str

from .db import task_list, transaction

logger = logging.getLogger(__name__)

# Task ids already warned about, so a poison row logs once, not every ~10s.
_warned_task_ids: set[str] = set()


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


def validate_trigger(trigger_type: str, trigger_config) -> str | None:
    """Validate a trigger at registration time. Returns error message or None.

    Keeps malformed rows (bad JSON, bad cron, unparseable datetime) out of
    the table so they can't poison get_due_tasks().
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


def _parse_cron_field(field_str: str, min_val: int, max_val: int) -> set[int]:
    """Parse a single cron field into a set of valid values.

    Out-of-range values raise ValueError instead of parsing into a set that
    can never match — "60 * * * *" used to register fine and then silently
    never fire, the exact silent-no-op class validate_trigger exists to stop.
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


def _task_is_due(task: dict, now: datetime, now_ts: int) -> bool:
    """Trigger + condition check for a single task.

    Cron semantics: due only when the expression matches the CURRENT minute
    and last_run_at is not already within that minute (the heartbeat polls
    every ~10s; without the guard a task fired up to 6× in its minute).
    Missed minutes are dropped by design — no catch-up.
    """
    trigger_type = task["trigger_type"]
    trigger_config = json.loads(task["trigger_config"]) if isinstance(task["trigger_config"], str) else task["trigger_config"]
    conditions = json.loads(task["conditions"]) if isinstance(task["conditions"], str) else (task["conditions"] or [])

    triggered = False

    if trigger_type == "cron":
        expr = trigger_config.get("expression", "")
        triggered = cron_matches(expr, now)
        if triggered and task.get("last_run_at"):
            last_dt = _coerce(datetime.fromisoformat(task["last_run_at"]))
            if last_dt.replace(second=0, microsecond=0) == now.replace(second=0, microsecond=0):
                triggered = False  # already fired this minute

    elif trigger_type == "interval":
        seconds = trigger_config.get("seconds", 600)
        last_run = task.get("last_run_at")
        if last_run:
            last_ts = datetime.fromisoformat(last_run).timestamp()
            triggered = (now_ts - last_ts) >= seconds
        else:
            triggered = True  # Never run → run now

    elif trigger_type == "date":
        target = trigger_config.get("datetime", "")
        if target:
            target_dt = _coerce(datetime.fromisoformat(target))
            triggered = now >= target_dt
            # One-shot: never re-fire
            if triggered and task.get("last_run_at"):
                return False  # Already ran

    return triggered and check_conditions(conditions, task)


def get_due_tasks() -> list[dict]:
    """Get all tasks that should run now.

    Each task is evaluated in isolation: one malformed row (bad JSON,
    bad cron, unparseable timestamp) is logged once and skipped instead
    of killing the whole due-check.
    """
    tasks = task_list(enabled_only=True)
    now = now_local()
    now_ts = int(time.time())
    due = []

    for task in tasks:
        try:
            if _task_is_due(task, now, now_ts):
                due.append(task)
        except Exception as e:
            if task["id"] not in _warned_task_ids:
                _warned_task_ids.add(task["id"])
                logger.warning("skipping malformed scheduled task %s: %s",
                               task["id"], e)

    return due


def mark_executed(task_id: str, result: str = "") -> None:
    """Mark a task as executed (atomic — no read-then-write lost updates)."""
    now = now_local_str("%Y-%m-%dT%H:%M:%S")
    with transaction() as db:
        db.execute(
            """UPDATE scheduled_tasks
               SET run_count = COALESCE(run_count, 0) + 1,
                   last_run_at = ?, last_result = ?
               WHERE id = ?""",
            (now, result, task_id),
        )


def register_alarm(name: str, dt: datetime, action_config: dict) -> str:
    """Convenience: register a one-shot alarm (e.g. '明早6点叫我')."""
    from .db import task_register
    import uuid
    task_id = f"alarm_{uuid.uuid4().hex[:8]}"
    task_register(
        task_id=task_id,
        name=name,
        trigger_type="date",
        trigger_config={"datetime": dt.isoformat()},
        action_type="notify",
        action_config=action_config,
        category="user",
        priority=1,
    )
    return task_id


def register_recurring(name: str, cron_expr: str, action_type: str,
                       action_config: dict, conditions: list | None = None,
                       priority: int = 5) -> str:
    """Convenience: register a recurring task."""
    from .db import task_register
    import uuid
    task_id = f"recurring_{uuid.uuid4().hex[:8]}"
    task_register(
        task_id=task_id,
        name=name,
        trigger_type="cron",
        trigger_config={"expression": cron_expr},
        action_type=action_type,
        action_config=action_config,
        conditions=conditions or [],
        category="user",
        priority=priority,
    )
    return task_id
