#!/usr/bin/env python3
"""Post-hook: append hourly summary to hourly_log.md"""
import sys
import os
from datetime import datetime
from pathlib import Path

MEMORY_DIR = Path(os.environ.get("MEMORY_DIR",
    Path.home() / ".jarvis" / "memory"))
HOURLY_LOG = MEMORY_DIR / "hourly_log.md"

summary = sys.stdin.read().strip()
if not summary or "HEARTBEAT_OK" in summary:
    sys.exit(0)

_ERROR_PATTERNS = [
    "Not logged in", "Please run /login", "Invalid authentication",
    "API Error", "authentication_error", "rate_limit", "Traceback",
]
if any(p in summary for p in _ERROR_PATTERNS) or len(summary) < 10:
    sys.exit(0)

ts = datetime.now().strftime("%Y-%m-%d %H:%M")
HOURLY_LOG.parent.mkdir(parents=True, exist_ok=True)
with open(HOURLY_LOG, "a", encoding="utf-8") as f:
    f.write(f"\n### {ts}\n{summary}\n")
print(f"[memory] Hourly summary saved at {ts}", file=sys.stderr)
