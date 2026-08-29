#!/usr/bin/env python3
"""Post-hook: save daily summary, archive and clear hourly log.

Also extracts any '→ UPDATE: filename.md: content' directives Claude may have
emitted and applies them directly to target memory files.
Entries older than 14 days in daily_log are archived to daily_archive.
"""
import fcntl
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.safety import is_idle_reply, looks_like_error
from core.timeutil import now_local_str

MEMORY_DIR = Path(os.environ.get("MEMORY_DIR",
    Path.home() / ".jarvis" / "memory"))
HOURLY_LOG = MEMORY_DIR / "timeline" / "hourly_log.md"
HOURLY_ARCHIVE = MEMORY_DIR / "timeline" / "hourly_archive.md"
DAILY_LOG = MEMORY_DIR / "timeline" / "daily_log.md"
DAILY_ARCHIVE = MEMORY_DIR / "timeline" / "daily_archive.md"
ARCHIVE_DAYS = 14


def _append_daily_index(index: str, ts_date: str) -> None:
    """Insert one summary under a single date heading, idempotently.

    Daily retries used to append another ``## YYYY-MM-DD`` block every time.
    Besides confusing recall, duplicate headings made the tidy task repeatedly
    report the same problem. Merge all existing same-day bodies under the first
    heading and add the new summary only when it is not already present.
    """
    DAILY_LOG.parent.mkdir(parents=True, exist_ok=True)
    lock_path = DAILY_LOG.with_suffix(".lock")
    with lock_path.open("a") as lock_fh:
        fcntl.flock(lock_fh, fcntl.LOCK_EX)
        content = (DAILY_LOG.read_text(encoding="utf-8")
                   if DAILY_LOG.exists() else "")
        matches = list(re.finditer(
            r"(?m)^## (\d{4}-\d{2}-\d{2})\s*$", content
        ))
        preamble = content[:matches[0].start()] if matches else content
        sections: list[tuple[str, str]] = []
        for pos, match in enumerate(matches):
            end = matches[pos + 1].start() if pos + 1 < len(matches) else len(content)
            sections.append((match.group(1), content[match.end():end].strip()))

        same_day = [body for day, body in sections if day == ts_date and body]
        if index.strip() and not any(index.strip() == body for body in same_day):
            same_day.append(index.strip())

        rebuilt: list[str] = [preamble.rstrip()]
        inserted = False
        for day, body in sections:
            if day == ts_date:
                if inserted:
                    continue
                body = "\n\n".join(dict.fromkeys(same_day))
                inserted = True
            rebuilt.append(f"## {day}\n{body}".rstrip())
        if not inserted:
            rebuilt.append(f"## {ts_date}\n{index.strip()}".rstrip())

        normalized = "\n\n".join(part for part in rebuilt if part).rstrip() + "\n"
        tmp = DAILY_LOG.with_suffix(".tmp")
        tmp.write_text(normalized, encoding="utf-8")
        os.replace(tmp, DAILY_LOG)


def _apply_update(memory_dir: Path, filename: str, content: str, ts: str) -> None:
    """Directly append an update directive to the target memory file."""
    target = memory_dir / filename
    # Path containment check
    try:
        rel = target.resolve().relative_to(memory_dir.resolve())
    except ValueError:
        print(f"[memory-daily] BLOCKED path traversal attempt: {filename}", file=sys.stderr)
        return
    if rel.parts and rel.parts[0] == "warm":
        # warm/ 用户画像以 auto-memory 为准 (CLAUDE.md): under the heartbeat
        # MEMORY_DIR the local warm/ is a replica that only memory-tidy's
        # one-way sync may write — an in-place write here is destroyed by the
        # next newer-wins sync (2026-07-09 red-team [12]). Function-level
        # import: memory_tidy_post imports from this module, so a top-level
        # import of the consolidate→tidy chain would be circular.
        from tasks.memory_consolidate_post import _canon_warm_target
        target = _canon_warm_target(memory_dir, rel, target, task="memory-daily")
    if not target.exists():
        print(f"[memory-daily] skipping update for {filename} — file does not exist", file=sys.stderr)
        return
    content = content.strip()
    if not content:
        # Same empty-bullet tombstone guard as memory-consolidate (2026-07-08
        # audit). Rare here — this regex's \s* slurps the next line instead —
        # but a directive at end-of-output can still strip to empty.
        print(f"[memory-daily] skipping update for {filename} — empty content", file=sys.stderr)
        return
    try:
        with target.open("a", encoding="utf-8") as f:
            f.write(f"\n<!-- auto-update {ts} -->\n- {content}\n")
    except OSError as e:
        print(f"[memory-daily] failed to write {filename}: {e}", file=sys.stderr)


