#!/usr/bin/env python3
"""Post-hook: save the weekly digest without destroying rolling daily history."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.safety import is_idle_reply, looks_like_error
from core.timeutil import now_local_str

MEMORY_DIR = Path(os.environ.get("MEMORY_DIR",
    Path.home() / ".jarvis" / "memory"))
DAILY_LOG = MEMORY_DIR / "timeline" / "daily_log.md"
DAILY_ARCHIVE = MEMORY_DIR / "timeline" / "daily_archive.md"
LONGTERM = MEMORY_DIR / "timeline" / "longterm_digest.md"
LONGTERM_BAK = MEMORY_DIR / "timeline" / "longterm_digest.bak.md"


def main() -> int:
    summary = sys.stdin.read().strip()
    if is_idle_reply(summary):
        return 0
    if looks_like_error(summary) or len(summary) < 20:
        print("[memory-weekly] skipping — output looks like error/noise", file=sys.stderr)
        return 0

    ts = now_local_str("%Y-%m-%d %H:%M")
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)

    # Backup previous digest (atomic)
    if LONGTERM.exists() and LONGTERM.stat().st_size > 0:
        tmp_bak = LONGTERM_BAK.with_suffix(".tmp")
        tmp_bak.write_text(LONGTERM.read_text(encoding="utf-8"), encoding="utf-8")
        os.replace(tmp_bak, LONGTERM_BAK)

    # Write new digest (atomic)
    tmp_lt = LONGTERM.with_suffix(".tmp")
    tmp_lt.write_text(f"# Long-term Digest\nLast updated: {ts}\n\n{summary}\n",
                      encoding="utf-8")
    os.replace(tmp_lt, LONGTERM)
    # memory_daily_post is the single owner of the rolling 14-day cutoff and
    # daily_archive. Weekly used to copy the entire log into the archive and
    # clear it, so day-level recall was empty immediately after every digest
    # and the next weekly run duplicated history in the archive.
    print("[memory] Weekly digest updated; rolling daily history preserved.",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
