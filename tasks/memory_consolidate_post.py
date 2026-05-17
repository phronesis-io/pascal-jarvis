#!/usr/bin/env python3
"""Post-hook: apply memory update directives directly to target files.

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


def _apply_update(memory_dir: Path, filename: str, content: str, ts: str) -> None:
    """Directly append an update directive to the target memory file."""
    target = memory_dir / filename
    try:
        target.resolve().relative_to(memory_dir.resolve())
    except ValueError:
        print(f"[memory-consolidate] BLOCKED path traversal: {filename}", file=sys.stderr)
        return
    if not target.exists():
        print(f"[memory-consolidate] skipping update for {filename} — file does not exist", file=sys.stderr)
        return
    try:
        with target.open("a", encoding="utf-8") as f:
            f.write(f"\n<!-- auto-update {ts} -->\n- {content.strip()}\n")
    except OSError as e:
        print(f"[memory-consolidate] failed to write {filename}: {e}", file=sys.stderr)


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
        for filename, content in updates:
            _apply_update(MEMORY_DIR, filename, content, ts)
        print(f"[memory-consolidate] applied {len(updates)} update(s) directly", file=sys.stderr)

    # Output diary portion (non-UPDATE lines) — this becomes the Lark message
    diary_lines = [l for l in raw.splitlines() if not l.startswith("→ UPDATE:")]
    diary = "\n".join(diary_lines).strip()
    if diary:
        print(diary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
