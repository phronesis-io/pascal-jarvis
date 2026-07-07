"""Delivery-alert dead-letter file — the alarm that survives the loop.

Stability backlog #7 (6/15 brain-death postmortem): the REQ-11 delivery
ledger alerts the user about missed deliveries — but the alert rides the
SAME Lark channel that is failing, from inside the SAME heartbeat loop that
may be dead. When the loop dies, the alarm dies with the patient. This
module is the producer half of the fix: the in-loop tracker writes each
overdue delivery to an independent dead-letter file that daemon.py (a
separate process with its own Claude-independent notify channel) watches.

Cross-process contract (design-review trap: the daemon does read+unlink
WITHOUT flock on some files — that race is exactly what this dedicated
file must avoid):
  - PRODUCER (this module, called from the heartbeat loop): opens
    data/.delivery_deadletter.jsonl in append mode and holds
    fcntl.flock(LOCK_EX) for the duration of every write.
  - CONSUMER (daemon side, wired separately): MUST take fcntl.flock(LOCK_EX)
    on the same file before read + truncate, and MUST truncate in place
    (keeping the inode) rather than read+unlink — an unlink would divert a
    concurrent locked append onto a dead inode and silently lose the alarm.

One JSON line per overdue delivery:
    {"ts": "...", "kind": "...", "detail": "...", "due_since": "..."}
where `ts` is the append time, `kind` names the alarm class (e.g.
"delivery_failures", "night_queue_expired"), `detail` is a short human
string, and `due_since` is the local "YYYY-MM-DD HH:MM" time the delivery
first became due.
"""

from __future__ import annotations

import fcntl
import json
from pathlib import Path

from core.log import log
from core.timeutil import now_local_str

DEADLETTER_FILE = "data/.delivery_deadletter.jsonl"
# Producer-side backstop: until the daemon consumer is wired (and whenever it
# is down), the file must not grow unbounded — that failure class is the whole
# point of this backlog batch. Trimmed under the same flock as the append.
DEADLETTER_MAX_LINES = 500


def record_overdue(jarvis_dir: str | Path, kind: str, detail: str,
                   due_since: str) -> bool:
    """Append one overdue-delivery alarm line. Never raises.

    Returns True when the line hit the file — callers may still send their
    own in-band alert; this is the out-of-band copy that survives them.
    """
    path = Path(jarvis_dir) / DEADLETTER_FILE
    row = {"ts": now_local_str("%Y-%m-%d %H:%M:%S"),
           "kind": str(kind)[:64],
           "detail": str(detail)[:500],
           "due_since": str(due_since)[:64]}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a+", encoding="utf-8") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                # Backstop trim (see DEADLETTER_MAX_LINES). In-place truncate
                # + rewrite keeps the inode — same rationale as _trim_file in
                # core.heartbeat_loop.
                f.seek(0)
                lines = f.read().splitlines()
                if len(lines) >= DEADLETTER_MAX_LINES:
                    f.seek(0)
                    f.truncate()
                    f.write("\n".join(lines[-(DEADLETTER_MAX_LINES // 2):]) + "\n")
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
        return True
    except OSError as e:
        log("deadletter", f"append failed ({kind}): {e}", level="warn")
        return False
