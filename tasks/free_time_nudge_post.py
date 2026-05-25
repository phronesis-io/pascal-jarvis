#!/usr/bin/env python3
"""Post-hook: send a casual free-time nudge. Short, non-pushy.
Also updates the daily nudge counter (only counts if message is actually sent)."""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.card import build_card
from core.safety import extract_json, looks_like_error
from core.timeutil import now_local_str

JARVIS_DIR = Path(os.environ.get("JARVIS_DIR", Path(__file__).resolve().parent.parent))
STATE_FILE = JARVIS_DIR / ".free_time_nudge_state"


def update_nudge_count():
    """Increment today's nudge counter after a successful send."""
    today = now_local_str("%Y-%m-%d")
    count = 0
    if STATE_FILE.exists():
        lines = STATE_FILE.read_text().strip().split("\n")
        if lines and lines[0] == today:
            try:
                count = int(lines[1]) if len(lines) > 1 else 0
            except ValueError:
                count = 0
    STATE_FILE.write_text(f"{today}\n{count + 1}\n")


def main():
    raw = sys.stdin.read().strip()
    if not raw or raw == "HEARTBEAT_OK":
        return

    # Parse JSON response
    data = None
    try:
        data = json.loads(extract_json(raw))
        message = data.get("user_message", "")
    except json.JSONDecodeError:
        if looks_like_error(raw):
            return
        message = raw

    if not message:
        return

    # Update nudge counter (only when we actually send)
    update_nudge_count()

    # Build Lark card — with watchlater button if present
    watchlater = data.get("watchlater") if isinstance(data, dict) and data else None
    buttons = None
    if watchlater and isinstance(watchlater, dict) and watchlater.get("url"):
        buttons = [{"text": "去看看", "url": watchlater["url"]}]

    print(build_card("⏰ 空档", message, buttons))


if __name__ == "__main__":
    main()
