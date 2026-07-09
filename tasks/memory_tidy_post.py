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
import fcntl
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.safety import looks_like_error, parse_json_response
from core.timeutil import now_local_str
from tasks.memory_daily_post import _archive_old_daily_entries

MEMORY_DIR = Path(os.environ.get("MEMORY_DIR",
    Path.home() / ".jarvis" / "memory"))
INDEX_FILE = MEMORY_DIR / "_index.md"

# Dual-directory paths for one-way sync (auto → heartbeat). CLAUDE.md
# source-of-truth rule: warm/ 用户画像以 auto-memory 为准; the heartbeat copy
# is a read-only replica that only this sync may write. Single home of the
# hardcoded pair — memory_consolidate_post imports it to reroute its warm/
# directives to canon instead of writing the replica (2026-07-08 memory audit).
AUTO_MEMORY = Path.home() / ".claude/projects/-Users-pascal-Desktop-jarvis/memory"
HEARTBEAT_MEMORY = Path.home() / ".claude/projects/-Users-pascal-Desktop-jarvis-repos-pascal-jarvis/memory"

# Budget enforcement (2026-07-07 memory audit): system/todos.md is append-only
# and grew unbounded since April (70KB) — it alone overflowed the loader's 40k
# system reserve, so cross_session_digest / pending_updates / the inboxes were
# dropped from EVERY prompt for days. Tidy is the maintenance path, so it owns
# the cap. Archive-not-delete, per the file's own 维护规则.
TODOS_MAX_CHARS = 20000
_AUTO_UPDATE_PREFIX = "<!-- auto-update"

# An auto-update block as the memory post-scripts write it: marker line plus
# the single bullet line that follows (see _apply_update in
# memory_consolidate_post / memory_daily_post).
_AUTO_UPDATE_BLOCK = re.compile(r"<!--\s*auto-update[^>]*-->\n(?:[^\n]*\n?)?")


def _replica_only_update_blocks(src_content: str, dst_content: str) -> list[str]:
    """Auto-update blocks present in the replica but absent from canon.

    Divergence guard for the newer-wins sync (2026-07-09 red-team [12]): any
    writer that still lands an auto-update on the heartbeat replica (a stale
    code path, a manual edit) would otherwise be silently destroyed the next
    time the auto copy's mtime is bumped. Non-empty result = do NOT overwrite;
    the operator reconciles heartbeat→auto by hand.
    """
    return [b for b in _AUTO_UPDATE_BLOCK.findall(dst_content)
            if b not in src_content]


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
            missing = _replica_only_update_blocks(src_content, dst_content)
            if missing:
                print(f"[memory-tidy] WARNING: NOT syncing warm/{src.name} — "
                      f"heartbeat replica holds {len(missing)} auto-update "
                      f"block(s) absent from the canonical auto copy; "
                      f"overwriting would destroy them. Reconcile "
                      f"heartbeat→auto manually.", file=sys.stderr)
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
        if dst.exists():
            dst_content = dst.read_text(encoding="utf-8")
            if dst_content == content:
                continue
            # Same divergence guard as warm/ — root files have no directive
            # reroute, so a heartbeat-side auto-update is a live possibility.
            missing = _replica_only_update_blocks(content, dst_content)
            if missing:
                print(f"[memory-tidy] WARNING: NOT syncing {src.name} — "
                      f"heartbeat replica holds {len(missing)} auto-update "
                      f"block(s) absent from the canonical auto copy; "
                      f"overwriting would destroy them. Reconcile "
                      f"heartbeat→auto manually.", file=sys.stderr)
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


