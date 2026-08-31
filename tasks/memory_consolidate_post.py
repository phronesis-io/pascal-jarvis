#!/usr/bin/env python3
"""Post-hook: apply memory update directives directly to target files.

The diary portion (non-directive lines) is archived to silent_outputs.jsonl,
NEVER printed to stdout: post-script stdout becomes a Lark message, and on
2026-07-07 21:08 the internal third-person diary (bookkeeping about the owner,
ops jargon, a wrong 「我这边没有直接对话」 claim) was delivered to his chat —
HEARTBEAT.md classifies the whole Memory Pipeline as silent.
"""
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.jsonl import append_jsonl
from core.safety import is_idle_reply, looks_like_error
from core.timeutil import now_local_str
# Canonical/replica warm/ pair — single home is memory_tidy_post (the sync owner).
from tasks.memory_tidy_post import AUTO_MEMORY, HEARTBEAT_MEMORY

MEMORY_DIR = Path(os.environ.get("MEMORY_DIR",
    Path.home() / ".jarvis" / "memory"))
JARVIS_DIR = Path(os.environ.get("JARVIS_DIR",
    Path(__file__).resolve().parent.parent))


def _canon_warm_target(memory_dir: Path, rel: Path, target: Path,
                       task: str = "memory-consolidate") -> Path:
    """Reroute a warm/ directive from the heartbeat replica to canon.

    CLAUDE.md source-of-truth rule: warm/用户画像以 auto-memory 为准 — the
    heartbeat copy is a read-only replica that only memory-tidy's one-way
    sync (auto → heartbeat) may write. This task runs under the heartbeat
    MEMORY_DIR, so writing warm/ in place made it a second replica writer and
    5 profile files silently diverged up to 5 weeks (2026-07-08 memory audit).
    Only warm/ is remapped; MEMORY_DIR keeps serving the other tiers. A
    replica-only file is seeded into canon first so the caller's exists()
    check doesn't silently drop its update.

    Shared with memory_daily_post (same directive grammar, same replica risk —
    2026-07-09 red-team [12]); `task` labels the stderr lines for the caller.
    """
    try:
        if memory_dir.resolve() != HEARTBEAT_MEMORY.resolve():
            return target
    except OSError:
        return target
    if not (AUTO_MEMORY / "warm").is_dir():
        return target
    canon = AUTO_MEMORY / rel
    if not canon.exists() and target.exists():
        try:
            canon.parent.mkdir(parents=True, exist_ok=True)
            canon.write_text(target.read_text(encoding="utf-8"), encoding="utf-8")
        except OSError as e:
            print(f"[{task}] canon seed failed for {rel}, writing replica: {e}",
                  file=sys.stderr)
            return target
        print(f"[{task}] seeded canonical copy from replica: {rel}",
              file=sys.stderr)
    print(f"[{task}] warm/ directive rerouted to canonical auto-memory: {rel}",
          file=sys.stderr)
    return canon


def _resolve_target(memory_dir: Path, filename: str) -> Path | None:
    """Resolve a directive's target file inside MEMORY_DIR, guarding traversal.

    warm/ targets may be remapped to the canonical auto-memory copy — see
    _canon_warm_target.
    """
    target = memory_dir / filename
    try:
        rel = target.resolve().relative_to(memory_dir.resolve())
    except ValueError:
        print(f"[memory-consolidate] BLOCKED path traversal: {filename}", file=sys.stderr)
        return None
    if rel.parts and rel.parts[0] == "warm":
        target = _canon_warm_target(memory_dir, rel, target)
    if not target.exists():
        print(f"[memory-consolidate] skipping {filename} — file does not exist", file=sys.stderr)
        return None
    return target


def _apply_update(memory_dir: Path, filename: str, content: str, ts: str) -> bool:
    """Append a new fact to the target memory file (additive only). Returns
    True only if the append landed — main() counts successes, not directives."""
    target = _resolve_target(memory_dir, filename)
    if target is None:
        return False
    # The model sometimes echoes the file's existing `<!-- auto-update ... -->`
    # marker into its content; strip a leading one so we don't double-stamp.
    content = re.sub(r'^\s*<!--\s*auto-update[^>]*-->\s*', '', content).strip()
    if not content:
        # Same-line-empty directive: the real body sat on the next line, which
        # the single-line grammar (see main) deliberately leaves in the diary.
        # Writing anyway tombstones the file with an empty bullet — 3 writes
        # were lost that way on 2026-07-08.
        print(f"[memory-consolidate] skipping UPDATE on {filename} — empty content "
              f"(body must be on the directive line)", file=sys.stderr)
        return False
    try:
        with target.open("a", encoding="utf-8") as f:
            f.write(f"\n<!-- auto-update {ts} -->\n- {content}\n")
    except OSError as e:
        print(f"[memory-consolidate] failed to write {filename}: {e}", file=sys.stderr)
        return False
    return True


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
    if is_idle_reply(raw):
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
        applied = sum(_apply_update(MEMORY_DIR, fn, content, ts) for fn, content in updates)
        print(f"[memory-consolidate] applied {applied}/{len(updates)} update(s) directly", file=sys.stderr)

    # Diary portion (non-directive lines): archive only, never stdout — see
    # module docstring. Same capped file the SILENT_TASKS path uses
    # (heartbeat.py _collect_output), so the full text survives for debugging
    # (jarvis.log keeps only 80-char prefixes). append_jsonl's
    # read-modify-write is safe here: posts run serialized under the cycle
    # flock, same condition heartbeat.py relies on.
    diary_lines = [l for l in raw.splitlines()
                   if not l.startswith("→ UPDATE:") and not l.startswith("→ REPLACE:")]
    diary = "\n".join(diary_lines).strip()
    if diary:
        try:
            append_jsonl(JARVIS_DIR / "silent_outputs.jsonl",
                         {"ts": now_local_str("%Y-%m-%d %H:%M"),
                          "task": "memory-consolidate", "text": diary},
                         keep_last=100)
        except Exception as e:
            print(f"[memory-consolidate] diary archive failed: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
