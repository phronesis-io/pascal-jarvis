#!/usr/bin/env python3
"""Post-hook: save weekly digest, archive and clear daily log."""
import sys
import os
from datetime import datetime
from pathlib import Path

MEMORY_DIR = Path(os.environ.get("MEMORY_DIR",
    Path.home() / ".jarvis" / "memory"))
DAILY_LOG = MEMORY_DIR / "daily_log.md"
DAILY_ARCHIVE = MEMORY_DIR / "daily_archive.md"
LONGTERM = MEMORY_DIR / "longterm_digest.md"
LONGTERM_BAK = MEMORY_DIR / "longterm_digest.bak.md"

summary = sys.stdin.read().strip()
if not summary or "HEARTBEAT_OK" in summary:
    sys.exit(0)

_ERROR_PATTERNS = ("Not logged in", "Please run /login", "Invalid authentication",
                   "API Error", "authentication_error", "Traceback")
if any(p in summary for p in _ERROR_PATTERNS) or len(summary) < 20:
    sys.exit(0)

ts = datetime.now().strftime('%Y-%m-%d %H:%M')

# Backup previous digest
if LONGTERM.exists() and LONGTERM.stat().st_size > 0:
    LONGTERM_BAK.write_text(LONGTERM.read_text(encoding="utf-8"))

# Archive daily log
if DAILY_LOG.exists() and DAILY_LOG.stat().st_size > 0:
    with open(DAILY_ARCHIVE, "a", encoding="utf-8") as f:
        f.write(f"\n# Archived {ts}\n{DAILY_LOG.read_text(encoding='utf-8')}\n")

# Write new digest
LONGTERM.write_text(f"# Long-term Digest\nLast updated: {ts}\n\n{summary}\n")
DAILY_LOG.write_text("")
print(f"[memory] Weekly digest updated, daily archived + cleared.", file=sys.stderr)