def _enforce_todos_budget():
    """Archive the oldest auto-update blocks of system/todos.md once the file
    exceeds TODOS_MAX_CHARS, keeping the curated head (进行中/已完成/维护规则)
    and the newest blocks intact. Blocks land verbatim in
    memory/archive/todos_archive_<YYYY>H<N>.md — no tier collector reads
    archive/ (core.memory globs only hot/, top-level warm/, system/,
    timeline/), so archived history stays on disk for on-demand recall.
    """
    todos = MEMORY_DIR / "system" / "todos.md"
    if not todos.exists():
        return
    # Locked read→archive→replace (2026-07-08 red-team fix): an append landing
    # between our read and os.replace would be silently reverted — neither
    # kept nor archived. Sidecar .lock, set_fact recipe (core/memory.py):
    # os.replace gives todos.md a new inode each run, so flocking the file
    # itself would let a concurrent opener of the NEW inode take its own
    # "exclusive" lock (same gotcha as core/session.py). Residual race: the
    # known writers today take NO lock — memory-consolidate's _apply_update /
    # _apply_replace are serialized against us only by the heartbeat cycle
    # flock, and interactive Claude sessions edit the file with no lock at
    # all — so this lock protects future lock-taking writers; against the
    # unlocked ones the archive-before-replace ordering below (archive
    # fsync'd BEFORE todos.md is swapped) guarantees the worst case is a
    # duplicated block, never a lost one (archive-not-delete contract).
    lock_path = todos.with_suffix(".lock")
    with open(lock_path, "w") as lock_fh:
        try:
            fcntl.flock(lock_fh, fcntl.LOCK_EX)
        except OSError:
            pass
        # Read under the lock — a pre-lock read could be stale.
        text = todos.read_text(encoding="utf-8")
        if len(text) <= TODOS_MAX_CHARS:
            return
        lines = text.splitlines(keepends=True)
        starts = [i for i, ln in enumerate(lines) if ln.startswith(_AUTO_UPDATE_PREFIX)]
        if not starts:
            # No append blocks — the bulk is curated content; leave that to the
            # tidy Claude session rather than cutting it mechanically.
            return
        head = "".join(lines[:starts[0]])
        bounds = starts + [len(lines)]
        blocks = ["".join(lines[bounds[j]:bounds[j + 1]]) for j in range(len(starts))]
        # Archive oldest-first until at/below half the cap — the headroom keeps
        # this from re-triggering on every run. Always keep the newest block.
        keep_size = len(head) + sum(len(b) for b in blocks)
        cut = 0
        while cut < len(blocks) - 1 and keep_size > TODOS_MAX_CHARS // 2:
            keep_size -= len(blocks[cut])
            cut += 1
        if cut == 0:
            return
        month = int(now_local_str("%m"))
        archive = (MEMORY_DIR / "archive" /
                   f"todos_archive_{now_local_str('%Y')}H{1 if month <= 6 else 2}.md")
        archive.parent.mkdir(parents=True, exist_ok=True)
        with open(archive, "a", encoding="utf-8") as f:
            f.write(f"\n<!-- memory-tidy auto-archive {now_local_str('%Y-%m-%d %H:%M')} "
                    f"— {cut} oldest block(s) from system/todos.md -->\n\n")
            f.write("".join(blocks[:cut]).strip() + "\n")
            # Persist the archive BEFORE todos.md is replaced — a crash in
            # between duplicates the blocks instead of losing them.
            f.flush()
            os.fsync(f.fileno())
        tmp = todos.with_suffix(".md.tmp")
        tmp.write_text(head + "".join(blocks[cut:]), encoding="utf-8")
        os.replace(tmp, todos)
    print(f"[memory-tidy] todos.md over {TODOS_MAX_CHARS} chars — archived "
          f"{cut} oldest block(s) to archive/{archive.name}", file=sys.stderr)


def _warn_tiers_over_budget():
    """ONE leveled warn naming offending files/sizes when the memory payload
    would still be truncated after enforcement. The loader's own stderr
    warning bypasses leveled logging, which is how a multi-day truncation
    stayed invisible (2026-07-07 memory audit). Tier reserves are floors that
    only bite when the GLOBAL payload exceeds MAX_MEMORY_CHARS, so the warn
    is gated on that — a warm tier over its floor with global headroom is
    fine by design and must not cry wolf.
    """
    from core.log import log
    from core.memory import (HOT_BUDGET, MAX_MEMORY_CHARS, SYSTEM_BUDGET,
                             TIMELINE_BUDGET, WARM_BUDGET,
                             _SYSTEM_FILE_CAPS, _TIMELINE_SKIP)

    def _tier_sizes(dirpath, skip=frozenset(), caps=None):
        sizes = {}
        for f in dirpath.glob("*.md"):  # non-recursive — archive/ excluded
            if not f.is_file() or f.name in skip:
                continue
            try:
                n = len(f.read_text(encoding="utf-8"))
            except OSError:
                continue
            if caps and f.name in caps:
                n = min(n, caps[f.name])
            sizes[f.name] = n
        return sizes

    tiers = {
        "hot": (_tier_sizes(MEMORY_DIR / "hot"), HOT_BUDGET),
        "warm": (_tier_sizes(MEMORY_DIR / "warm"), WARM_BUDGET),
        "system": (_tier_sizes(MEMORY_DIR / "system", caps=_SYSTEM_FILE_CAPS),
                   SYSTEM_BUDGET),
        "timeline": (_tier_sizes(MEMORY_DIR / "timeline", skip=_TIMELINE_SKIP),
                     TIMELINE_BUDGET),
    }
    total = sum(sum(sizes.values()) for sizes, _ in tiers.values())
    if total <= MAX_MEMORY_CHARS:
        return
    over = {}
    for tier, (sizes, budget) in tiers.items():
        tier_total = sum(sizes.values())
        if tier_total > budget:
            top = sorted(sizes.items(), key=lambda kv: -kv[1])[:5]
            over[tier] = {"total": tier_total, "budget": budget,
                          "largest": [f"{name}:{chars}" for name, chars in top]}
    if over:
        log("memory-tidy", "tier_over_budget", level="warn",
            total=total, cap=MAX_MEMORY_CHARS, tiers=over)


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

    # Budget enforcement — todos.md cap, then one leveled warn if the payload
    # would still truncate (2026-07-07 memory audit).
    try:
        _enforce_todos_budget()
    except Exception as e:
        print(f"[memory-tidy] todos budget enforcement failed: {e}", file=sys.stderr)
    try:
        _warn_tiers_over_budget()
    except Exception as e:
        print(f"[memory-tidy] tier budget check failed: {e}", file=sys.stderr)

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
