"""Report EigenFlux auto-replies into the silent activity log.

tasks/activity_log_pre.sh runs this as ``python3 -m core.autoreply_activity``.
Auto-replies raise no card, so this report is the only place the owner's day
shows the outbound side of the 2026-08-20 "有些你可以自动回复掉吧" feature.

The reader keeps a byte-offset high-water cursor beside the ledger: every run
reports ALL rows since the last consumed point, and the cursor only advances
over rows that were actually emitted. A wall-clock window here would
permanently drop whatever was sent while the shell script's hourly gate
(08:00–23:00) was closed — the gate may only delay a row, never lose it.

This module and core.ef_stream_loop share LEDGER_RELPATH and TS_FORMAT, so
the writer and the reader cannot drift apart again (red team 2026-08-21: the
writer stamped '%Y-%m-%d %H:%M' while the reader parsed '%Y-%m-%dT%H:%M:%S',
so 100% of rows were silently skipped and the outbound side was invisible).

Stdlib only: the shell hook must not depend on the daemon's import graph.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime
from pathlib import Path

LEDGER_RELPATH = "data/ef_autoreply_ledger.jsonl"
CURSOR_RELPATH = "data/ef_autoreply_ledger.cursor"
TS_FORMAT = "%Y-%m-%dT%H:%M:%S"
MAX_ROWS = 50
_HEAD_SPAN = 256  # fingerprinted prefix; detects truncate-and-regrow


def _read_cursor(cursor_path: Path) -> tuple[int, int, str]:
    """Return (offset, head_len, head_sha256); zeros on any damage."""
    try:
        raw = cursor_path.read_text(encoding="utf-8").strip()
    except OSError:
        return 0, 0, ""
    try:
        state = json.loads(raw)
        return (
            max(0, int(state.get("offset", 0))),
            max(0, int(state.get("head_len", 0))),
            str(state.get("head", "")),
        )
    except (TypeError, ValueError, json.JSONDecodeError):
        return 0, 0, ""


def _write_cursor(cursor_path: Path, offset: int, ledger_path: Path) -> None:
    head_len = min(_HEAD_SPAN, int(offset))
    head = ""
    if head_len:
        try:
            with ledger_path.open("rb") as handle:
                head = hashlib.sha256(handle.read(head_len)).hexdigest()
        except OSError:
            head_len = 0
    cursor_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = cursor_path.with_suffix(
        cursor_path.suffix + f".tmp.{os.getpid()}"
    )
    temporary.write_text(
        json.dumps({
            "offset": int(offset), "head_len": head_len, "head": head,
        }),
        encoding="utf-8",
    )
    temporary.replace(cursor_path)


def _cursor_matches_ledger(ledger_path: Path, size: int,
                           offset: int, head_len: int, head: str) -> bool:
    """False when the ledger is no longer the file the cursor described."""
    if offset > size:
        return False
    if not head_len:
        return True  # nothing consumed yet, or a legacy cursor — trust offset
    if head_len > size:
        return False
    try:
        with ledger_path.open("rb") as handle:
            current = hashlib.sha256(handle.read(head_len)).hexdigest()
    except OSError:
        return False
    return current == head


def _format_row(raw_line: str) -> str:
    """One display line per ledger row; "" for a row that cannot render.

    A timestamp that fails to parse is shown raw instead of dropping the
    row — a formatting bug must never make an outbound message invisible.
    """
    try:
        row = json.loads(raw_line)
    except (json.JSONDecodeError, TypeError, ValueError):
        return ""
    if not isinstance(row, dict):
        return ""
    who = str(row.get("title", "")).replace(" 来信", "").strip() or "peer"
    note = str(row.get("note") or "").strip() or str(row.get("reply", ""))[:60]
    stamp = str(row.get("ts", ""))
    try:
        stamp = datetime.strptime(stamp[:19], TS_FORMAT).strftime("%m-%d %H:%M")
    except ValueError:
        stamp = stamp[:16]
    prefix = f"[{stamp}] " if stamp else ""
    return f"  {prefix}{who}: {note}"


def report(jarvis_dir: str | Path, *, consume: bool = True) -> str:
    """Return unconsumed auto-reply rows as a prompt block ("" when none)."""
    root = Path(jarvis_dir)
    ledger = root / LEDGER_RELPATH
    cursor = root / CURSOR_RELPATH
    try:
        size = ledger.stat().st_size
    except OSError:
        return ""
    offset, head_len, head = _read_cursor(cursor)
    if not _cursor_matches_ledger(ledger, size, offset, head_len, head):
        # The ledger shrank or was rewritten (rotation/manual cleanup).
        # Restart from the top — re-reporting a row is acceptable, skipping
        # one is not.
        offset = 0
    if offset >= size:
        return ""
    try:
        with ledger.open("rb") as handle:
            handle.seek(offset)
            chunk = handle.read(size - offset)
    except OSError:
        return ""
    # Only newline-terminated lines are consumed; a row the writer is still
    # appending stays unconsumed for the next run.
    end = chunk.rfind(b"\n")
    if end < 0:
        return ""
    consumed = chunk[: end + 1]
    lines = [
        formatted
        for line in consumed.decode("utf-8", errors="replace").splitlines()
        if line.strip()
        for formatted in (_format_row(line),)
        if formatted
    ]
    if consume:
        _write_cursor(cursor, offset + len(consumed), ledger)
    if not lines:
        return ""
    body = "\n".join(lines[:MAX_ROWS])
    if len(lines) > MAX_ROWS:
        body += f"\n  … and {len(lines) - MAX_ROWS} more"
    return (
        "EIGENFLUX AUTO-REPLIES (Jarvis handled these itself, no card raised;"
        " everything since the last report):\n" + body
    )


def main(argv: list[str] | None = None) -> int:
    _ = argv
    block = report(os.environ.get("JARVIS_DIR", "."))
    if block:
        print(block)
    return 0


if __name__ == "__main__":
    sys.exit(main())
