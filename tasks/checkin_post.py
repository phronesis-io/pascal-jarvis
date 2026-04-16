#!/usr/bin/env python3
"""Post-hook: log each check-in as one JSONL line so future rounds can avoid
repetition. JSONL avoids parsing bugs when Claude writes its own "### " headers
inside the reply. Also caps the log at MAX_ENTRIES to bound context growth.

Stdin: the check-in message Claude generated (markdown).
Stdout: same message, passed through to the user.
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.safety import looks_like_error
from core.timeutil import now_local_str

MEMORY_DIR = Path(os.environ.get("MEMORY_DIR", Path.home() / ".jarvis" / "memory"))
LOG_FILE = MEMORY_DIR / "checkin_log.jsonl"
MAX_ENTRIES = 20


def main() -> int:
    message = sys.stdin.read().strip()
    if not message or message == "HEARTBEAT_OK":
        return 0
    if looks_like_error(message):
        print("[checkin] skipping — looks like error output", file=sys.stderr)
        return 0

    MEMORY_DIR.mkdir(parents=True, exist_ok=True)

    # Read existing entries (one per line). Skip malformed lines silently.
    entries: list[dict] = []
    if LOG_FILE.exists():
        for line in LOG_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    entries.append({
        "ts": now_local_str("%Y-%m-%d %H:%M"),
        "content": message,
    })
    entries = entries[-MAX_ENTRIES:]

    # Atomic write: temp + rename
    tmp = LOG_FILE.with_suffix(".jsonl.tmp")
    tmp.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in entries) + "\n",
                   encoding="utf-8")
    os.replace(tmp, LOG_FILE)

    # Pass through unchanged so Lark still gets the reply
    print(message)
    return 0


if __name__ == "__main__":
    sys.exit(main())
