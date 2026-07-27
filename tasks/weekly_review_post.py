#!/usr/bin/env python3
"""Post-hook: execute weekly review decisions, send summary."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.tasks import TaskManager
from core.card import build_card, build_rich_card
from core.safety import looks_like_error, parse_json_response, salvage_field, summarize
from core.timeutil import now_local_str

MEMORY_DIR = Path(os.environ.get("MEMORY_DIR", Path.home() / ".jarvis" / "memory"))


def main() -> int:
    raw = sys.stdin.read().strip()
    if not raw or "HEARTBEAT_OK" in raw:
        return 0
    if looks_like_error(raw):
        return 0

    data = parse_json_response(raw)
    if data is None:
        # Broken JSON (e.g. unescaped quotes in a string value). If it's the
        # structured object, salvage the message only — never dump raw JSON,
        # which leaks auto_actions internals. Skip auto_actions when broken
        # (can't safely recover defer dates / action types from a regex).
        if '"user_message"' in raw or '"auto_actions"' in raw:
            data = {"user_message": salvage_field(raw, "user_message") or "", "auto_actions": []}
        elif len(raw) > 20:
            # Genuine plain-text response — send as rich card
            print(build_rich_card(
                header="📋 周省",
                summary=summarize(raw),
                sections=[{"type": "markdown", "content": raw}],
                meta={
                    "source": "weekly_review",
                    "date": now_local_str("%Y-%m-%d"),
                },
            ))
            return 0
        else:
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
        print(build_rich_card(
            header="📋 周省",
            summary=summarize(msg),
            sections=[{"type": "markdown", "content": msg}],
            meta={
                "source": "weekly_review",
                "date": now_local_str("%Y-%m-%d"),
            },
        ))

    # Stamp this week so the pre-script skips on restart (dedup)
    import datetime
    jarvis_dir = Path(os.environ.get("JARVIS_DIR", Path(__file__).resolve().parent.parent))
    stamp = jarvis_dir / "data" / ".weekly_review_stamp"
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.write_text(datetime.date.today().strftime("%Y-W%V"), encoding="utf-8")

    return 0


if __name__ == "__main__":
    sys.exit(main())
