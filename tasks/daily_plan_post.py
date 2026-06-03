#!/usr/bin/env python3
"""Post-hook: send morning daily plan as Lark card + log for plan-vs-reality comparison."""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.card import build_card, build_rich_card
from core.safety import looks_like_error, parse_json_response, summarize
from core.jsonl import append_jsonl
from core.timeutil import now_local_str

MEMORY_DIR = Path(os.environ.get("MEMORY_DIR", Path.home() / ".jarvis" / "memory"))
PLAN_LOG = MEMORY_DIR / "system" / "daily_plan_log.jsonl"


def main():
    raw = sys.stdin.read().strip()
    if not raw or raw == "HEARTBEAT_OK":
        return

    # Parse JSON response (handles code fences + trailing text)
    data = parse_json_response(raw)
    if data is not None:
        message = data.get("user_message", "")
    else:
        # If plain text (Claude didn't follow JSON format), use as-is
        if looks_like_error(raw):
            return
        message = raw

    if not message:
        return

    # Strip Claude format artifacts that sometimes leak
    for noise in ["user_message follows", "user_message:", "以下是"]:
        message = message.replace(noise, "").strip()

    if not message:
        return

    # Log for plan-vs-reality comparison later (keep last 14 entries)
    append_jsonl(PLAN_LOG, {
        "date": now_local_str("%Y-%m-%d"),
        "ts": now_local_str("%Y-%m-%d %H:%M"),
        "plan": message,
    }, keep_last=14)

    # Output as Lark card with richview link for full plan
    date_str = now_local_str("%Y-%m-%d")
    # Card shows first lines as summary, full content in richview
    summary = summarize(message)

    print(build_rich_card(
        header="🌅 今日",
        summary=summary,
        sections=[{"type": "markdown", "content": message}],
        meta={"source": "daily_plan", "date": date_str},
        source="daily-plan",
    ))


if __name__ == "__main__":
    main()
