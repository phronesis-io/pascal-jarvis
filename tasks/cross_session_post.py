#!/usr/bin/env python3
"""Validate one Memory Compiler result and surface only real conflicts.

The model is an extractor, not the memory authority. Exact source quotes,
coverage, Matter scope, lifecycle changes, and conflict creation are enforced
by ``core.memory_compiler``. Ordinary progress stays silent.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.memory_compiler import MemoryCompilerError, apply_compile_result
from core.safety import looks_like_error


def main() -> int:
    raw = sys.stdin.read().strip()
    if not raw or "HEARTBEAT_OK" in raw:
        return 0
    if looks_like_error(raw):
        print("[memory-compiler] provider output looks like an error", file=sys.stderr)
        return 1
    try:
        receipt = apply_compile_result(raw)
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
