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
JARVIS_DIR = Path(os.environ.get(
    "JARVIS_DIR", Path(__file__).resolve().parent.parent
))
# The loader reads top-level ``warm/*.md`` and never reads memory/_index.md.
# Keep the generated index beside the knowledge files it describes.
def _index_file(memory_dir: Path) -> Path:
    """Return the canonical warm-tier index, including on fresh installs."""
    return memory_dir / "warm" / "_index.md"


INDEX_FILE = _index_file(MEMORY_DIR)
STRAY_WARM_DIR = JARVIS_DIR / "warm"

# Dual-directory paths for one-way sync (auto → heartbeat). CLAUDE.md
# source-of-truth rule: warm/ 用户画像以 auto-memory 为准; the heartbeat copy
# is a read-only replica that only this sync may write. Single home of the
# hardcoded pair — memory_consolidate_post imports it to reroute its warm/
# directives to canon instead of writing the replica (2026-07-08 memory audit).
from core.claude_projects import auto_memory_dir as _auto_mem, heartbeat_memory_dir as _hb_mem
AUTO_MEMORY = _auto_mem()
HEARTBEAT_MEMORY = _hb_mem()

# Budget enforcement (2026-07-07 memory audit): system/todos.md is append-only
# and grew unbounded since April (70KB) — it alone overflowed the loader's 40k
# system reserve, so pending_updates and the inboxes were
# dropped from EVERY prompt for days. Tidy is the maintenance path, so it owns
# the cap. Archive-not-delete, per the file's own 维护规则.
# Aligned with core.memory._SYSTEM_FILE_CAPS["todos.md"] (2026-07-29): keeping
# 20k on disk while the loader injected at most 13k left 7k that no prompt ever
# saw — the same dark matter REQ-92 removed for the inbox buffers. Archive-not-
# delete still applies, so the trimmed blocks stay recallable on disk.
TODOS_MAX_CHARS = 13000
_AUTO_UPDATE_PREFIX = "<!-- auto-update"

# An auto-update block as the memory post-scripts write it: marker line plus
# the single bullet line that follows (see _apply_update in
# memory_consolidate_post / memory_daily_post).
_AUTO_UPDATE_BLOCK = re.compile(r"<!--\s*auto-update[^>]*-->\n(?:[^\n]*\n?)?")


def _recover_stray_repo_warm() -> None:
    """Preserve and remove model-written ``<repo>/warm`` files.

    Before heartbeat tasks had task-level tool policy, the GPT fallback could
    call ``file_write('warm/...')`` from the repository cwd. Those files are
    outside MEMORY_DIR and therefore invisible to Jarvis. Archive each file in
    the real memory tree, verify the bytes, and only then remove the stray.
    """
    if not STRAY_WARM_DIR.is_dir():
        return
    try:
        if STRAY_WARM_DIR.resolve() == (MEMORY_DIR / "warm").resolve():
            return
    except OSError:
        return
    archive = MEMORY_DIR / "archive" / "recovered_repo_warm"
    recovered: list[str] = []
    for src in sorted(STRAY_WARM_DIR.glob("*.md")):
        try:
            payload = src.read_bytes()
            archive.mkdir(parents=True, exist_ok=True)
            dst = archive / src.name
            suffix = 1
            while dst.exists() and dst.read_bytes() != payload:
                dst = archive / f"{src.stem}.{suffix}{src.suffix}"
                suffix += 1
            if not dst.exists():
                tmp = archive / f".{dst.name}.{os.getpid()}.tmp"
                with tmp.open("wb") as fh:
                    fh.write(payload)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, dst)
            if dst.read_bytes() != payload:
                print(
                    f"[memory-tidy] stray warm verification failed: {src.name}",
                    file=sys.stderr,
                )
                continue
            src.unlink()
            recovered.append(src.name)
        except OSError as exc:
            print(
                f"[memory-tidy] stray warm recovery failed for {src.name}: {exc}",
                file=sys.stderr,
            )
    try:
        STRAY_WARM_DIR.rmdir()
    except OSError:
        pass
    if recovered:
        print(
            "[memory-tidy] recovered repo-root warm/ → "
            f"archive/recovered_repo_warm/: {', '.join(recovered)}",
            file=sys.stderr,
        )


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

        lock_path = dst.with_suffix(".md.lock")
        with open(lock_path, "a") as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            dst.write_text(src_content, encoding="utf-8")
            _copy_mtime(src, dst)
        synced.append(src.name)

    if synced:
        print(f"[memory-tidy] synced auto→heartbeat warm/: {', '.join(synced)}", file=sys.stderr)


