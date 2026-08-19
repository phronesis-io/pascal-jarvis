#!/usr/bin/env python3
"""Post-hook: apply each due Routine's output under its autonomy contract.

The model writes content. This hook decides what happens to it, using the
routine's *stored* autonomy level — never a level the model claims for itself:

  observe  → recorded in the audit trail, delivered to nobody
  propose  → one memorial card, consequences need 批红
  act      → same card, plus the allow-listed internal actions it asked for

Every run claimed by the pre-hook is closed here, including ones the model
forgot to mention (closed as `no_output`). A run left `running` is how a task
dies silently, so that path is not allowed to exist.

Input (stdin): {"routines": {"<run_id>": {"title": "...", "body": "...",
    "work_receipt": "what was completed first", "actions": [...]}}}
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.routines import (  # noqa: E402
    apply_run_result,
    defer_inflight_infrastructure,
)
from core.safety import parse_json_response  # noqa: E402


def main() -> int:
    raw = sys.stdin.read().strip()
    if raw == "__CALL_FAILED__":
        result = defer_inflight_infrastructure("模型调用失败")
        if result["deferred"]:
            print(f"[routine-run] infrastructure failure deferred: "
                  f"{result['deferred']}", file=sys.stderr)
        return 0

    # '__NO_ENVELOPE__' is the ACK_REQUIRED_TASKS contract: the Claude call
    # answered without a usable Routine slice.  Infrastructure failures use
    # the distinct __CALL_FAILED__ path above and preserve the occurrence.
    if not raw or raw == "__NO_ENVELOPE__" or "HEARTBEAT_OK" in raw:
        # Nothing usable came back, but runs were already claimed. Closing them
        # as no_output keeps the audit honest and re-arms the next occurrence.
        apply_run_result({})
        return 0

    payload = parse_json_response(raw)
    if not isinstance(payload, dict):
        print("[routine-run] JSON parse failed", file=sys.stderr)
        apply_run_result({})
        return 0

    try:
        results = apply_run_result(payload)
    except Exception as exc:  # never let a bad run wedge the heartbeat
        print(f"[routine-run] apply failed: {exc}", file=sys.stderr)
        return 0

    for r in results:
        print(f"[routine-run] {r['run_id']} → {r['status']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
