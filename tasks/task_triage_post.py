#!/usr/bin/env python3
"""Post-hook: apply triage decisions (decay stale items, notify user)."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.tasks import TaskManager
from core.card import build_card
from core.safety import looks_like_error, parse_json_response, salvage_field, salvage_task_ids

MEMORY_DIR = Path(os.environ.get("MEMORY_DIR", Path.home() / ".jarvis" / "memory"))


def main() -> int:
    raw = sys.stdin.read().strip()
    if not raw or "HEARTBEAT_OK" in raw:
        return 0
    if looks_like_error(raw):
        return 0

    data = parse_json_response(raw)
    if data is None:
        # Broken JSON — most often the model used unescaped quotes inside a
        # string value. If this is clearly the structured object, salvage the
        # human message + decay ids (never dump raw JSON: it leaks auto_decay).
        if '"user_message"' in raw or '"auto_decay"' in raw:
            data = {
                "user_message": salvage_field(raw, "user_message") or "",
                "auto_decay": [{"task_id": t, "reason": ""} for t in salvage_task_ids(raw)],
            }
        elif len(raw) > 10 and "HEARTBEAT_OK" not in raw:
            # Genuine plain-text response → forward as a message
            print(build_card("📋 任务", raw, source="task-triage"))
            return 0
        else:
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
        print(build_card("📋 任务", msg, source="task-triage"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
