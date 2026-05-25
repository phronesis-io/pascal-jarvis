"""EigenFlux stream helpers — parse NDJSON events from eigenflux stream.

Extracted from bot.sh inline Python blocks. Used by eigenflux_stream_loop.
"""

from __future__ import annotations

import json


def parse_cursor(ndjson_line: str) -> str:
    """Extract next_cursor from a stream event for reconnect resume."""
    try:
        event = json.loads(ndjson_line)
        return event.get("data", {}).get("next_cursor", "")
    except (json.JSONDecodeError, TypeError):
        return ""


def format_message(event_json: str) -> str:
    """Format an EigenFlux PM event for Lark delivery.

    Returns formatted markdown string, or empty if no messages.
    """
    try:
        event = json.loads(event_json) if isinstance(event_json, str) else event_json
        messages = event.get("data", {}).get("messages", [])
        if not messages:
            return ""
        parts = []
        for m in messages:
            sender = m.get("sender_name", "Unknown agent")
            content = m.get("content", "")
            if content:
                parts.append(f"💬 **{sender}**: {content}")
        if parts:
            return "\n".join(parts) + "\n\n📡 Powered by EigenFlux"
        return ""
    except (json.JSONDecodeError, TypeError):
        return ""


def extract_metadata(event_json: str) -> dict:
    """Extract conv_id, sender_id, sender_name from first message in event."""
    try:
        event = json.loads(event_json) if isinstance(event_json, str) else event_json
        msgs = event.get("data", {}).get("messages", [])
        if msgs:
            m = msgs[0]
            return {
                "conv_id": m.get("conv_id", ""),
                "sender_id": m.get("sender_id", ""),
                "sender_name": m.get("sender_name", ""),
            }
    except (json.JSONDecodeError, TypeError):
        pass
    return {}


def extract_detail(event_json: str) -> list[dict]:
    """Extract detailed per-message info for Claude analysis."""
    try:
        event = json.loads(event_json) if isinstance(event_json, str) else event_json
        messages = event.get("data", {}).get("messages", [])
        return [
            {
                "sender": m.get("sender_name", "Unknown"),
                "sender_id": m.get("sender_id", ""),
                "content": m.get("content", ""),
                "item_id": m.get("item_id", ""),
                "conv_id": m.get("conv_id", ""),
            }
            for m in messages
        ]
    except (json.JSONDecodeError, TypeError):
        return []
