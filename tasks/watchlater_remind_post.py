#!/usr/bin/env python3
"""Post-hook: format watch-later reminder as Lark card and update item status.

Stdin: Claude's JSON response with title, url, user_message.
Stdout: Lark interactive card JSON (or plain text).
"""

import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.card import build_card
from core.safety import looks_like_error

MEMORY_DIR = Path(os.environ.get("MEMORY_DIR", Path.home() / ".jarvis" / "memory"))
STORE_FILE = MEMORY_DIR / "system" / "watchlater.jsonl"


def load_entries() -> list[dict]:
    entries = []
    if STORE_FILE.exists():
        for line in STORE_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


def save_entries(entries: list[dict]) -> None:
    STORE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STORE_FILE.with_suffix(".jsonl.tmp")
    tmp.write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in entries) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, STORE_FILE)


def main() -> int:
    raw = sys.stdin.read().strip()
    if not raw or raw == "HEARTBEAT_OK":
        return 0
    if looks_like_error(raw):
        print("[watchlater-remind] skipping — looks like error", file=sys.stderr)
        return 0

    # Parse Claude's response
    cleaned = re.sub(r"^```json?\s*", "", raw)
    cleaned = re.sub(r"```\s*$", "", cleaned.strip())

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        # If Claude returned plain text, pass through if it has useful content
        if "http" in raw:
            print(raw)
        return 0

    url = data.get("url", "")
    title = data.get("title", "")
    user_message = data.get("user_message", "")

    if not user_message and not url:
        return 0

    # Mark item as "reminded" in the store
    if url:
        entries = load_entries()
        for entry in entries:
            if entry.get("url") == url and entry.get("status") == "pending":
                entry["status"] = "reminded"
                break
        save_entries(entries)

    # Build interactive card using shared helper
    header_text = f"📌 收藏提醒 | {title}" if title else "📌 收藏提醒"
    buttons = [{"text": "去看看", "url": url}] if url else None
    print(build_card(header_text, user_message, buttons))
    return 0


if __name__ == "__main__":
    sys.exit(main())
