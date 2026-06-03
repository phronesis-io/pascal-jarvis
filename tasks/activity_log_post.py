#!/usr/bin/env python3
"""Post-hook: append activity entries to system/activity_log.jsonl.

This is a SILENT task — never outputs to user.
Maintains a rolling 7-day log of what Pascal actually did.
"""

import os
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.timeutil import now_local
from core.safety import parse_json_response
from core.jsonl import read_jsonl, write_jsonl

MEMORY_DIR = Path(os.environ.get("MEMORY_DIR", Path.home() / ".jarvis" / "memory"))
LOG_FILE = MEMORY_DIR / "system" / "activity_log.jsonl"
MAX_DAYS = 7


def main():
    raw = sys.stdin.read().strip()
    if not raw or raw == "HEARTBEAT_OK":
        return

    # Parse Claude's JSON response
    data = parse_json_response(raw)
    if data is None:
        # If Claude didn't return valid JSON, skip silently
        print("[activity-log] non-JSON response, skipping", file=sys.stderr)
        return
    entries = data.get("entries", [])

    if not entries:
        return

    today = now_local().strftime("%Y-%m-%d")

    existing = read_jsonl(LOG_FILE)

    # Add new entries with today's date
    for entry in entries:
        record = {
            "date": entry.get("date", today),
            "time": entry.get("time", ""),
            "activity": entry.get("activity", ""),
            "source": entry.get("source", "inferred"),
            "energy": entry.get("energy_hint", entry.get("energy", "unknown")),
        }
        if record["activity"]:
            existing.append(record)

    # Trim to last 7 days
    cutoff = (now_local() - timedelta(days=MAX_DAYS)).strftime("%Y-%m-%d")
    existing = [e for e in existing if e.get("date", "") >= cutoff]

    write_jsonl(LOG_FILE, existing)

    # SILENT — no output to stdout (nothing sent to user)


if __name__ == "__main__":
    main()
