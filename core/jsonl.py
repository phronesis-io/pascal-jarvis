"""Shared JSONL store helpers — read/append rolling JSONL logs safely.

Many post-hooks keep a rolling log (checkin history, activity log, content
recommendations, watch-later items, plan/pattern logs) as one compact JSON
object per line. Each was re-implementing the same read-skip-malformed dance
and the same tmp-write-then-rename, but slightly differently (`.split("\\n")`
vs `.splitlines()`, `with_suffix` vs `with_name`, `os.replace` vs `.replace`).
These helpers make it one tested code path.
"""

from __future__ import annotations

import json
from pathlib import Path

from core.safety import atomic_write


def read_jsonl(path) -> list[dict]:
    """Read a JSONL file into a list of objects.

    Missing file → []. Blank and malformed lines are skipped — logs that grow
    by appends are never guaranteed 100% clean, and one bad line must not lose
    the rest of the history.
    """
    p = Path(path)
    if not p.exists():
        return []
    out: list[dict] = []
    for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except (json.JSONDecodeError, ValueError):
            continue
    return out


def write_jsonl(path, entries) -> None:
    """Atomically write `entries` as JSONL (one compact object per line, UTF-8).

    Goes through core.safety.atomic_write (tmp + rename, parent mkdir) so a
    concurrent reader — the heartbeat or the main session — never sees a
    half-written file.
    """
    body = "\n".join(json.dumps(e, ensure_ascii=False) for e in entries)
    atomic_write(path, body + "\n" if body else "")


def append_jsonl(path, entry, *, keep_last: int | None = None) -> None:
    """Append one entry, optionally trim to the last `keep_last`, write back.

    Convenience for the common "add one row, cap the log" case. Not safe against
    concurrent writers to the *same* file, but heartbeat tasks are serialized,
    so the read-modify-write is fine here.
    """
    entries = read_jsonl(path)
    entries.append(entry)
    if keep_last is not None:
        entries = entries[-keep_last:]
    write_jsonl(path, entries)
