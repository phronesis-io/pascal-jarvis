#!/usr/bin/env python3
"""Post-hook: write calendar snapshot to hot/calendar_today.md.

Never sends a message to the user — purely background sync.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.safety import looks_like_error
from core.timeutil import now_local_str

MEMORY_DIR = Path(os.environ.get("MEMORY_DIR",
    Path.home() / ".jarvis" / "memory"))
CALENDAR_FILE = MEMORY_DIR / "hot" / "calendar_today.md"


def main() -> int:
    raw = sys.stdin.read().strip()
    if not raw or "HEARTBEAT_OK" in raw:
        return 0
    if looks_like_error(raw):
        print("[calendar-sync] skipping — output looks like error", file=sys.stderr)
        return 0

    ts = now_local_str("%Y-%m-%d %H:%M")
    CALENDAR_FILE.parent.mkdir(parents=True, exist_ok=True)
    CALENDAR_FILE.write_text(
        f"---\nname: 今日日程\ndescription: Lark 日历自动同步，含今天和明天的日程\n"
        f"type: reference\n---\n\n# Calendar (synced {ts})\n\n{raw}\n"
    )
    print(f"[calendar-sync] Updated calendar at {ts}", file=sys.stderr)
    # Return empty — no Lark message
    return 0


if __name__ == "__main__":
    sys.exit(main())
