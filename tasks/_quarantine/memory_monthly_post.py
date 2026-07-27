#!/usr/bin/env python3
"""Post-hook: save monthly archive, clear weekly digest."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.safety import looks_like_error
from core.timeutil import now_local_str

MEMORY_DIR = Path(os.environ.get("MEMORY_DIR",
    Path.home() / ".jarvis" / "memory"))
WEEKLY_DIGEST = MEMORY_DIR / "timeline" / "longterm_digest.md"
MONTHLY_ARCHIVE = MEMORY_DIR / "timeline" / "monthly_archive.md"
MONTHLY_ARCHIVE_BAK = MEMORY_DIR / "timeline" / "monthly_archive.bak.md"


def main() -> int:
    summary = sys.stdin.read().strip()
    if not summary or "HEARTBEAT_OK" in summary:
        return 0
    if looks_like_error(summary) or len(summary) < 20:
        print("[memory-monthly] skipping — output looks like error/noise", file=sys.stderr)
        return 0

    ts = now_local_str("%Y-%m-%d")
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)

    if MONTHLY_ARCHIVE.exists() and MONTHLY_ARCHIVE.stat().st_size > 0:
        MONTHLY_ARCHIVE_BAK.write_text(MONTHLY_ARCHIVE.read_text(encoding="utf-8"))

    MONTHLY_ARCHIVE.write_text(f"# Monthly Archive\nLast updated: {ts}\n\n{summary}\n")
    WEEKLY_DIGEST.write_text("")
    print("[memory] Monthly archive updated, weekly digest cleared.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