def _copy_mtime(src: Path, dst: Path) -> None:
    """Replica keeps the SOURCE mtime (记忆瘦身 PRD R2). write_text stamps
    sync time, which made every replica look freshly edited — the loader's
    newest-first ordering and demote's staleness test both keyed off a lie
    (timeless feedback_* files read as the stalest, fat prep docs as fresh).
    """
    try:
        st = src.stat()
        os.utime(dst, (st.st_atime, st.st_mtime))
    except OSError:
        pass


def _mirror_warm_deletions():
    """Mirror DELIBERATE auto-side archival to the replica (记忆瘦身 PRD R1).

    红队修正：'absent from auto warm/' is NOT deletion evidence — 8 replica-
    only files turned out to be LIVE profile docs written directly by
    heartbeat sessions whose canon never existed in auto warm/ (health,
    energy, portfolio…); archiving them would have evicted the owner's
    profile from every prompt. Only a copy in auto warm/archive/ proves the
    auto side deliberately demoted/retired the file — that's the gate.
    Replica-only files with no such evidence are left alone. Move (never
    delete) into the replica's warm/archive/.
    """
    auto_warm = AUTO_MEMORY / "warm"
    hb_warm = HEARTBEAT_MEMORY / "warm"
    if not auto_warm.is_dir() or not hb_warm.is_dir():
        return
    auto_names = {f.name for f in auto_warm.glob("*.md") if f.is_file()}
    auto_archive = auto_warm / "archive"
    archived_stems = ({f.name.split(".")[0] for f in auto_archive.glob("*.md")}
                      if auto_archive.is_dir() else set())
    archived = []
    archive_dir = hb_warm / "archive"
    for f in hb_warm.glob("*.md"):
        if not f.is_file() or f.name == "_index.md" or f.name in auto_names:
            continue
        if f.stem.split(".")[0] not in archived_stems:
            continue  # no deletion evidence — replica-owned or unknown, keep
        archive_dir.mkdir(parents=True, exist_ok=True)
        dst = archive_dir / f.name
        if dst.exists():
            n = 1
            while (archive_dir / f"{f.stem}.{n}{f.suffix}").exists():
                n += 1
            dst = archive_dir / f"{f.stem}.{n}{f.suffix}"
        f.rename(dst)
        archived.append(f.name)
    if archived:
        print(f"[memory-tidy] mirrored auto archival — replica warm/ → "
              f"archive/: {', '.join(archived)}", file=sys.stderr)


def _demote_stale_auto_warm():
    """Wire demote_stale_warm into the maintenance path (记忆瘦身 PRD R5).

    The function existed since REQ-73 but nothing in production ever called
    it — warm grew unbounded until the assembled payload blew the global cap
    and truncation ran every heartbeat. Runs on the AUTO side (true mtimes;
    the replica's are sync artifacts) — the replica copy is then archived by
    _mirror_warm_deletions in the same run. feedback_*/user_* are exempt
    inside demote_stale_warm itself.
    """
    from core.memory import demote_stale_warm
    demoted = demote_stale_warm(AUTO_MEMORY)
    if demoted:
        print(f"[memory-tidy] demoted stale auto warm/ → archive/: "
              f"{', '.join(demoted)}", file=sys.stderr)


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
        lock_path = dst.with_suffix(".md.lock")
        with open(lock_path, "a") as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            dst.write_text(content, encoding="utf-8")
            _copy_mtime(src, dst)
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
    lock_path = dst.with_suffix(".md.lock")
    with open(lock_path, "a") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        dst.write_text(src_content, encoding="utf-8")
        _copy_mtime(src, dst)
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


# REQ-93 (2026-07-14): issue/report files parked in system/ ride EVERY
# heartbeat prompt forever unless someone remembers to move them — two
# status:fixed issue files from June were still burning system-tier budget in
# July while inbox_private_mail got truncated away. Only files that OPT IN via
# a resolved-family frontmatter status are touched; everything else is
# operator-owned. mtime > 7 days keeps a just-fixed issue visible long enough
# for the fix to be verified in production.
_RESOLVED_STATUS = re.compile(
    r"^status:\s*(?:fixed\S*|resolved\S*|closed|done)\s*$",
    re.MULTILINE | re.IGNORECASE)
RESOLVED_ARCHIVE_AFTER_S = 7 * 86400


