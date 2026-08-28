"""Small provider-response helpers shared by heartbeat execution paths."""

from __future__ import annotations

import json


_BENIGN_CLI_NOTICES = ("connectors are disabled",)


def isolated_failure_scope(is_heavy: bool, context_overflow: bool,
                           timed_out: bool, error: str) -> str:
    """Classify an isolated model failure without poisoning task circuits."""
    if is_heavy or context_overflow or not (timed_out or error):
        return "task"
    return "provider"


def record_isolated_failure(circuit, is_heavy: bool, context_overflow: bool,
                            timed_out: bool, error: str) -> tuple[bool, str]:
    """Charge only task-owned failures to a task's circuit breaker."""
    scope = isolated_failure_scope(
        is_heavy, context_overflow, timed_out, error,
    )
    return (circuit.record_failure() if scope == "task" else False), scope


def drop_benign_notices(text: str) -> str:
    """Remove CLI banners so the first retained line is the real failure."""
    if not text:
        return text
    kept = [
        line for line in text.splitlines()
        if not any(notice in line.lower() for notice in _BENIGN_CLI_NOTICES)
    ]
    cleaned = "\n".join(kept).strip()
    return cleaned or text


def error_summary(text: str, limit: int = 900) -> str:
    """The part of a failed CLI call that says WHY it failed.

    The Claude CLI answers `--output-format json` even on failure, and puts
    the human-readable cause in the trailing `result` field — after a ~500
    char preamble of usage counters. Head-truncating that envelope therefore
    logs exactly the half that carries no information: on 2026-08-27 six
    consecutive shared-call failures were logged as `is_error: true` plus
    zeroed token counts, and the cause was unrecoverable from the log.

    Falls back to head+tail for anything that is not that envelope, so a
    plain stderr traceback keeps both its first and last line.
    """
    text = (text or "").strip()
    if not text:
        return text
    start = text.find("{")
    if start != -1:
        try:
            payload = json.loads(text[start:])
        except (ValueError, TypeError):
            payload = None
        if isinstance(payload, dict):
            cause = payload.get("result") or payload.get("error")
            if cause:
                subtype = payload.get("subtype") or payload.get("stop_reason") or ""
                head = f"[{subtype}] " if subtype else ""
                return f"{head}{str(cause)[:limit]}"
    if len(text) <= limit:
        return text
    half = limit // 2
    return f"{text[:half]}\n…[{len(text) - limit} chars omitted]…\n{text[-half:]}"


def openai_usage_fields(response: object) -> dict[str, int]:
    """Normalize numeric Responses API usage for the scheduler ledger."""
    if not isinstance(response, dict) or not isinstance(response.get("usage"), dict):
        return {}
    usage = response["usage"]
    fields: dict[str, int] = {}
    for source in ("input_tokens", "output_tokens"):
        value = usage.get(source)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            fields[source] = int(value)
    details = usage.get("input_tokens_details")
    cached = details.get("cached_tokens") if isinstance(details, dict) else None
    if isinstance(cached, (int, float)) and not isinstance(cached, bool):
        fields["cache_read_input_tokens"] = int(cached)
    return fields
