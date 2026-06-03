#!/usr/bin/env python3
"""Post-hook: format watch-later reminder as Lark card and update item status.

Stdin: Claude's JSON response with title, url, user_message.
Stdout: Lark interactive card JSON (or plain text).
"""

import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.card import build_card
from core.safety import looks_like_error, parse_json_response
from core.jsonl import read_jsonl, write_jsonl

MEMORY_DIR = Path(os.environ.get("MEMORY_DIR", Path.home() / ".jarvis" / "memory"))
STORE_FILE = MEMORY_DIR / "system" / "watchlater.jsonl"


def main() -> int:
    raw = sys.stdin.read().strip()
    if not raw or raw == "HEARTBEAT_OK":
        return 0
    if looks_like_error(raw):
        print("[watchlater-remind] skipping — looks like error", file=sys.stderr)
        return 0

    # Parse Claude's response
    data = parse_json_response(raw)
    if data is None:
        # Never emit raw JSON — extract human-readable text only
        text = re.sub(r'\{[^{}]*\}', '', raw).strip()
        if text and "http" in text:
            print(build_card("⏰ 稍后看", text, source="watchlater-remind"))
        return 0

    url = data.get("url", "")
    title = data.get("title", "")
    user_message = data.get("user_message", "")

    if not user_message and not url:
        return 0

    # Mark item as "reminded" in the store
    if url:
        entries = read_jsonl(STORE_FILE)
        for entry in entries:
            if entry.get("url") == url and entry.get("status") == "pending":
                entry["status"] = "reminded"
                break
        write_jsonl(STORE_FILE, entries)

    # Build interactive card using shared helper
    header_text = f"📌 收藏提醒 | {title}" if title else "📌 收藏提醒"
    buttons = [{"text": "去看看", "url": url}] if url else None
    print(build_card(header_text, user_message, buttons, source="watchlater-remind"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
