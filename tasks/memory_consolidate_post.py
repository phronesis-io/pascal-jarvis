#!/usr/bin/env python3
"""Post-hook: queue memory update directives for main session to apply."""
import sys
import os
import re
from datetime import datetime
from pathlib import Path

MEMORY_DIR = Path(os.environ.get("MEMORY_DIR",
    Path.home() / ".jarvis" / "memory"))
PENDING_UPDATES = MEMORY_DIR / "pending_updates.md"

raw = sys.stdin.read().strip()
if not raw or "HEARTBEAT_OK" in raw:
    sys.exit(0)

ts = datetime.now().strftime('%Y-%m-%d')

updates = re.findall(r'→ UPDATE:\s*(\S+\.md):\s*(.+)', raw)
if updates:
    if not PENDING_UPDATES.exists():
        PENDING_UPDATES.write_text(
            "---\nname: Pending Memory Updates\n"
            "description: Queued memory updates for main session to apply\n"
            "type: reference\n---\n\n# Pending Memory Updates\n\n## Queue\n"
        )
    with open(PENDING_UPDATES, "a", encoding="utf-8") as f:
        for filename, content in updates:
            f.write(f"- [{ts}] {filename}: {content.strip()}\n")

# Output diary summary (non-UPDATE lines) for IM
diary_lines = [l for l in raw.splitlines() if not l.startswith("→ UPDATE:")]
diary = "\n".join(diary_lines).strip()
if diary:
    print(diary)
