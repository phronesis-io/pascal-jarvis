#!/usr/bin/env python3
"""Validate one Memory Compiler result and surface only real conflicts.

The model is an extractor, not the memory authority. Exact source quotes,
coverage, Matter scope, lifecycle changes, and conflict creation are enforced
by ``core.memory_compiler``. Ordinary progress stays silent.

Envelope first, idle second: the batch quotes owner-operated Claude Code and
Codex transcripts about Jarvis itself, so a valid envelope can carry the
literal idle token inside a quoted source. A bare substring test for the
token threw such envelopes away, left the batch pending, and re-ran the same full
sonnet call every ten minutes (28.1h / 143 calls on 2026-08-28, 4.7h on
2026-08-29). An idle reply while a batch is pending is now said out loud on
stderr (jarvis.log) instead of passing as a silent success.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.memory_compiler import (
    MemoryCompilerError,
    apply_compile_result,
    compiler_status,
)
from core.safety import is_idle_reply, looks_like_error, parse_json_response


def _pending_batches() -> int:
    try:
        return int(compiler_status().get("pending_batches") or 0)
    except Exception:  # noqa: BLE001 - status is diagnostic only
        return 0


def main() -> int:
    raw = sys.stdin.read().strip()
    envelope = parse_json_response(raw)
    if envelope is None:
        if is_idle_reply(raw):
            pending = _pending_batches()
            if pending:
                print(
                    "[memory-compiler] idle reply while "
                    f"{pending} compile batch(es) stay pending; will replay",
                    file=sys.stderr,
                )
            return 0
        if looks_like_error(raw):
            print("[memory-compiler] provider output looks like an error", file=sys.stderr)
            return 1
    try:
        receipt = apply_compile_result(envelope if envelope is not None else raw)
    except (MemoryCompilerError, KeyError, ValueError) as exc:
        print(f"[memory-compiler] rejected output: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(receipt, ensure_ascii=False), file=sys.stderr)
    conflicts = receipt.get("new_conflict_ids") or []
    if conflicts:
        print("TITLE: 🧠 有一条记忆需要核对")
        print("WORKED: 已逐条核对来源并暂停使用互相冲突的说法")
        print(
            f"发现 {len(conflicts)} 组关于同一件事的相反记录。"
            "它们不会进入后续上下文；处理相关事项时再确认即可。"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
