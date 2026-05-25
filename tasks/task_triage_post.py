#!/usr/bin/env python3
"""Post-hook: apply triage decisions (decay stale items, notify user)."""
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.tasks import TaskManager
from core.card import build_card
from core.safety import extract_json, looks_like_error

MEMORY_DIR = Path(os.environ.get("MEMORY_DIR", Path.home() / ".jarvis" / "memory"))


def main() -> int:
    raw = sys.stdin.read().strip()
    if not raw or "HEARTBEAT_OK" in raw:
        return 0
    if looks_like_error(raw):
        return 0

    # Try to parse JSON response (handles code fences + trailing text)
    cleaned = extract_json(raw)

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        # If Claude returned plain text instead of JSON, just forward as message
        if len(raw) > 10 and "HEARTBEAT_OK" not in raw:
            print(build_card("📋 任务提醒", raw))
        return 0

    tm = TaskManager(MEMORY_DIR)

    # Apply auto-decay
    for item in data.get("auto_decay", []):
        task_id = item.get("task_id", "")
        reason = item.get("reason", "")
        if task_id:
            tm.decay(task_id, reason)

    # Send user message
    msg = data.get("user_message", "").strip()
    if msg:
        print(build_card("📋 任务", msg))
    return 0


if __name__ == "__main__":
    sys.exit(main())