def _yaml_frontmatter(head: str) -> str | None:
    """The YAML frontmatter block, or None if the file doesn't open with one.

    Strictly line-anchored (red-team fix): a file that merely OPENS with a
    markdown horizontal rule must not have its prose scanned as frontmatter —
    every line up to the closing `---` line must look like YAML (key:, list
    item, comment, blank, or indented continuation), else this is not
    frontmatter and the file stays operator-owned."""
    lines = head.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    fm: list[str] = []
    for ln in lines[1:]:
        if ln.strip() == "---":
            for f in fm:
                if f.strip() and not re.match(r"^(\s|-\s|#|[A-Za-z_][\w-]*:)", f):
                    return None
            return "\n".join(fm)
        fm.append(ln)
    return None  # unterminated within the scanned head


def _archive_resolved_system_issues():
    """Move system/*.md with a resolved-family frontmatter `status:` and
    mtime > 7 days to memory/archive/system/ (no tier collector reads
    archive/). Archive-not-delete; symlinks (open_threads canon pointer) are
    never touched."""
    import shutil
    import time as _time
    sys_dir = MEMORY_DIR / "system"
    if not sys_dir.is_dir():
        return
    now = _time.time()
    archive_dir = MEMORY_DIR / "archive" / "system"
    for f in sorted(sys_dir.glob("*.md")):
        if f.is_symlink() or not f.is_file():
            continue
        try:
            if now - f.stat().st_mtime < RESOLVED_ARCHIVE_AFTER_S:
                continue
            head = f.read_text(encoding="utf-8")[:2000]
        except OSError:
            continue
        frontmatter = _yaml_frontmatter(head)
        if frontmatter is None or not _RESOLVED_STATUS.search(frontmatter):
            continue
        archive_dir.mkdir(parents=True, exist_ok=True)
        dest = archive_dir / f.name
        if dest.exists():
            dest = archive_dir / f"{f.stem}_{now_local_str('%Y%m%d%H%M%S')}{f.suffix}"
        shutil.move(str(f), str(dest))
        print(f"[memory-tidy] archived resolved system file: {f.name} "
              f"→ archive/system/{dest.name}", file=sys.stderr)


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
                             _SYSTEM_FILE_CAPS, _TIMELINE_SKIP,
                             load_tiered_memory)

    # Production calls use warm=index. Full-mode corpus size is diagnostic and
    # must not write a false overflow warning into the index that every later
    # call reads.
    index_total = len(load_tiered_memory(MEMORY_DIR, warm_mode="index"))
    if index_total < MAX_MEMORY_CHARS:
        return

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

    from core.memory import WARM_FILE_CAP
    warm_sizes = {name: min(n, WARM_FILE_CAP)
                  for name, n in _tier_sizes(MEMORY_DIR / "warm").items()}
    tiers = {
        "hot": (_tier_sizes(MEMORY_DIR / "hot"), HOT_BUDGET),
        # warm sizes are load-capped (R4) — sizing them uncapped would count
        # ~40k phantom chars and cry tier_over_budget while the loader isn't
        # actually truncating anything.
        "warm": (warm_sizes, WARM_BUDGET),
        "system": (_tier_sizes(MEMORY_DIR / "system", caps=_SYSTEM_FILE_CAPS),
                   SYSTEM_BUDGET),
        "timeline": (_tier_sizes(MEMORY_DIR / "timeline", skip=_TIMELINE_SKIP),
                     TIMELINE_BUDGET),
    }
    total = index_total
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
    try:
        _recover_stray_repo_warm()
    except Exception as e:
        print(f"[memory-tidy] stray warm recovery failed: {e}", file=sys.stderr)

    # Always run daily_log archive check (independent of Claude's response)
    try:
        _archive_old_daily_entries(now_local_str("%Y-%m-%d"))
    except Exception as e:
        print(f"[memory-tidy] archive check failed: {e}", file=sys.stderr)

    # 记忆瘦身 PRD R5: demote stale prep docs on the auto side (true mtimes)
    # BEFORE the sync/mirror pass so the replica archives in the same run.
    try:
        _demote_stale_auto_warm()
    except Exception as e:
        print(f"[memory-tidy] stale-warm demotion failed: {e}", file=sys.stderr)

    # One-way sync: auto → heartbeat for warm/ files
    try:
        _sync_warm_auto_to_heartbeat()
    except Exception as e:
        print(f"[memory-tidy] warm sync failed: {e}", file=sys.stderr)

    # 记忆瘦身 PRD R1: replica-only warm files (auto deleted/demoted them)
    # stop riding every prompt — moved to the replica's warm/archive/.
    try:
        _mirror_warm_deletions()
    except Exception as e:
        print(f"[memory-tidy] mirror-deletions failed: {e}", file=sys.stderr)

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
    # REQ-93: resolved issue files stop riding every heartbeat prompt.
    try:
        _archive_resolved_system_issues()
    except Exception as e:
        print(f"[memory-tidy] resolved-issue archive failed: {e}", file=sys.stderr)
    try:
        _warn_tiers_over_budget()
    except Exception as e:
        print(f"[memory-tidy] tier budget check failed: {e}", file=sys.stderr)

    # First reconcile the exact operational statements invalidated by shipped
    # runtime changes.  This is a narrow allowlist, not a model-authored memory
    # rewrite, and runs on both the canonical auto-memory and active replica.
    try:
        from core.memory_operational_claims import reconcile_operational_claims
        repaired = []
        for root in dict.fromkeys((AUTO_MEMORY, MEMORY_DIR)):
            repaired.extend(reconcile_operational_claims(root))
        if repaired:
            print(
                "[memory-tidy] reconciled obsolete operational claims: "
                + ", ".join(sorted(set(repaired))),
                file=sys.stderr,
            )
    except Exception as exc:
        print(
            f"[memory-tidy] operational-claim reconciliation failed: {exc}",
            file=sys.stderr,
        )

    raw = sys.stdin.read().strip()
    if not raw or "HEARTBEAT_OK" in raw:
        return 0
    return _process(raw)


