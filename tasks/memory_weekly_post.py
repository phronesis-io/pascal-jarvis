#!/usr/bin/env python3
"""Post-hook: save weekly digest, archive and clear daily log."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.safety import looks_like_error
from core.timeutil import now_local_str

MEMORY_DIR = Path(os.environ.get("MEMORY_DIR",
    Path.home() / ".jarvis" / "memory"))
DAILY_LOG = MEMORY_DIR / "timeline" / "daily_log.md"
DAILY_ARCHIVE = MEMORY_DIR / "timeline" / "daily_archive.md"
LONGTERM = MEMORY_DIR / "timeline" / "longterm_digest.md"
LONGTERM_BAK = MEMORY_DIR / "timeline" / "longterm_digest.bak.md"


def main() -> int:
    summary = sys.stdin.read().strip()
    if not summary or "HEARTBEAT_OK" in summary:
        return 0
    if looks_like_error(summary) or len(summary) < 20:
        print("[memory-weekly] skipping — output looks like error/noise", file=sys.stderr)
        return 0

    ts = now_local_str("%Y-%m-%d %H:%M")
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)

    # Backup previous digest
    if LONGTERM.exists() and LONGTERM.stat().st_size > 0:
        LONGTERM_BAK.write_text(LONGTERM.read_text(encoding="utf-8"))

    # Archive daily log
    if DAILY_LOG.exists() and DAILY_LOG.stat().st_size > 0:
        with DAILY_ARCHIVE.open("a", encoding="utf-8") as f:
            f.write(f"\n# Archived {ts}\n{DAILY_LOG.read_text(encoding='utf-8')}\n")

    # Write new digest
    LONGTERM.write_text(f"# Long-term Digest\nLast updated: {ts}\n\n{summary}\n")
    DAILY_LOG.write_text("")
    print("[memory] Weekly digest updated, daily archived + cleared.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
