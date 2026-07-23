"""Channel watermark monitoring (REQ-12).

Every push channel the user depends on (eigenflux feed, checkin,
recommendations, ...) has gone silently dead before — at least 4 outages were
discovered by the user asking "我好久没有收到 feed 了". This module computes
"time since the channel last worked" against its expected cadence so
self-diagnostic can flag a dead channel instead of waiting for a human.

Usage (from tasks/self_diagnostic_pre.sh):
    python3 -m core.watermarks
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from core.heartbeat import parse_heartbeat

# A task is "starved" when it hasn't run for longer than this multiple of its
# expected interval. 2x tolerates one missed slot before alarming.
STARVATION_FACTOR = 2.0

# Fresh-install grace: a collaborator's first-ever self-diagnostic (v1.3.0,
# 2026-07-13) listed SIX "has NEVER run" ⚠️ lines — including self-diagnostic
# reporting ITSELF, whose last_success only lands after its first cycle
# finishes — because last_success==0 alarmed with no notion of how long the
# install had existed. A task that never succeeded is only an outage once the
# install is older than the same 2x-interval bar used for starvation; younger
# than that it is simply a schedule that has not reached the task yet.
INSTALL_STAMP = Path("data") / ".install_stamp"


def _install_ts(jd: Path, now: float) -> float:
    """Mtime of the install stamp; self-heals by creating it on first use
    (existing installs get it stamped `now`, which is harmless — their tasks
    already carry last_success). Falls back to `now` if data/ is unwritable."""
    stamp = jd / INSTALL_STAMP
    try:
        return stamp.stat().st_mtime
    except OSError:
        pass
    try:
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.touch()
        os.utime(stamp, (now, now))
    except OSError:
        pass
    return now


def _fmt_age(seconds: float) -> str:
    if seconds >= 86400:
        return f"{seconds / 86400:.1f}d"
    if seconds >= 3600:
        return f"{seconds / 3600:.1f}h"
    return f"{int(seconds / 60)}min"


# A queue entry is only "stuck" once a batch window has been open this long
# without a flush — one heartbeat cycle plus headroom. Anything younger is the
# queue working as designed (waiting for a window or a user-activity breakpoint).
QUEUE_OVERDUE_GRACE_SECONDS = 15 * 60


def _queue_status_line(jd: Path, queue_depth: int, now: float) -> str:
    """Describe the batch queue: held entries are normal (quiet hours hold for
    the morning digest, daytime general-interest holds for the next batch
    window), and only a flush that failed to fire after a window opened is a
    warning. The old unconditional 'pending morning flush' wording made normal
    daytime batching read as a stuck queue in self-diagnostic."""
    from datetime import datetime

    from core.heartbeat_loop import (BATCH_FLUSH_STAMP, BATCH_WINDOWS_MIN,
                                     _in_quiet_hours)
    # Derive time-of-day from the report's `now` (system-local tz, same clock
    # as the flush stamp) so tests can pin the wall clock.
    t = datetime.fromtimestamp(now)
    mod = t.hour * 60 + t.minute
    if _in_quiet_hours(mod):
        return (f"  Batch queue: {queue_depth} message(s) held for the "
                f"morning digest (quiet hours — normal)")
    try:
        last_flush = float((jd / BATCH_FLUSH_STAMP).read_text().strip())
    except (OSError, ValueError):
        last_flush = 0.0
    passed = [w for w in BATCH_WINDOWS_MIN if mod >= w]
    if passed:
        since_window = (mod - max(passed)) * 60
        flush_predates_window = now - last_flush > since_window + 60
        if flush_predates_window and since_window > QUEUE_OVERDUE_GRACE_SECONDS:
            return (f"  ⚠️ Batch queue: {queue_depth} message(s) STUCK — the "
                    f"{max(passed) // 60:02d}:{max(passed) % 60:02d} window opened "
                    f"{_fmt_age(since_window)} ago without a flush")
    nxt = next((w for w in BATCH_WINDOWS_MIN if w > mod), None)
    when = (f"{nxt // 60:02d}:{nxt % 60:02d}" if nxt is not None
            else f"tomorrow {BATCH_WINDOWS_MIN[0] // 60:02d}:00")
    return (f"  Batch queue: {queue_depth} message(s) awaiting next batch "
            f"window ({when}) or user activity — normal")


def channel_watermark_report(jarvis_dir: str | Path,
                             heartbeat_file: str | Path | None = None,
                             now: float | None = None) -> str:
    """Build a human-readable watermark section for self-diagnostic."""
    jd = Path(jarvis_dir)
    hb = Path(heartbeat_file) if heartbeat_file else jd / "HEARTBEAT.md"
    now = now if now is not None else time.time()

    def _load(path: Path) -> dict:
        try:
            return json.loads(path.read_text())
        except (OSError, json.JSONDecodeError, ValueError):
            return {}

    state = _load(jd / "heartbeat_state.json")
    overrides = _load(jd / "interval_overrides.json")
    install_ts = _install_ts(jd, now)

    starved, circuits, pending_first = [], [], []
    for task in parse_heartbeat(hb):
        name = task["name"]
        ts = state.get(name, {})
        # Same precedence as run_cycle: override → legacy effective_interval
        # in state → HEARTBEAT.md default. Ignoring the legacy field made
        # legitimately slowed-down tasks look STARVED.
        interval = (overrides.get(name)
                    or ts.get("effective_interval", 0)
                    or task["interval"])
        last_run = ts.get("last_run", 0)
        # TRUTH watermark (REQ-51): last_run is a scheduling watermark that
        # gets rewritten on every empty_pre/pre-failure skip, so chronically
        # failing channels looked perpetually fresh (repos-sync: 19/19 pre
        # timeouts, report said 'all channels within expected cadence').
        # last_success is set only on a real ok/idle finish. Fall back to
        # last_run for state predating the field.
        last_success = ts.get("last_success", 0) or last_run
        last_status = ts.get("last_status", "")
        disabled_until = ts.get("circuit", {}).get("disabled_until", 0)

        if disabled_until > now:
            circuits.append(
                f"  ⚠️ {name}: circuit OPEN, re-enables in {_fmt_age(disabled_until - now)}")
            continue
        if last_status.startswith("pre_") and                 ts.get("circuit", {}).get("consecutive_failures", 0) >= 3:
            starved.append(
                f"  ⚠️ {name}: pre-script failing ({last_status} ×"
                f"{ts['circuit']['consecutive_failures']}) — channel data-gathering DEAD")
        if last_success == 0:
            # Within the fresh-install grace (2x the task's own interval since
            # install) a missing first run is the schedule, not an outage —
            # report it as an info line, never a ⚠️ alert.
            if now - install_ts < STARVATION_FACTOR * interval:
                pending_first.append(name)
            else:
                starved.append(f"  ⚠️ {name}: has NEVER run (expected every {_fmt_age(interval)})")
        elif now - last_success > STARVATION_FACTOR * interval:
            starved.append(
                f"  ⚠️ {name}: last real success {_fmt_age(now - last_success)} ago "
                f"(expected every {_fmt_age(interval)}) — STARVED")

    from core.state_projection import delivery_overview
    delivery = delivery_overview(jd)
    if delivery is None:
        delivery = _load(jd / ".delivery_state.json")
    consec_fails = delivery.get("consec_fails", 0)
    delivery_queue_depth = int(delivery.get("queued", 0) or 0)

    night_queue = jd / "night_queue.jsonl"
    try:
        queue_depth = len(night_queue.read_text().splitlines())
    except OSError:
        queue_depth = 0
    queue_line = _queue_status_line(jd, queue_depth, now) if queue_depth else ""

    lines = ["--- Channel Watermarks ---"]
    if starved:
        lines.extend(starved)
    if circuits:
        lines.extend(circuits)
    if pending_first:
        lines.append(
            f"  ○ first run pending (installed {_fmt_age(now - install_ts)} ago"
            f" — normal): {', '.join(pending_first)}")
    if consec_fails > 0:
        lines.append(f"  ⚠️ Lark delivery: {consec_fails} consecutive send failures")
    if delivery_queue_depth:
        lines.append(
            f"  ⚠️ Unified delivery: {delivery_queue_depth} queued item(s)")
    if queue_line:
        lines.append(queue_line)
    if len(lines) == 1:
        lines.append("  ✓ All task channels within expected cadence")
    return "\n".join(lines)


if __name__ == "__main__":
    import os
    print(channel_watermark_report(os.environ.get("JARVIS_DIR", ".")))
