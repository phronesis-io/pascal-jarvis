#!/usr/bin/env python3
"""Post-hook: write cross-session digest to memory.

Receives Claude's summary from stdin and writes to memory/system/cross_session_digest.md.
Keeps max 50 lines, newest first. No user-facing output (silent task).
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.safety import looks_like_error
from core.timeutil import now_local_str

MEMORY_DIR = Path(os.environ.get("MEMORY_DIR",
    Path.home() / ".jarvis" / "memory"))
DIGEST_FILE = MEMORY_DIR / "system" / "cross_session_digest.md"
MAX_LINES = 50

HEADER = """\
---
name: Cross-Session Digest
description: Recent activity from other Claude Code projects
type: reference
---

# Cross-Session Digest
"""


def main() -> int:
    raw = sys.stdin.read().strip()
    if not raw or "HEARTBEAT_OK" in raw:
        return 0
    if looks_like_error(raw):
        print("[cross-session] skipping — output looks like error", file=sys.stderr)
        return 0

    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    (MEMORY_DIR / "system").mkdir(parents=True, exist_ok=True)

    ts = now_local_str("%Y-%m-%d %H:%M")
    new_entry = f"\n## {ts}\n{raw.strip()}\n"

    # Read existing entries (skip header)
    existing_body = ""
    if DIGEST_FILE.exists():
        content = DIGEST_FILE.read_text(encoding="utf-8")
        # Split off the header (everything before the first ## date entry)
        parts = content.split("\n## ", 1)
        if len(parts) > 1:
            existing_body = "\n## " + parts[1]

    # Combine: new entry first, then existing
    combined = new_entry + existing_body

    # Limit to MAX_LINES of body content
    body_lines = combined.strip().splitlines()
    if len(body_lines) > MAX_LINES:
        body_lines = body_lines[:MAX_LINES]

    final = HEADER + "\n".join(body_lines) + "\n"

    # Atomic write
    tmp = DIGEST_FILE.with_suffix(".md.tmp")
    tmp.write_text(final, encoding="utf-8")
    os.replace(tmp, DIGEST_FILE)

    print(f"[cross-session] digest updated at {ts}", file=sys.stderr)
    # No stdout output — silent task
    return 0


if __name__ == "__main__":
    sys.exit(main())
