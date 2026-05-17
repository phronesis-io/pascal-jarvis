#!/usr/bin/env python3
"""Post-hook: execute weekly review decisions, send summary."""
import json
import os
import re
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.tasks import TaskManager
from core.card import build_card
from core.safety import looks_like_error

MEMORY_DIR = Path(os.environ.get("MEMORY_DIR", Path.home() / ".jarvis" / "memory"))


def main() -> int:
    raw = sys.stdin.read().strip()
    if not raw or "HEARTBEAT_OK" in raw:
        return 0
    if looks_like_error(raw):
        return 0

    cleaned = re.sub(r'^```json?\s*', '', raw)
    cleaned = re.sub(r'```\s*$', '', cleaned)

    # Try JSON parse
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        # Plain text response — just send as card
        if len(raw) > 20:
            print(build_card("📋 周省", raw))
        return 0

    tm = TaskManager(MEMORY_DIR)

    # Apply auto_actions
    for action in data.get("auto_actions", []):
        task_id = action.get("task_id", "")
        if not task_id:
            continue
        if action.get("action") == "decay":
            tm.decay(task_id, action.get("reason", ""))
        elif action.get("action") == "defer":
            tm.defer(task_id, action.get("to_date", ""))

    # Archive old resolved items
    tm.archive_old(days=30)

    # Send user message
    msg = data.get("user_message", "").strip()
    if msg:
        print(build_card("📋 周省", msg))
    return 0


if __name__ == "__main__":
    sys.exit(main())
