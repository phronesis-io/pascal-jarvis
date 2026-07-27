#!/usr/bin/env python3
"""Post-hook: log the morning daily plan for plan-vs-reality comparison.

REQ-84 (2026-07-02): the Lark card build was removed — daily-plan is in
SILENT_TASKS (6/12 hallucination incident), so the card was assembled and
then discarded by the heartbeat every single day. PLAN_LOG stays: it has a
real consumer (daily_reflect_pre.sh reads today's plan for the evening
plan-vs-reality comparison).
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.safety import looks_like_error, parse_json_response
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

    # Stamp today so the pre-script skips on restart (dedup)
    jarvis_dir = Path(os.environ.get("JARVIS_DIR", Path(__file__).resolve().parent.parent))
    stamp = jarvis_dir / "data" / ".daily_plan_stamp"
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.write_text(now_local_str("%Y-%m-%d"), encoding="utf-8")

    # No card output (REQ-84): daily-plan is a SILENT_TASK — anything printed
    # here would be built and then dropped by the heartbeat. Log only.
    print("daily-plan: plan logged to PLAN_LOG (card build removed, REQ-84)",
          file=sys.stderr)


if __name__ == "__main__":
    main()
