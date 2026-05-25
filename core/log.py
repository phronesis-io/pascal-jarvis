"""Structured JSON logger for Jarvis.

All components should use this instead of bare print() to stderr.
Output format: one JSON object per line (JSONL), compatible with jq.

Usage:
    from core.log import log
    log("heartbeat", "calling_claude", tasks=4, cycle="a3f2b1c9")
    # → {"ts":"2026-05-25T14:06:03","level":"info","component":"heartbeat",
    #    "msg":"calling_claude","tasks":4,"cycle":"a3f2b1c9"}
"""

from __future__ import annotations

import json
import sys

from core.timeutil import now_local_str


def log(component: str, msg: str, level: str = "info", file=None, **kwargs):
    """Emit a structured log line to stderr (or specified file).

    Args:
        component: Source module (e.g. "heartbeat", "bot", "daemon")
        msg: Human-readable message (snake_case preferred for grep-ability)
        level: "info", "warn", "error"
        **kwargs: Additional structured fields (task count, cycle_id, etc.)
    """
    entry = {
        "ts": now_local_str("%Y-%m-%dT%H:%M:%S"),
        "level": level,
        "component": component,
        "msg": msg,
    }
    entry.update(kwargs)
    output = file or sys.stderr
    print(json.dumps(entry, ensure_ascii=False), file=output)
