#!/usr/bin/env python3
"""Post-hook: save monthly archive, clear weekly digest."""
import sys
import os
from datetime import datetime
from pathlib import Path

MEMORY_DIR = Path(os.environ.get("MEMORY_DIR",
    Path.home() / ".jarvis" / "memory"))
WEEKLY_DIGEST = MEMORY_DIR / "longterm_digest.md"
MONTHLY_ARCHIVE = MEMORY_DIR / "monthly_archive.md"
MONTHLY_ARCHIVE_BAK = MEMORY_DIR / "monthly_archive.bak.md"

summary = sys.stdin.read().strip()
if not summary or "HEARTBEAT_OK" in summary:
    sys.exit(0)

_ERROR_PATTERNS = ("Not logged in", "Please run /login", "Invalid authentication",
                   "API Error", "authentication_error", "Traceback")
if any(p in summary for p in _ERROR_PATTERNS) or len(summary) < 20:
    sys.exit(0)

ts = datetime.now().strftime("%Y-%m-%d")

# Backup previous archive
if MONTHLY_ARCHIVE.exists() and MONTHLY_ARCHIVE.stat().st_size > 0:
    MONTHLY_ARCHIVE_BAK.write_text(MONTHLY_ARCHIVE.read_text(encoding="utf-8"))

MONTHLY_ARCHIVE.write_text(f"# Monthly Archive\nLast updated: {ts}\n\n{summary}\n")
WEEKLY_DIGEST.write_text("")
print(f"[memory] Monthly archive updated, weekly digest cleared.", file=sys.stderr)
