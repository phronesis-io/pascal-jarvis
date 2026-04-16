#!/usr/bin/env python3
"""Post-hook: append hourly summary to hourly_log.md"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.safety import looks_like_error
from core.timeutil import now_local_str

MEMORY_DIR = Path(os.environ.get("MEMORY_DIR",
    Path.home() / ".jarvis" / "memory"))
HOURLY_LOG = MEMORY_DIR / "hourly_log.md"


def main() -> int:
    summary = sys.stdin.read().strip()
    if not summary or "HEARTBEAT_OK" in summary:
        return 0
    if looks_like_error(summary) or len(summary) < 10:
        print(f"[memory-hourly] skipping — output looks like error/noise", file=sys.stderr)
        return 0

    ts = now_local_str("%Y-%m-%d %H:%M")
    HOURLY_LOG.parent.mkdir(parents=True, exist_ok=True)
    with HOURLY_LOG.open("a", encoding="utf-8") as f:
        f.write(f"\n### {ts}\n{summary}\n")
    print(f"[memory] Hourly summary saved at {ts}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
