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


_CONTEXT_DEPENDENT_OWNER_EXACT = {
    "好", "好的", "可以", "可以的", "行", "嗯", "嗯嗯", "对", "同意",
    "通过", "确认", "确认发布", "继续", "开始", "开始吧", "搞吧", "做吧",
    "改吧", "发吧", "上吧", "来吧", "开干", "就这样", "按这个", "就按这个",
    "照这个来", "全部做", "全部做完", "都做", "全部同意", "都同意",
    "没问题",
    "ok", "okay", "yes", "go ahead", "do it", "ship it", "approved",
    "继续修", "继续优化", "继续检查", "sounds good", "continue", "proceed",
}
_CONTEXT_DEPENDENT_OWNER_DEICTIC = re.compile(
    r"^(?:(?:那就|就)(?:把)?|把)?"
    r"(?:这个|这些|上面(?:的)?|前面(?:的)?|刚才(?:的)?|这样|"
    r"按(?:这个|上面|建议)).{0,28}(?:吧)?$",
    re.IGNORECASE,
)
_CONTEXT_DEPENDENT_OWNER_DESTINATION = re.compile(
    r"^(?:写|放|加|发|贴|存|同步|更新|部署)(?:进|到|在|上)"
    r"[^。！？!?]{1,28}吧$",
    re.IGNORECASE,
)


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


def context_dependent_owner_text(value: Any) -> bool:
    """Return true when an owner turn cannot authorize a claim by itself.

    Short acknowledgements are meaningful inside a conversation, but their
    referent lives in the previous turn. An exact quote therefore proves that
    the owner said the words, not the model-expanded decision attached to them.
    """
    text = normalized(value)
    if re.search(r"[?？]\s*$", text):
        return True
    text = re.sub(r"[。！!？?，,；;：:]+$", "", text).strip()
    if text in _CONTEXT_DEPENDENT_OWNER_EXACT:
        return True
    return bool(
        _CONTEXT_DEPENDENT_OWNER_DEICTIC.fullmatch(text)
        or _CONTEXT_DEPENDENT_OWNER_DESTINATION.fullmatch(text)
    )


def source_authority(role: Any, text: Any) -> str:
    return (
        "owner_asserted"
        if source_activation_policy(role, text) == "owner_asserted"
        else "assistant_candidate"
    )


def source_activation_policy(role: Any, text: Any) -> str:
    role_name = normalized(role)
    if role_name != "user":
        return "assistant_candidate"
    if context_dependent_owner_text(text):
        return "owner_context_candidate"
    return "owner_asserted"


def claim_key(value: Any) -> str:
    key = normalized(value)
    if not key:
        raise MemoryCompilerError("claim_key is required")
    if len(key) > 200:
        raise MemoryCompilerError("claim_key exceeds 200 characters")
    return key
