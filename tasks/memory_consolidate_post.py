#!/usr/bin/env python3
"""Post-hook: queue memory update directives for main session to apply.

Outputs the diary portion (non-UPDATE lines) to stdout so bot.sh sends it to Lark.
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
PENDING_UPDATES = MEMORY_DIR / "pending_updates.md"


def _ensure_pending_header(path: Path) -> None:
    if not path.exists():
        path.write_text(
            "---\nname: Pending Memory Updates\n"
            "description: Queued memory updates for main session to apply\n"
            "type: reference\n---\n\n# Pending Memory Updates\n\n## Queue\n"
        )


def main() -> int:
    raw = sys.stdin.read().strip()
    if not raw or "HEARTBEAT_OK" in raw:
        return 0
    if looks_like_error(raw):
        print("[memory-consolidate] skipping — output looks like error", file=sys.stderr)
        return 0

    ts = now_local_str("%Y-%m-%d")

    MEMORY_DIR.mkdir(parents=True, exist_ok=True)

    updates = re.findall(r'→ UPDATE:\s*(\S+\.md):\s*(.+)', raw)
    if updates:
        _ensure_pending_header(PENDING_UPDATES)
        with PENDING_UPDATES.open("a", encoding="utf-8") as f:
            for filename, content in updates:
                f.write(f"- [{ts}] {filename}: {content.strip()}\n")
        print(f"[memory-consolidate] queued {len(updates)} update(s)", file=sys.stderr)

    # Output diary portion (non-UPDATE lines) — this becomes the Lark message
    diary_lines = [l for l in raw.splitlines() if not l.startswith("→ UPDATE:")]
    diary = "\n".join(diary_lines).strip()
    if diary:
        print(diary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
