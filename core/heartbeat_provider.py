"""Small provider-response helpers shared by heartbeat execution paths."""

from __future__ import annotations


_BENIGN_CLI_NOTICES = ("connectors are disabled",)


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
