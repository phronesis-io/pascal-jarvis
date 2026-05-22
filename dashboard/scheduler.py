"""Dynamic task scheduler — Trigger/Condition/Action model.

Runs alongside (not replacing) the static HEARTBEAT.md tasks.
Reads from SQLite `scheduled_tasks` table. LLM can register tasks
via the bot action system: [ACTION:schedule_task|...]

Trigger types:
  - cron: standard cron expression (minute hour dom month dow)
  - interval: every N seconds
  - date: one-shot at specific ISO datetime
  - event: when a specific event fires on the bus

Condition types:
  - time_window: only run between HH:MM and HH:MM
  - not_already_done: skip if ran within window (today/1h/etc)
  - weekday: only on specific days (mon,tue,...)
  - user_awake: skip before wake time / after sleep time

Action types:
  - prompt: run a Claude prompt via heartbeat
  - script: run a shell script
  - notify: send a message to Lark
  - bus_event: emit an event on the bus
"""

import json
import re
import time
from datetime import datetime, timedelta
from pathlib import Path

from .db import get_db, task_list, task_update
from .event_bus import bus


def _parse_cron_field(field_str: str, min_val: int, max_val: int) -> set[int]:
    """Parse a single cron field into a set of valid values."""
    values = set()
    for part in field_str.split(","):
        if part == "*":
            values.update(range(min_val, max_val + 1))
        elif "/" in part:
            base, step = part.split("/")
            step = int(step)
            start = min_val if base == "*" else int(base)
            values.update(range(start, max_val + 1, step))
        elif "-" in part:
            lo, hi = part.split("-")
            values.update(range(int(lo), int(hi) + 1))
        else:
            values.add(int(part))
    return values


def cron_matches(expression: str, dt: datetime | None = None) -> bool:
    """Check if a cron expression matches the given datetime."""
    if dt is None:
        dt = datetime.now()
    parts = expression.strip().split()
    if len(parts) != 5:
        return False
    minute, hour, dom, month, dow = parts
    checks = [
        (minute, dt.minute, 0, 59),
        (hour, dt.hour, 0, 23),
        (dom, dt.day, 1, 31),
        (month, dt.month, 1, 12),
        (dow, dt.weekday(), 0, 6),  # 0=Monday
    ]
    for field_str, current, min_v, max_v in checks:
        if current not in _parse_cron_field(field_str, min_v, max_v):
            return False
    return True


def check_conditions(conditions: list[dict], task: dict) -> bool:
    """Evaluate all conditions for a task. Returns True if all pass."""
    now = datetime.now()
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
                last_dt = datetime.fromisoformat(last_run)
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


def get_due_tasks() -> list[dict]:
    """Get all tasks that should run now."""
    tasks = task_list(enabled_only=True)
    now = datetime.now()
    now_ts = int(time.time())
    due = []

    for task in tasks:
        trigger_type = task["trigger_type"]
        trigger_config = json.loads(task["trigger_config"]) if isinstance(task["trigger_config"], str) else task["trigger_config"]
        conditions = json.loads(task["conditions"]) if isinstance(task["conditions"], str) else (task["conditions"] or [])

        # Check trigger
        triggered = False

        if trigger_type == "cron":
            expr = trigger_config.get("expression", "")
            triggered = cron_matches(expr, now)

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
                target_dt = datetime.fromisoformat(target)
                triggered = now >= target_dt
                # One-shot: disable after trigger
                if triggered and task.get("last_run_at"):
                    continue  # Already ran

        # Check conditions
        if triggered and check_conditions(conditions, task):
            due.append(task)

    return due


def mark_executed(task_id: str, result: str = "") -> None:
    """Mark a task as executed."""
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    db = get_db()
    row = db.execute("SELECT run_count FROM scheduled_tasks WHERE id = ?", (task_id,)).fetchone()
    count = (row[0] or 0) + 1 if row else 1
    task_update(task_id, last_run_at=now, run_count=count, last_result=result)
    bus.emit_sync("task:executed", {"task_id": task_id, "result": result})


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
    bus.emit_sync("task:registered", {"task_id": task_id, "name": name})
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
    bus.emit_sync("task:registered", {"task_id": task_id, "name": name})
    return task_id
