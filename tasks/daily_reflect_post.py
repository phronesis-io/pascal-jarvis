#!/usr/bin/env python3
"""Post-hook: send evening reflection as Lark card + log observed patterns."""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.card import build_card, build_rich_card
from core.safety import looks_like_error, parse_json_response, summarize
from core.jsonl import read_jsonl, write_jsonl
from core.timeutil import now_local_str

MEMORY_DIR = Path(os.environ.get("MEMORY_DIR", Path.home() / ".jarvis" / "memory"))
PATTERNS_FILE = MEMORY_DIR / "system" / "patterns.jsonl"
MAX_PATTERNS = 50


def main():
    raw = sys.stdin.read().strip()
    if not raw or raw == "HEARTBEAT_OK":
        return

    # Parse JSON response
    data = parse_json_response(raw)
    if data is not None:
        message = data.get("user_message", "")
        patterns = data.get("patterns_noted", [])
    else:
        if looks_like_error(raw):
            return
        message = raw
        patterns = []

    if not message:
        return

    # Log patterns if any were noted
    if patterns:
        existing = read_jsonl(PATTERNS_FILE)
        for p in patterns:
            if p:
                existing.append({
                    "date": now_local_str("%Y-%m-%d"),
                    "pattern": p,
                })
        # Keep last N entries
        existing = existing[-MAX_PATTERNS:]
        write_jsonl(PATTERNS_FILE, existing)

    # Output as Lark card with richview for full reflection
    date_str = now_local_str("%Y-%m-%d")
    summary = summarize(message)

    sections = [{"type": "markdown", "content": message}]
    if patterns:
        sections.append({"type": "kv", "items": {f"模式 {i+1}": p for i, p in enumerate(patterns)}})

    print(build_rich_card(
        header="🌙 回顾",
        summary=summary,
        sections=sections,
        meta={"source": "daily_reflect", "date": date_str},
    source="daily-reflect",
))


if __name__ == "__main__":
    main()
