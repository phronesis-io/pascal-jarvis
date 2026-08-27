"""Shared contracts and normalization helpers for compiled memory."""

from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Any

from core.cross_session_parsing import redact_text


INPUT_SCHEMA = "jarvis.memory-compile-input.v1"
OUTPUT_SCHEMA = "jarvis.memory-candidates.v1"
CONTEXT_SCHEMA = "jarvis.compiled-memory.v1"
VALID_KINDS = {
    "fact", "decision", "artifact", "todo", "constraint", "preference",
}
VALID_STATUSES = {
    "candidate", "active", "conflicted", "superseded", "rejected",
}
AUTO_SUPERSEDE_KINDS = {"decision", "preference", "todo"}
DEFAULT_BATCH_SIZE = 16
SOURCE_SCAN_PAGE_SIZE = 1000
MAX_CLAIMS_PER_SOURCE = 3
MAX_CONTEXT_CLAIMS = 16
MAX_CONTEXT_CHARS = 6000


class MemoryCompilerError(ValueError):
    """A compile envelope violates the source or lifecycle contract."""


def db():
    from core.db import get_db
    return get_db()


def now(value: float | None = None) -> float:
    return float(time.time() if value is None else value)


def json_text(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )


def decode(value: Any, default: Any) -> Any:
    try:
        result = json.loads(str(value or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return default
    return result if isinstance(result, type(default)) else default


def digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def flat(value: Any, limit: int = 4000) -> str:
    text = " ".join(str(value or "").split()).strip()
    return redact_text(text, limit=limit)


def normalized(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip()).casefold()


def claim_key(value: Any) -> str:
    key = normalized(value)
    if not key:
        raise MemoryCompilerError("claim_key is required")
    if len(key) > 200:
        raise MemoryCompilerError("claim_key exceeds 200 characters")
    return key
