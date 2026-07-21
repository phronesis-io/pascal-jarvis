"""Bridge between the static HEARTBEAT.md system and dynamic SQLite scheduler.

This module is called by bot.sh's heartbeat loop to check if any dynamic
tasks are due. It returns task prompts/actions that should be executed
in the current heartbeat cycle.

Usage from bot.sh (via Python):
    python3 -c "
    from dashboard.heartbeat_bridge import check_dynamic_tasks
    result = check_dynamic_tasks()
    if result: print(result)
    "
"""

import json
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from dashboard.db import get_db, task_update
from dashboard.scheduler import get_due_tasks, mark_executed

logger = logging.getLogger(__name__)

# Task ids already warned about (legacy non-notify rows, bad action_config)
# so each poison row logs once per process, not every heartbeat tick.
_warned_task_ids: set[str] = set()


def check_dynamic_tasks() -> str:
    """Check for due dynamic tasks and return action instructions.

    Returns a JSON string with tasks to execute, or empty string.
    Format:
    {
        "tasks": [
            {
                "id": "alarm_abc123",
                "name": "morning alarm",
                "action_type": "notify",
                "action_config": {"message": "起床了！"}
            }
        ]
    }
    """
    due = get_due_tasks()
    if not due:
        return ""

    actions = []
    for task in due:
        try:
            action_type = task.get("action_type", "prompt")
            if action_type != "notify":
                # Legacy prompt/script rows: no executor exists, so marking
                # them executed would be a lie. Leave untouched, warn once.
                if task["id"] not in _warned_task_ids:
                    _warned_task_ids.add(task["id"])
                    logger.warning(
                        "task %s has action_type %r but no executor exists; "
                        "skipping (not marked executed)", task["id"], action_type)
                continue

            action_config = task.get("action_config", "{}")
            if isinstance(action_config, str):
                action_config = json.loads(action_config)

            actions.append({
                "id": task["id"],
                "name": task["name"],
                "action_type": action_type,
                "action_config": action_config,
                "category": task.get("category", "user"),
                "priority": task.get("priority", 5),
            })

            # Mark as executed
            mark_executed(task["id"])
        except Exception as e:
            # One bad row (e.g. malformed action_config JSON) must not kill
            # the remaining due tasks.
            if task["id"] not in _warned_task_ids:
                _warned_task_ids.add(task["id"])
                logger.warning("skipping malformed due task %s: %s", task["id"], e)

    if not actions:
        return ""

    return json.dumps({"tasks": actions}, ensure_ascii=False)


def register_from_action(action_params: str) -> str:
    """Parse an [ACTION:schedule_task|...] and register it.

    Called when bot.sh encounters a schedule_task action from Claude.
    Expected format: name=<name>|type=<cron|interval|date>|config=<value>|action=<notify|prompt>|message=<text>

    Returns confirmation message.
    """
    import uuid
    parts = {}
    for segment in action_params.split("|"):
        if "=" in segment:
            k, v = segment.split("=", 1)
            parts[k.strip()] = v.strip()

    name = parts.get("name", "unnamed_task")
    trigger_type = parts.get("type", "interval")
    config_str = parts.get("config", "")
    action_type = parts.get("action", "notify")
    message = parts.get("message", name)

    if action_type != "notify":
        return (f"Cannot register '{name}': action type '{action_type}' has no "
                f"executor (it would never actually run); only 'notify' is supported")

    # Parse trigger config
    if trigger_type == "cron":
        trigger_config = {"expression": config_str}
    elif trigger_type == "interval":
        try:
            seconds = int(config_str)
        except ValueError:
            seconds = 600
        trigger_config = {"seconds": seconds}
    elif trigger_type == "date":
        trigger_config = {"datetime": config_str}
    else:
        trigger_config = {"seconds": 600}

    # Parse conditions
    conditions = []
    if "wake" in parts:
        conditions.append({
            "type": "user_awake",
            "wake": parts.get("wake", "08:00"),
            "sleep": parts.get("sleep", "23:30"),
        })

    task_id = f"agent_{uuid.uuid4().hex[:8]}"

    from dashboard.db import task_register
    try:
        task_register(
            task_id=task_id,
            name=name,
            trigger_type=trigger_type,
            trigger_config=trigger_config,
            action_type=action_type,
            action_config={"message": message},
            conditions=conditions,
            category="agent",
        )
    except ValueError as e:
        return f"Cannot register '{name}': {e}"

    return f"Task '{name}' registered (id: {task_id}, trigger: {trigger_type})"


if __name__ == "__main__":
    # CLI interface for bot.sh
    if len(sys.argv) > 1 and sys.argv[1] == "check":
        result = check_dynamic_tasks()
        if result:
            print(result)
    elif len(sys.argv) > 1 and sys.argv[1] == "register":
        params = sys.argv[2] if len(sys.argv) > 2 else ""
        msg = register_from_action(params)
        print(msg)
    else:
        print("Usage: heartbeat_bridge.py [check|register <params>]", file=sys.stderr)