def main() -> int:
    summary = sys.stdin.read().strip()
    if is_idle_reply(summary):
        return 0
    if looks_like_error(summary) or len(summary) < 10:
        print("[memory-daily] skipping — output looks like error/noise", file=sys.stderr)
        return 0

    ts_date = now_local_str("%Y-%m-%d")

    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    (MEMORY_DIR / "timeline").mkdir(parents=True, exist_ok=True)
    (MEMORY_DIR / "system").mkdir(parents=True, exist_ok=True)

    # Apply UPDATE directives directly to target files
    updates = re.findall(r'→ UPDATE:\s*(\S+\.md):\s*(.+)', summary)
    if updates:
        for filename, content in updates:
            _apply_update(MEMORY_DIR, filename, content, ts_date)

    # Strip UPDATE lines from the index we'll write to daily_log
    index_lines = [l for l in summary.splitlines() if not l.startswith("→ UPDATE:")]
    index = "\n".join(index_lines).strip()

    # Append to daily log
    if index:
        _append_daily_index(index, ts_date)

    # Archive hourly log before clearing
    if HOURLY_LOG.exists() and HOURLY_LOG.stat().st_size > 0:
        with HOURLY_ARCHIVE.open("a", encoding="utf-8") as f:
            f.write(f"\n# Archived {ts_date}\n{HOURLY_LOG.read_text(encoding='utf-8')}\n")
        HOURLY_LOG.write_text("")

    # Auto-archive daily_log entries older than 14 days
    _archive_old_daily_entries(ts_date)

    print(f"[memory] Daily index saved for {ts_date}.", file=sys.stderr)
    return 0


def _archive_old_daily_entries(today_str: str) -> None:
    """Move entries older than ARCHIVE_DAYS from daily_log to daily_archive."""
    if not DAILY_LOG.exists():
        return
    content = DAILY_LOG.read_text(encoding="utf-8")
    if not content.strip():
        return

    try:
        cutoff = datetime.strptime(today_str, "%Y-%m-%d") - timedelta(days=ARCHIVE_DAYS)
    except ValueError:
        return

    # Split by ## date headers
    sections = re.split(r'(^## \d{4}-\d{2}-\d{2})', content, flags=re.MULTILINE)

    keep = []
    archive = []
    i = 0
    # First section (before any ## header) — keep as preamble
    if sections and not sections[0].startswith("## "):
        keep.append(sections[0])
        i = 1

    while i < len(sections) - 1:
        header = sections[i]
        body = sections[i + 1] if i + 1 < len(sections) else ""
        date_match = re.match(r'## (\d{4}-\d{2}-\d{2})', header)
        if date_match:
            try:
                entry_date = datetime.strptime(date_match.group(1), "%Y-%m-%d")
                if entry_date < cutoff:
                    archive.append(header + body)
                else:
                    keep.append(header + body)
            except ValueError:
                keep.append(header + body)
        else:
            keep.append(header + body)
        i += 2

    if not archive:
        return

    # Append archived entries to daily_archive
    with DAILY_ARCHIVE.open("a", encoding="utf-8") as f:
        f.write("".join(archive))

    # Rewrite daily_log with only recent entries (atomic)
    tmp = DAILY_LOG.with_suffix(".tmp")
    tmp.write_text("".join(keep), encoding="utf-8")
    os.replace(tmp, DAILY_LOG)
    print(f"[memory] Archived {len(archive)} old entries from daily_log", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
