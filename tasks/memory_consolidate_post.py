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


def _resolve_target(memory_dir: Path, filename: str) -> Path | None:
    """Resolve a directive's target file inside MEMORY_DIR, guarding traversal."""
    target = memory_dir / filename
    try:
        target.resolve().relative_to(memory_dir.resolve())
    except ValueError:
        print(f"[memory-consolidate] BLOCKED path traversal: {filename}", file=sys.stderr)
        return None
    if not target.exists():
        print(f"[memory-consolidate] skipping {filename} — file does not exist", file=sys.stderr)
        return None
    return target


def _apply_update(memory_dir: Path, filename: str, content: str, ts: str) -> None:
    """Append a new fact to the target memory file (additive only)."""
    target = _resolve_target(memory_dir, filename)
    if target is None:
        return
    # The model sometimes echoes the file's existing `<!-- auto-update ... -->`
    # marker into its content; strip a leading one so we don't double-stamp.
    content = re.sub(r'^\s*<!--\s*auto-update[^>]*-->\s*', '', content).strip()
    try:
        with target.open("a", encoding="utf-8") as f:
            f.write(f"\n<!-- auto-update {ts} -->\n- {content}\n")
    except OSError as e:
        print(f"[memory-consolidate] failed to write {filename}: {e}", file=sys.stderr)


def _apply_replace(memory_dir: Path, filename: str, old: str, new: str, ts: str) -> bool:
    """Reconcile: replace existing text in-place. Returns True if applied.

    The match must be exact and present in the file. If `old` is absent we do
    NOT fall back to appending — a missed REPLACE is a no-op by design, so a
    bad match can't silently re-introduce the contradiction it meant to fix.
    Empty `new` deletes the matched text.
    """
    target = _resolve_target(memory_dir, filename)
    if target is None:
        return False
    old = old.strip()
    if not old:
        print(f"[memory-consolidate] skipping REPLACE on {filename} — empty match text", file=sys.stderr)
        return False
    try:
        text = target.read_text(encoding="utf-8")
    except OSError as e:
        print(f"[memory-consolidate] failed to read {filename}: {e}", file=sys.stderr)
        return False
    if old not in text:
        print(f"[memory-consolidate] REPLACE no-op on {filename} — match text not found", file=sys.stderr)
        return False
    updated = text.replace(old, new.strip(), 1)
    try:
        target.write_text(updated, encoding="utf-8")
    except OSError as e:
        print(f"[memory-consolidate] failed to write {filename}: {e}", file=sys.stderr)
        return False
    return True


def main() -> int:
    raw = sys.stdin.read().strip()
    if not raw or "HEARTBEAT_OK" in raw:
        return 0
    if looks_like_error(raw):
        print("[memory-consolidate] skipping — output looks like error", file=sys.stderr)
        return 0

    ts = now_local_str("%Y-%m-%d")

    MEMORY_DIR.mkdir(parents=True, exist_ok=True)

    # Reconcile directives first: contradictions get fixed in place before any
    # additive appends, so a REPLACE and a related UPDATE can't fight.
    # [ \t] (not \s) so a directive never spills across a newline into the next line.
    replaces = re.findall(r'→ REPLACE:[ \t]*(\S+\.md):[ \t]*(.+?)[ \t]*\|\|\|[ \t]*(.*)', raw)
    if replaces:
        applied = sum(_apply_replace(MEMORY_DIR, fn, old, new, ts) for fn, old, new in replaces)
        print(f"[memory-consolidate] applied {applied}/{len(replaces)} replace(s)", file=sys.stderr)

    updates = re.findall(r'→ UPDATE:[ \t]*(\S+\.md):[ \t]*(.+)', raw)
    if updates:
        for filename, content in updates:
            _apply_update(MEMORY_DIR, filename, content, ts)
        print(f"[memory-consolidate] applied {len(updates)} update(s) directly", file=sys.stderr)

    # Output diary portion (non-directive lines) — this becomes the Lark message
    diary_lines = [l for l in raw.splitlines()
                   if not l.startswith("→ UPDATE:") and not l.startswith("→ REPLACE:")]
    diary = "\n".join(diary_lines).strip()
    if diary:
        print(diary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
