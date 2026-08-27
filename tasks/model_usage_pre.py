#!/usr/bin/env python3
"""Refresh the sanitized model-runtime usage snapshot without a model call."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.model_usage import build_report


def main() -> int:
    try:
        report = build_report()
    except Exception as exc:
        print(f"[model-usage] refresh failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
