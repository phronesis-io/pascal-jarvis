"""Scheduler event log — append-only JSONL replay trail for the heartbeat.

Motivation (2026-06-12 incident): when daily-plan output slipped into the
10:00 batch digest, the only forensic trail was the outbox + jarvis.log prose,
which had to be reverse-engineered by hand. This log records every scheduling
decision as one structured JSONL line so an incident can be REPLAYED:

    08:12:59 task_spawn   daily-plan  run=68e08db6
    08:13:41 task_finish  daily-plan  run=68e08db6 status=ok duration_s=41.8
    08:13:41 task_skip    daily-plan  reason=queued_quiet_hours
    10:00:07 batch_flush              count=3

Design (borrowed from Kron's scheduler event log, self-hosted here):
- Events: task_spawn, task_finish (duration + exit status), task_timeout,
  task_skip (reason: silent_output / queued_quiet_hours / overlap_lock /
  circuit_open / empty_pre / batch_deferred / duplicate_send / ...),
  batch_flush (entry count). Every entry carries ts / event / task / run_id
  (run_id == the heartbeat cycle_id, the existing correlation key).
- Hot-path append (same convention as engagement_log.jsonl): writers are
  serialized by the heartbeat flock, so plain `open(path, "a")` is safe.
- emit() NEVER raises — a broken event log must not take down the scheduler.
  Failures degrade to a stderr warning.
- Size-guarded: when the file exceeds MAX_BYTES it is rotated to `.1`
  (one generation kept), checked at write time.

Replay CLI:
    python3 -m core.sched_events [--dir JARVIS_DIR] \
        [--since "2026-06-12 08:00"] [--until "2026-06-12 10:30"] \
        [--task daily-plan] [--event task_skip] [--limit 200]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from core.timeutil import now_local_str

SCHED_EVENT_FILE = "sched_events.jsonl"
MAX_BYTES = 10 * 1024 * 1024  # rotate to .1 past this size
TS_FMT = "%Y-%m-%d %H:%M:%S"  # second precision — replay needs ordering


def _rotate_if_oversized(path: Path) -> None:
    """Keep one `.1` generation once the live file exceeds MAX_BYTES."""
    try:
        if path.exists() and path.stat().st_size > MAX_BYTES:
            os.replace(path, path.with_name(path.name + ".1"))
    except OSError:
        pass  # rotation is best-effort; the append below still proceeds


def emit(jarvis_dir, event: str, task: str = "", run_id: str = "", **fields) -> None:
    """Append one scheduler event. NEVER raises — scheduling must not be
    affected by a logging failure (disk full, permissions, weird path).
    """
    try:
        path = Path(jarvis_dir) / SCHED_EVENT_FILE
        _rotate_if_oversized(path)
        entry = {"ts": now_local_str(TS_FMT), "event": event,
                 "task": task, "run_id": run_id}
        entry.update(fields)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:  # noqa: BLE001 — by contract, swallow everything
        try:
            print(f"[warn] sched_events: failed to write {event!r}: {e}",
                  file=sys.stderr)
        except Exception:
            pass


def query(jarvis_dir, since: str = "", until: str = "",
          task: str = "", event: str = "") -> list[dict]:
    """Read events (rotated generation first, then live file) with filters.

    `since`/`until` are compared as zero-padded local-time strings, so any
    prefix of "YYYY-MM-DD HH:MM:SS" works: since="2026-06-12 08:00" includes
    everything from 08:00:00; until="2026-06-12 10:30" is inclusive of the
    whole 10:30 minute. `task` matches the task field exactly, or as one
    member of a comma-separated multi-source task value.
    """
    base = Path(jarvis_dir) / SCHED_EVENT_FILE
    out: list[dict] = []
    for path in (base.with_name(base.name + ".1"), base):
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue  # one bad line must not lose the rest of the replay
            ts = str(e.get("ts", ""))
            if since and ts < since:
                continue
            if until and ts[:len(until)] > until:
                continue
            if event and e.get("event") != event:
                continue
            if task:
                names = {s.strip() for s in str(e.get("task", "")).split(",")}
                if task not in names:
                    continue
            out.append(e)
    return out


def format_timeline(entries: list[dict]) -> str:
    """One aligned line per event — the replay view."""
    lines = []
    for e in entries:
        extras = " ".join(
            f"{k}={json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v}"
            for k, v in e.items()
            if k not in ("ts", "event", "task", "run_id"))
        rid = f"run={e.get('run_id')}" if e.get("run_id") else ""
        lines.append(" ".join(filter(None, [
            str(e.get("ts", "")),
            f"{e.get('event', ''):<13}",
            f"{e.get('task', '') or '-':<22}",
            rid, extras,
        ])).rstrip())
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Replay the heartbeat scheduler event log.")
    ap.add_argument("--dir", default=os.environ.get("JARVIS_DIR", "."),
                    help="jarvis dir holding sched_events.jsonl (default: $JARVIS_DIR or .)")
    ap.add_argument("--since", default="", help='e.g. "2026-06-12 08:00"')
    ap.add_argument("--until", default="", help='e.g. "2026-06-12 10:30" (inclusive)')
    ap.add_argument("--task", default="", help="filter by task name")
    ap.add_argument("--event", default="", help="filter by event type")
    ap.add_argument("--limit", type=int, default=0,
                    help="print only the last N matching events")
    args = ap.parse_args(argv)

    entries = query(args.dir, since=args.since, until=args.until,
                    task=args.task, event=args.event)
    if args.limit > 0:
        entries = entries[-args.limit:]
    if not entries:
        print("(no matching scheduler events)")
        return 0
    print(format_timeline(entries))
    return 0


if __name__ == "__main__":
    sys.exit(main())