def _verify_index(index_content: str, warm_dir) -> str:
    """Mechanically ground the LLM-regenerated _index.md against warm/.

    7/22 audit: the 7/21 regeneration listed 2 archived files as live and
    omitted 12 existing ones — an index the LLM writes from memory drifts
    from the directory it describes, and index-guided readers (§4 memory
    scan) inherit the drift. Two mechanical passes: ① drop entry lines whose
    file no longer exists in warm/ (archived/deleted), ② append a
    "未分类（自动补录）" section for live files the LLM omitted.

    Only a bullet whose leading token is a bare warm-tier filename is eligible
    for deletion. Notes about ``system/x.md`` or ``daily_archive.md`` and
    pointers into ``archive/`` are knowledge, not stale warm entries.
    """
    import re as _re
    from pathlib import Path as _P
    warm_dir = _P(warm_dir)
    live = {f.name for f in warm_dir.glob("*.md")} - {"_index.md"}
    archived = {f.name for f in warm_dir.glob("archive/*.md")}
    kept_lines, mentioned = [], set()
    for line in index_content.splitlines():
        refs = _re.findall(
            r"([A-Za-z0-9_\-./]*?[A-Za-z0-9_\-.]+\.md)", line,
        )
        entry_refs = [ref for ref in refs if _P(ref).name != "_index.md"]
        entry_names = [_P(ref).name for ref in entry_refs]
        head = _re.sub(r"^-\s*", "", line.lstrip())
        head = _re.sub(r"^(?:[📦⭐✂🔥⛔]\ufe0f?\s*)+", "", head)
        match = _re.match(
            r"^(?:[`*_]+)?(?P<ref>[A-Za-z0-9_\-./]+\.md)"
            r"(?:[`*_]+)?(?:\s*(?:[—–:：]|$))",
            head,
        )
        leading_ref = match.group("ref") if match else ""
        owns_entry = (
            line.lstrip().startswith("-")
            and bool(leading_ref)
            and "/" not in leading_ref
        )
        if (owns_entry
                and _P(leading_ref).name not in live
                and _P(leading_ref).name not in archived):
            print(f"[memory-tidy] index entry dropped (file gone): "
                  f"{leading_ref}", file=sys.stderr)
            continue
        mentioned.update(entry_names)
        kept_lines.append(line)
    missing = sorted(live - mentioned)
    if missing:
        kept_lines.append("")
        kept_lines.append("## 未分类（自动补录：上次再生成漏掉的现存文件）")
        for name in missing:
            kept_lines.append(f"- {name}")
        print(f"[memory-tidy] index: appended {len(missing)} omitted files",
              file=sys.stderr)
    return "\n".join(kept_lines) + "\n"


def _process(raw: str) -> int:
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
        index_content = _verify_index(index_content, INDEX_FILE.parent)
        INDEX_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = INDEX_FILE.with_suffix(".tmp")
        tmp.write_text(index_content, encoding="utf-8")
        os.replace(tmp, INDEX_FILE)
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
