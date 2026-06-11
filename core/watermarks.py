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
import time
from pathlib import Path

from core.heartbeat import parse_heartbeat

# A task is "starved" when it hasn't run for longer than this multiple of its
# expected interval. 2x tolerates one missed slot before alarming.
STARVATION_FACTOR = 2.0


def _fmt_age(seconds: float) -> str:
    if seconds >= 86400:
        return f"{seconds / 86400:.1f}d"
    if seconds >= 3600:
        return f"{seconds / 3600:.1f}h"
    return f"{int(seconds / 60)}min"


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

    starved, circuits = [], []
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
        disabled_until = ts.get("circuit", {}).get("disabled_until", 0)

        if disabled_until > now:
            circuits.append(
                f"  ⚠️ {name}: circuit OPEN, re-enables in {_fmt_age(disabled_until - now)}")
            continue
        if last_run == 0:
            starved.append(f"  ⚠️ {name}: has NEVER run (expected every {_fmt_age(interval)})")
        elif now - last_run > STARVATION_FACTOR * interval:
            starved.append(
                f"  ⚠️ {name}: last ran {_fmt_age(now - last_run)} ago "
                f"(expected every {_fmt_age(interval)}) — STARVED")

    delivery = _load(jd / ".delivery_state.json")
    consec_fails = delivery.get("consec_fails", 0)

    night_queue = jd / "night_queue.jsonl"
    try:
        queue_depth = len(night_queue.read_text().splitlines())
    except OSError:
        queue_depth = 0

    lines = ["--- Channel Watermarks ---"]
    if starved:
        lines.extend(starved)
    if circuits:
        lines.extend(circuits)
    if consec_fails > 0:
        lines.append(f"  ⚠️ Lark delivery: {consec_fails} consecutive send failures")
    if queue_depth > 0:
        lines.append(f"  Night queue: {queue_depth} message(s) pending morning flush")
    if len(lines) == 1:
        lines.append("  ✓ All task channels within expected cadence")
    return "\n".join(lines)


if __name__ == "__main__":
    import os
    print(channel_watermark_report(os.environ.get("JARVIS_DIR", ".")))
