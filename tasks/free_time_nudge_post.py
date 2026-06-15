#!/usr/bin/env python3
"""Post-hook: send a casual free-time nudge. Short, non-pushy.
Also updates the daily nudge counter (only counts if message is actually sent)."""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.card import build_card
from core.safety import looks_like_error, parse_json_response
from core.timeutil import now_local_str

JARVIS_DIR = Path(os.environ.get("JARVIS_DIR", Path(__file__).resolve().parent.parent))
STATE_FILE = JARVIS_DIR / ".free_time_nudge_state"


def update_nudge_count():
    """Increment today's nudge counter after a successful send.

    State file is up to 3 lines: date / count / block_id. Line 3 (the per-block
    fire stamp written by free_time_nudge_pre.sh for REQ-75 double-fire dedup)
    must be PRESERVED here — otherwise this rewrite would drop it and a second
    fire in the same block would slip through.
    """
    today = now_local_str("%Y-%m-%d")
    count = 0
    block_id = ""
    if STATE_FILE.exists():
        lines = STATE_FILE.read_text().strip().split("\n")
        if lines and lines[0] == today:
            try:
                count = int(lines[1]) if len(lines) > 1 else 0
            except ValueError:
                count = 0
            block_id = lines[2] if len(lines) > 2 else ""
    STATE_FILE.write_text(f"{today}\n{count + 1}\n{block_id}\n")


def main():
    raw = sys.stdin.read().strip()
    if not raw or raw == "HEARTBEAT_OK":
        return

    # Parse JSON response
    data = parse_json_response(raw)
    if data is not None:
        message = data.get("user_message", "")
    else:
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

    print(build_card("⏰ 空档", message, buttons, source="free-time-nudge"))


if __name__ == "__main__":
    main()
