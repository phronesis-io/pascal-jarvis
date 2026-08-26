#!/usr/bin/env python3
"""Post-hook: send evening reflection as Lark card + log observed patterns."""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.card import build_rich_card
from core.log import log as _structured_log
from core.safety import looks_like_error, parse_json_response, summarize
from core.jsonl import read_jsonl, write_jsonl
from core.timeutil import now_local_str
from core.journal import append_entry

MEMORY_DIR = Path(os.environ.get("MEMORY_DIR", Path.home() / ".jarvis" / "memory"))
PATTERNS_FILE = MEMORY_DIR / "system" / "patterns.jsonl"
MAX_PATTERNS = 50


def _log_failure(message: str, exc: Exception) -> None:
    _structured_log(
        "daily-reflect",
        message,
        level="error",
        error_type=type(exc).__name__,
    )


def main() -> int:
    raw = sys.stdin.read().strip()
    if not raw or raw == "HEARTBEAT_OK":
        return 0

    # Parse JSON response
    data = parse_json_response(raw)
    if data is not None and not isinstance(data, dict):
        _structured_log(
            "daily-reflect",
            "invalid_response_envelope",
            level="error",
            response_type=type(data).__name__,
        )
        return 0
    if isinstance(data, dict):
        message = data.get("user_message", "")
        raw_patterns = data.get("patterns_noted", [])
        patterns = [
            item.strip()
            for item in raw_patterns
            if isinstance(item, str) and item.strip()
        ] if isinstance(raw_patterns, list) else []
    else:
        if looks_like_error(raw):
            return 0
        message = raw
        patterns = []

    if not isinstance(message, str) or not message.strip():
        return 0
    message = message.strip()

    # Log patterns if any were noted
    if patterns:
        try:
            existing = read_jsonl(PATTERNS_FILE)
            for pattern in patterns:
                existing.append({
                    "date": now_local_str("%Y-%m-%d"),
                    "pattern": pattern,
                })
            write_jsonl(PATTERNS_FILE, existing[-MAX_PATTERNS:])
        except Exception as exc:
            _log_failure("pattern_store_failed", exc)

    # Persist the reflection into the longitudinal 《Jarvis 日志》 (append-only,
    # fully guarded). Only journal a cleanly-PARSED reflection — never a raw/
    # error blob from a parse-fallback, which would pollute the journal.
    if isinstance(data, dict):
        try:
            append_entry(message)
        except Exception as exc:
            _log_failure("journal_append_failed", exc)

    # Output as Lark card with richview for full reflection
    date_str = now_local_str("%Y-%m-%d")
    summary = summarize(message)

    sections = [{"type": "markdown", "content": message}]
    if patterns:
        sections.append({"type": "kv", "items": {f"模式 {i+1}": p for i, p in enumerate(patterns)}})

    print(build_rich_card(
        header="🌙 回顾",
        summary=summary,
        sections=sections,
        meta={"source": "daily_reflect", "date": date_str},
        source="daily-reflect",
        work_receipt="汇总当日记录、提炼模式并写入纵向日志",
    ))

    # Stamp today so the pre-script skips on restart (dedup)
    jarvis_dir = Path(os.environ.get("JARVIS_DIR", Path(__file__).resolve().parent.parent))
    stamp = jarvis_dir / "data" / ".daily_reflect_stamp"
    try:
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.write_text(now_local_str("%Y-%m-%d"), encoding="utf-8")
    except Exception as exc:
        _log_failure("dedup_stamp_failed", exc)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
