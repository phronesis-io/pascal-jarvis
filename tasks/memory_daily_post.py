#!/usr/bin/env python3
"""Post-hook: save daily summary, archive and clear hourly log."""
import sys
import os
import re
from datetime import datetime
from pathlib import Path

MEMORY_DIR = Path(os.environ.get("MEMORY_DIR",
    Path.home() / ".jarvis" / "memory"))
HOURLY_LOG = MEMORY_DIR / "hourly_log.md"
HOURLY_ARCHIVE = MEMORY_DIR / "hourly_archive.md"
DAILY_LOG = MEMORY_DIR / "daily_log.md"
PENDING_UPDATES = MEMORY_DIR / "pending_updates.md"

summary = sys.stdin.read().strip()
if not summary or "HEARTBEAT_OK" in summary:
    sys.exit(0)

_ERROR_PATTERNS = ("Not logged in", "Please run /login", "Invalid authentication",
                   "API Error", "authentication_error", "Traceback")
if any(p in summary for p in _ERROR_PATTERNS) or len(summary) < 10:
    sys.exit(0)

ts = datetime.now().strftime("%Y-%m-%d")

# Queue UPDATE directives
updates = re.findall(r'→ UPDATE:\s*(\S+\.md):\s*(.+)', summary)
if updates:
    _ensure_pending(PENDING_UPDATES)
    with open(PENDING_UPDATES, "a", encoding="utf-8") as f:
        for filename, content in updates:
            f.write(f"- [{ts}] {filename}: {content.strip()}\n")

# Strip UPDATE lines from index
index_lines = [l for l in summary.splitlines() if not l.startswith("→ UPDATE:")]
index = "\n".join(index_lines).strip()

# Append to daily log
DAILY_LOG.parent.mkdir(parents=True, exist_ok=True)
with open(DAILY_LOG, "a", encoding="utf-8") as f:
    f.write(f"\n## {ts}\n{index}\n")

# Archive hourly log before clearing
if HOURLY_LOG.exists() and HOURLY_LOG.stat().st_size > 0:
    with open(HOURLY_ARCHIVE, "a", encoding="utf-8") as f:
        f.write(f"\n# Archived {ts}\n{HOURLY_LOG.read_text(encoding='utf-8')}\n")

HOURLY_LOG.write_text("")
print(f"[memory] Daily index saved for {ts}.", file=sys.stderr)


def _ensure_pending(path: Path):
    if not path.exists():
        path.write_text(
            "---\nname: Pending Memory Updates\n"
            "description: Queued memory updates for main session to apply\n"
            "type: reference\n---\n\n# Pending Memory Updates\n\n## Queue\n"
        )
