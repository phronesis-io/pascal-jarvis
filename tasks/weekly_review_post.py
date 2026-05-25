#!/usr/bin/env python3
"""Post-hook: execute weekly review decisions, send summary."""
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.tasks import TaskManager
from core.card import build_card, build_rich_card
from core.safety import extract_json, looks_like_error
from core.timeutil import now_local_str

MEMORY_DIR = Path(os.environ.get("MEMORY_DIR", Path.home() / ".jarvis" / "memory"))


def main() -> int:
    raw = sys.stdin.read().strip()
    if not raw or "HEARTBEAT_OK" in raw:
        return 0
    if looks_like_error(raw):
        return 0

    cleaned = extract_json(raw)

    # Try JSON parse
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        # Plain text response — send as rich card
        if len(raw) > 20:
            summary_lines = raw.strip().splitlines()[:4]
            summary = "\n".join(summary_lines)
            if len(raw.strip().splitlines()) > 4:
                summary += "\n..."
            print(build_rich_card(
                header="📋 周省",
                summary=summary,
                sections=[{"type": "markdown", "content": raw}],
                meta={"source": "weekly_review", "date": now_local_str("%Y-%m-%d", source="weekly-review")},
            ))
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

    # Send user message with richview
    msg = data.get("user_message", "").strip()
    if msg:
        summary_lines = msg.strip().splitlines()[:4]
        summary = "\n".join(summary_lines)
        if len(msg.strip().splitlines()) > 4:
            summary += "\n..."
        print(build_rich_card(
            header="📋 周省",
            summary=summary,
            sections=[{"type": "markdown", "content": msg}],
            meta={"source": "weekly_review", "date": now_local_str("%Y-%m-%d", source="weekly-review")},
        ))
    return 0


if __name__ == "__main__":
    sys.exit(main())
