#!/usr/bin/env python3
"""Post-hook: apply tidy actions from Claude's response.

Claude returns a JSON with actions to take:
{
  "index_update": "<new _index.md content>",
  "actions_taken": ["removed duplicate in hourly_log", ...],
  "warnings": ["hot/ over budget by 500 chars"]
}

Or HEARTBEAT_OK if nothing needs fixing.
"""
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.safety import looks_like_error
from core.timeutil import now_local_str

MEMORY_DIR = Path(os.environ.get("MEMORY_DIR",
    Path.home() / ".jarvis" / "memory"))
INDEX_FILE = MEMORY_DIR / "_index.md"


def main() -> int:
    raw = sys.stdin.read().strip()
    if not raw or "HEARTBEAT_OK" in raw:
        return 0
    if looks_like_error(raw):
        print("[memory-tidy] skipping — output looks like error", file=sys.stderr)
        return 0

    # Try to parse JSON response
    cleaned = re.sub(r'^```json?\s*', '', raw)
    cleaned = re.sub(r'```\s*$', '', cleaned.strip())
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        # If not JSON, Claude might have returned plain text actions
        print(f"[memory-tidy] non-JSON response, skipping auto-apply", file=sys.stderr)
        return 0

    # Update index if provided
    index_content = data.get("index_update", "")
    if index_content and len(index_content) > 50:
        INDEX_FILE.write_text(index_content)
        print(f"[memory-tidy] Updated _index.md", file=sys.stderr)

    actions = data.get("actions_taken", [])
    if actions:
        print(f"[memory-tidy] Actions: {', '.join(actions)}", file=sys.stderr)

    warnings = data.get("warnings", [])
    if warnings:
        for w in warnings:
            print(f"[memory-tidy] WARNING: {w}", file=sys.stderr)

    # Never send anything to user — purely background
    return 0


if __name__ == "__main__":
    sys.exit(main())
