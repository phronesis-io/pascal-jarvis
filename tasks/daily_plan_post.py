#!/usr/bin/env python3
"""Post-hook: send morning daily plan as Lark card + log for plan-vs-reality comparison."""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.card import build_card, build_rich_card
from core.safety import looks_like_error
from core.timeutil import now_local_str

MEMORY_DIR = Path(os.environ.get("MEMORY_DIR", Path.home() / ".jarvis" / "memory"))
PLAN_LOG = MEMORY_DIR / "system" / "daily_plan_log.jsonl"


def main():
    raw = sys.stdin.read().strip()
    if not raw or raw == "HEARTBEAT_OK":
        return

    # Parse JSON response
    try:
        data = json.loads(raw)
        message = data.get("user_message", "")
    except json.JSONDecodeError:
        # If plain text (Claude didn't follow JSON format), use as-is
        if looks_like_error(raw):
            return
        message = raw

    if not message:
        return

    # Log for plan-vs-reality comparison later
    PLAN_LOG.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "date": now_local_str("%Y-%m-%d"),
        "ts": now_local_str("%Y-%m-%d %H:%M"),
        "plan": message,
    }

    # Append to log (keep last 14 days)
    entries = []
    if PLAN_LOG.exists():
        for line in PLAN_LOG.read_text(errors="ignore").strip().split("\n"):
            if line.strip():
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

    entries.append(entry)
    # Trim to 14 entries
    entries = entries[-14:]

    tmp = PLAN_LOG.with_suffix(".tmp")
    tmp.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in entries) + "\n")
    tmp.replace(PLAN_LOG)

    # Output as Lark card with richview link for full plan
    date_str = now_local_str("%Y-%m-%d")
    # Card shows first 3 lines as summary, full content in richview
    summary_lines = message.strip().splitlines()[:4]
    summary = "\n".join(summary_lines)
    if len(message.strip().splitlines()) > 4:
        summary += "\n..."

    print(build_rich_card(
        header="🌅 今日",
        summary=summary,
        sections=[{"type": "markdown", "content": message}],
        meta={"source": "daily_plan", "date": date_str},
    ))


if __name__ == "__main__":
    main()
