#!/usr/bin/env python3
"""Post-hook: save daily summary, archive and clear hourly log.

Also extracts any '→ UPDATE: filename.md: content' directives Claude may have
emitted and queues them in pending_updates.md for later application.
"""
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.safety import looks_like_error
from core.timeutil import now_local_str

MEMORY_DIR = Path(os.environ.get("MEMORY_DIR",
    Path.home() / ".jarvis" / "memory"))
HOURLY_LOG = MEMORY_DIR / "hourly_log.md"
HOURLY_ARCHIVE = MEMORY_DIR / "hourly_archive.md"
DAILY_LOG = MEMORY_DIR / "daily_log.md"
PENDING_UPDATES = MEMORY_DIR / "pending_updates.md"


def _ensure_pending_header(path: Path) -> None:
    """Create pending_updates.md with frontmatter if missing."""
    if not path.exists():
        path.write_text(
            "---\nname: Pending Memory Updates\n"
            "description: Queued memory updates for main session to apply\n"
            "type: reference\n---\n\n# Pending Memory Updates\n\n## Queue\n"
        )


def main() -> int:
    summary = sys.stdin.read().strip()
    if not summary or "HEARTBEAT_OK" in summary:
        return 0
    if looks_like_error(summary) or len(summary) < 10:
        print("[memory-daily] skipping — output looks like error/noise", file=sys.stderr)
        return 0

    ts_date = now_local_str("%Y-%m-%d")

    MEMORY_DIR.mkdir(parents=True, exist_ok=True)

    # Queue UPDATE directives (if any) — must happen before stripping them
    updates = re.findall(r'→ UPDATE:\s*(\S+\.md):\s*(.+)', summary)
    if updates:
        _ensure_pending_header(PENDING_UPDATES)
        with PENDING_UPDATES.open("a", encoding="utf-8") as f:
            for filename, content in updates:
                f.write(f"- [{ts_date}] {filename}: {content.strip()}\n")

    # Strip UPDATE lines from the index we'll write to daily_log
    index_lines = [l for l in summary.splitlines() if not l.startswith("→ UPDATE:")]
    index = "\n".join(index_lines).strip()

    # Append to daily log
    if index:
        with DAILY_LOG.open("a", encoding="utf-8") as f:
            f.write(f"\n## {ts_date}\n{index}\n")

    # Archive hourly log before clearing
    if HOURLY_LOG.exists() and HOURLY_LOG.stat().st_size > 0:
        with HOURLY_ARCHIVE.open("a", encoding="utf-8") as f:
            f.write(f"\n# Archived {ts_date}\n{HOURLY_LOG.read_text(encoding='utf-8')}\n")
        HOURLY_LOG.write_text("")

    print(f"[memory] Daily index saved for {ts_date}.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
