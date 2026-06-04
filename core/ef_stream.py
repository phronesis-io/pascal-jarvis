"""EigenFlux stream helpers — parse NDJSON events from eigenflux stream.

Extracted from bot.sh inline Python blocks. Used by eigenflux_stream_loop.
"""

from __future__ import annotations

import json
from pathlib import Path


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


# ── PM dedup ─────────────────────────────────────────────────────────
# A reconnect between delivery and the cursor write can re-deliver the same
# event, which would fire a duplicate Lark message AND a duplicate (costly)
# background Claude analysis. We suppress that with a bounded, persisted
# seen-set keyed on item_id.

SEEN_CAP = 200


def extract_item_ids(event_json) -> list[str]:
    """Non-empty item_ids of all messages in an event, in order."""
    try:
        event = json.loads(event_json) if isinstance(event_json, str) else event_json
        msgs = event.get("data", {}).get("messages", [])
        ids = []
        for m in msgs:
            iid = str(m.get("item_id", "") or "")
            if iid:
                ids.append(iid)
        return ids
    except (json.JSONDecodeError, TypeError, AttributeError):
        return []


def is_duplicate_event(item_ids, seen) -> bool:
    """True only if there is ≥1 item_id and ALL are already in `seen`.

    Messages without an item_id are never deduped (returns False), so a missing
    id can't silently suppress a real delivery.
    """
    ids = [str(i) for i in (item_ids or []) if i]
    return bool(ids) and all(i in seen for i in ids)


def load_seen(path) -> list[str]:
    """Load the persisted seen-item_id list (oldest first); [] on any error."""
    try:
        data = json.loads(Path(path).read_text())
        if isinstance(data, list):
            return [str(x) for x in data]
    except Exception:
        pass
    return []


def remember_seen(seen_list, new_ids, cap: int = SEEN_CAP) -> list[str]:
    """Append new_ids (skipping dups), preserve order, trim to the last `cap`."""
    result = list(seen_list)
    have = set(result)
    for i in new_ids or []:
        i = str(i)
        if i and i not in have:
            result.append(i)
            have.add(i)
    if len(result) > cap:
        result = result[-cap:]
    return result


def save_seen(path, seen_list) -> None:
    """Persist the seen-item_id list (best-effort)."""
    try:
        Path(path).write_text(json.dumps(list(seen_list)))
    except Exception:
        pass


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


# ── Friend-request / relation events ─────────────────────────────────
# These ride the stream differently from PMs. A NEW friend request arrives
# inside a `pm_push` packet whose `messages` array is EMPTY and whose
# `friend_requests` array is populated; an ACCEPTANCE arrives as a separate
# `{"type":"friend_accepted","data":{"friend_uid":...}}` envelope. The PM
# formatter only reads `data.messages`, so without the helpers below these
# events are silently dropped and never reach the user.


def format_relation_event(event_json) -> str:
    """Format a friend-request / friend-accepted event for Lark delivery.

    Returns a markdown string, or "" if this is not a relation event.
    """
    try:
        event = json.loads(event_json) if isinstance(event_json, str) else event_json
    except (json.JSONDecodeError, TypeError):
        return ""
    if not isinstance(event, dict):
        return ""
    data = event.get("data") or {}
    if not isinstance(data, dict):
        return ""

    if event.get("type") == "friend_accepted" or data.get("friend_uid"):
        return "✅ EigenFlux 好友申请已通过，你们现在是好友了。\n\n📡 Powered by EigenFlux"

    reqs = data.get("friend_requests") or []
    lines = []
    for r in reqs:
        if not isinstance(r, dict):
            continue
        name = r.get("from_name") or "某个 agent"
        greeting = (r.get("greeting") or "").strip()
        line = f"👋 **{name}** 想加你为 EigenFlux 好友"
        if greeting:
            line += f"\n> {greeting}"
        lines.append(line)
    if not lines:
        return ""
    body = "\n\n".join(lines)
    return f"📡 EigenFlux · 好友申请\n\n{body}\n\n跟我说「通过」就帮你接受。\n\n📡 Powered by EigenFlux"


def extract_relation_ids(event_json) -> list[str]:
    """Dedup keys for relation events.

    request_id for each new request, plus a synthetic `accepted:<uid>` key for
    an acceptance (which carries no request_id). [] if not a relation event.
    Snowflake ids don't collide with PM item_ids, so the same seen-set is safe.
    """
    try:
        event = json.loads(event_json) if isinstance(event_json, str) else event_json
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(event, dict):
        return []
    data = event.get("data") or {}
    if not isinstance(data, dict):
        return []
    ids = []
    for r in (data.get("friend_requests") or []):
        if isinstance(r, dict):
            rid = str(r.get("request_id", "") or "")
            if rid:
                ids.append(rid)
    if event.get("type") == "friend_accepted" or data.get("friend_uid"):
        uid = str(data.get("friend_uid", "") or "")
        if uid:
            ids.append(f"accepted:{uid}")
    return ids
