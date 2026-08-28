"""Deterministic signal classification and Matter reconciliation."""

from __future__ import annotations

import json
import re
from typing import Any

from core.matters import add_event, find_by_entity, link_entity, list_matters

DECISION_SOURCES = {
    "selfmon", "eigenflux-publish", "intention-check", "intentions",
    "approval", "calendar-change",
}
CONVERSATION_TYPES = {"lark_chat", "cli_stream", "direct_message", "pm"}
_DECISION_RE = re.compile(
    r"(是否|请选择|确认|批准|同意|拒绝|要不要|怎么定|需要.*决定|approve|reject|decision)",
    re.I,
)
_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{2,}|[\u4e00-\u9fff]{2,6}")


def _metadata(signal: dict) -> dict:
    value = signal.get("metadata") or {}
    if isinstance(value, dict):
        return value
    try:
        decoded = json.loads(value)
    except (json.JSONDecodeError, TypeError, ValueError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def classify_signal(signal: dict) -> str:
    """Classify into information, conversation, or decision."""
    metadata = _metadata(signal)
    explicit = str(metadata.get("route") or signal.get("route") or "").lower()
    if explicit in {"information", "conversation", "decision"}:
        return explicit
    source = str(signal.get("source_id") or signal.get("source") or "").lower()
    source_type = str(signal.get("source_type") or "").lower()
    text = " ".join(str(signal.get(k) or "") for k in ("title", "summary", "body"))
    if source in DECISION_SOURCES or metadata.get("requires_decision") or _DECISION_RE.search(text):
        return "decision"
    if (source_type in CONVERSATION_TYPES or metadata.get("conv_id")
            or metadata.get("reply_to") or metadata.get("sender_id")):
        return "conversation"
    return "information"


def matter_id_from_context(context: Any = None, conv_key: str = "") -> str:
    data = context or {}
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except (json.JSONDecodeError, TypeError, ValueError):
            data = {}
    if isinstance(data, dict) and data.get("matter_id"):
        return str(data["matter_id"])
    conv_key = str(conv_key or (data.get("conv_key") if isinstance(data, dict) else "") or "")
    if conv_key:
        try:
            from core.matter_bridge import get_binding
            binding = get_binding(conv_key)
        except Exception:
            binding = None
        if binding:
            return str(binding["matter_id"])
    return ""


def _tokens(text: str) -> set[str]:
    return {token.lower() for token in _TOKEN_RE.findall(str(text or ""))
            if len(token) >= 2}


def resolve_signal_matter(signal: dict) -> str:
    metadata = _metadata(signal)
    explicit = matter_id_from_context(metadata, str(signal.get("conv_key") or ""))
    if explicit:
        return explicit
    source = str(signal.get("source_id") or signal.get("source") or "")
    event_id = str(signal.get("event_id") or signal.get("id") or "")
    if source and event_id:
        try:
            found = find_by_entity("artifact", event_id, provider=source)
        except Exception:
            found = None
        if found:
            return str(found["id"])
    signal_tokens = _tokens(" ".join(str(signal.get(k) or "")
                                     for k in ("title", "summary")))
    if not signal_tokens:
        return ""
    ranked = []
    for matter in list_matters(status="active,waiting,blocked", limit=100):
        matter_tokens = _tokens(" ".join(str(matter.get(k) or "")
                                         for k in ("title", "summary", "next_action")))
        overlap = len(signal_tokens & matter_tokens)
        score = overlap / max(1, min(len(signal_tokens), len(matter_tokens)))
        if overlap >= 2 and score >= 0.34:
            ranked.append((score, int(matter.get("priority", 5)), matter["id"]))
    ranked.sort(reverse=True)
    return ranked[0][2] if ranked else ""


def ingest_signal(signal: dict, *, create_decision_memorial: bool = False) -> dict:
    """Write one signal to a matching Matter, without creating noisy Matters."""
    route = classify_signal(signal)
    matter_id = resolve_signal_matter(signal)
    result = {"route": route, "matter_id": matter_id, "memorial_id": ""}
    if not matter_id:
        return result
    source = str(signal.get("source_id") or signal.get("source") or "signal")
    event_id = str(signal.get("event_id") or signal.get("id") or "")
    title = str(signal.get("title") or signal.get("summary") or source)[:300]
    payload = {"source": source, "source_type": signal.get("source_type", ""),
               "event_id": event_id, "route": route}
    add_event(matter_id, f"signal_{route}", title, actor=source, payload=payload)
    if event_id:
        try:
            link_entity(matter_id, "artifact", event_id, provider=source,
                        title=title, metadata=payload, actor="router")
        except ValueError:
            pass
    if route == "decision" and create_decision_memorial:
        from core.memorial import create
        mid, _ = create(
            source=source, title=title,
            body=str(signal.get("body") or signal.get("summary") or title),
            work_receipt="完成信号归档、事项绑定和决策必要性判断",
            owner_need="judgment",
            why_now="信号已完成归档和去重，只剩事项方向需要本人判断",
            owner_action="选择这个事项接下来是否推进",
            silence_cost="不提示会让已核验信号停在未决状态，无法继续或关闭",
            preset="decision", context=json.dumps(payload, ensure_ascii=False),
            matter_id=matter_id,
        )
        result["memorial_id"] = mid
    return result
