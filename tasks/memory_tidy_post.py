#!/usr/bin/env python3
"""Post-hook: apply tidy actions from Claude's response.

Claude returns a JSON with actions to take:
{
  "index_update": "<new _index.md content>",
  "actions_taken": ["removed duplicate in hourly_log", ...],
  "warnings": ["hot/ over budget by 500 chars"]
}

Or HEARTBEAT_OK if nothing needs fixing.
Also always runs daily_log auto-archive (14-day TTL) as a side-effect.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.safety import looks_like_error, parse_json_response
from core.timeutil import now_local_str
from tasks.memory_daily_post import _archive_old_daily_entries

MEMORY_DIR = Path(os.environ.get("MEMORY_DIR",
    Path.home() / ".jarvis" / "memory"))
INDEX_FILE = MEMORY_DIR / "_index.md"

# Dual-directory paths for one-way sync (auto → heartbeat)
AUTO_MEMORY = Path.home() / ".claude/projects/-Users-pascal-Desktop-jarvis/memory"
HEARTBEAT_MEMORY = Path.home() / ".claude/projects/-Users-pascal-Desktop-jarvis-repos-pascal-jarvis/memory"


def _sync_warm_auto_to_heartbeat():
    """One-way sync: auto-memory warm/ → heartbeat warm/ (auto is source of truth for user profile files)."""
    auto_warm = AUTO_MEMORY / "warm"
    hb_warm = HEARTBEAT_MEMORY / "warm"
    if not auto_warm.exists() or not hb_warm.exists():
        return

    synced = []
    for src in auto_warm.glob("*.md"):
        dst = hb_warm / src.name
        # Skip if heartbeat version is newer or identical
        if dst.exists():
            src_mtime = src.stat().st_mtime
            dst_mtime = dst.stat().st_mtime
            if dst_mtime >= src_mtime:
                continue
            # Auto is newer — sync
            src_content = src.read_text(encoding="utf-8")
            dst_content = dst.read_text(encoding="utf-8")
            if src_content == dst_content:
                continue
        else:
            src_content = src.read_text(encoding="utf-8")

        dst.write_text(src_content, encoding="utf-8")
        synced.append(src.name)

    if synced:
        print(f"[memory-tidy] synced auto→heartbeat warm/: {', '.join(synced)}", file=sys.stderr)


def _sync_root_feedback_auto_to_heartbeat():
    """One-way sync: auto-memory root feedback_*.md → heartbeat root.

    Heartbeat-side memories wikilink 24+ feedback files that only exist in
    auto-memory's ROOT (the old sync covered warm/ only) — on the heartbeat
    side those behavioral-rule references were dangling pointers. Root files
    are not auto-loaded by load_tiered_memory (deliberate: 24 rule files
    would bloat every prompt); the sync makes the wikilinks RESOLVABLE via
    on-demand Read when a heartbeat session follows one. Newer-wins, same as
    warm/.
    """
    if not AUTO_MEMORY.exists() or not HEARTBEAT_MEMORY.exists():
        return
    synced = []
    for src in AUTO_MEMORY.glob("feedback_*.md"):
        dst = HEARTBEAT_MEMORY / src.name
        if dst.exists() and dst.stat().st_mtime >= src.stat().st_mtime:
            continue
        content = src.read_text(encoding="utf-8")
        if dst.exists() and dst.read_text(encoding="utf-8") == content:
            continue
        dst.write_text(content, encoding="utf-8")
        synced.append(src.name)
    if synced:
        print(f"[memory-tidy] synced auto→heartbeat root feedback: {', '.join(synced)}",
              file=sys.stderr)


def _sync_open_threads_auto_to_heartbeat():
    """One-way sync: auto-memory open_threads.md → heartbeat system/open_threads.md.

    Named exception to the "system/ files stay independent" rule (CLAUDE.md):
    open_threads drives the heartbeat's proactive follow-ups, so the heartbeat
    copy must track the live thread state the main conversation maintains in
    auto-memory. Auto is the source of truth (that's where main-convo edits
    land); heartbeat gets a read-only copy. Note the path remap — auto keeps it
    at memory root, heartbeat loads it from system/ (load_tiered_memory only
    reads system/*.md, never root files).
    """
    src = AUTO_MEMORY / "open_threads.md"
    dst = HEARTBEAT_MEMORY / "system" / "open_threads.md"
    if not src.exists():
        return
    src_content = src.read_text(encoding="utf-8")
    if dst.exists():
        # Skip if heartbeat copy is newer or already identical
        if dst.stat().st_mtime >= src.stat().st_mtime:
            return
        if dst.read_text(encoding="utf-8") == src_content:
            return
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(src_content, encoding="utf-8")
    print("[memory-tidy] synced auto→heartbeat: open_threads.md", file=sys.stderr)


def main() -> int:
    # Always run daily_log archive check (independent of Claude's response)
    try:
        _archive_old_daily_entries(now_local_str("%Y-%m-%d"))
    except Exception as e:
        print(f"[memory-tidy] archive check failed: {e}", file=sys.stderr)

    # One-way sync: auto → heartbeat for warm/ files
    try:
        _sync_warm_auto_to_heartbeat()
    except Exception as e:
        print(f"[memory-tidy] warm sync failed: {e}", file=sys.stderr)

    # One-way sync: auto → heartbeat for open_threads.md (named system/ exception)
    try:
        _sync_open_threads_auto_to_heartbeat()
    except Exception as e:
        print(f"[memory-tidy] open_threads sync failed: {e}", file=sys.stderr)

    # One-way sync: auto → heartbeat for root feedback_*.md (wikilink targets)
    try:
        _sync_root_feedback_auto_to_heartbeat()
    except Exception as e:
        print(f"[memory-tidy] root feedback sync failed: {e}", file=sys.stderr)

    raw = sys.stdin.read().strip()
    if not raw or "HEARTBEAT_OK" in raw:
        return 0
    if looks_like_error(raw):
        print("[memory-tidy] skipping — output looks like error", file=sys.stderr)
        return 0

    # Try to parse JSON response
    data = parse_json_response(raw)
    if data is None:
        # If not JSON, Claude might have returned plain text actions
        print("[memory-tidy] non-JSON response, skipping auto-apply", file=sys.stderr)
        return 0

    # Update index if provided
    index_content = data.get("index_update", "")
    if index_content and len(index_content) > 50:
        INDEX_FILE.write_text(index_content)
        print(f"[memory-tidy] Updated _index.md", file=sys.stderr)

    actions = data.get("actions_taken", [])
    if actions:
        print(f"[memory-tidy] Actions: {', '.join(actions)}", file=sys.stderr)

    warnings = data.get("warnings", [])
    if warnings:
        for w in warnings:
            print(f"[memory-tidy] WARNING: {w}", file=sys.stderr)

    # Never send anything to user — purely background
    return 0


if __name__ == "__main__":
    sys.exit(main())
