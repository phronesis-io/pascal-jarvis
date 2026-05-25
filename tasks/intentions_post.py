#!/usr/bin/env python3
"""intentions_post.py — Process Claude's intent responses and route actions.

Reads Claude's JSON response from stdin, marks intents as executed,
and emits a Lark card with the combined user-facing messages.

Expected envelope from Claude (multi-intent — the standard shape):
  {"intents": {"<id>": {"response": "...", "action": "notify|silent|chain|failed"}}}

Fallbacks:
  - {"response": "...", "action": "..."} (single intent without envelope) —
    we don't know the ID, so we resolve it from the most-recently triggered
    intent in the DB. Without this, the intent would stay stuck in 'triggered'.
  - Plain text — emit as-is, but log a warning since no intent is marked
    executed (the stale-triggered sweeper in intentions_pre.sh will recover).
"""

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from core.intentions import mark_executed, mark_failed, list_intents
from core.card import build_card


def _extract_json(raw: str) -> dict | None:
    """Try to extract a JSON object from raw text that may contain preamble or markdown."""
    # 1. Direct parse
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    # 2. Strip markdown code fences
    cleaned = re.sub(r'^```json?\s*', '', raw.strip())
    cleaned = re.sub(r'```\s*$', '', cleaned.strip())
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    # 3. Find JSON substring (handles preamble text from Claude)
    start = cleaned.find('{')
    end = cleaned.rfind('}')
    if start >= 0 and end > start:
        try:
            return json.loads(cleaned[start:end + 1])
        except json.JSONDecodeError:
            pass
    return None


def _resolve_single_triggered_id() -> str:
    """Find the only currently-triggered intent (single-intent fallback path).

    Returns "" if zero or more than one triggered intents exist — in those
    cases we can't safely guess which one Claude is replying about.
    """
    triggered = list_intents(status="triggered", limit=10)
    if len(triggered) == 1:
        return triggered[0]["id"]
    return ""


def _apply_action(intent_id: str, response: str, action: str,
                  user_messages: list) -> None:
    """Mark the intent and optionally surface a user message."""
    if action == "failed":
        mark_failed(intent_id, error=response)
        return
    # notify | silent | chain | (anything else) → executed
    mark_executed(intent_id, result=response)
    if action != "silent" and response:
        user_messages.append(response)


def main():
    raw = sys.stdin.read().strip()
    if not raw:
        return

    data = _extract_json(raw)
    if data is None:
        # Plain text with no extractable JSON — emit only if it looks human-readable.
        # Never emit raw JSON to the user.
        print("[intentions_post] Non-JSON response. "
              "Stuck intents will auto-reset.", file=sys.stderr)
        # Strip anything that looks like JSON from the output
        text = re.sub(r'\{[^{}]*\}', '', raw).strip()
        if text:
            print(build_card("🎯 Intent", text, source="intentions"))
        return

    user_messages: list = []

    intents_map = data.get("intents") if isinstance(data, dict) else None
    if isinstance(intents_map, dict) and intents_map:
        for intent_id, result in intents_map.items():
            if not isinstance(result, dict):
                result = {"response": str(result), "action": "notify"}
            try:
                _apply_action(
                    intent_id,
                    response=result.get("response", ""),
                    action=result.get("action", "notify"),
                    user_messages=user_messages,
                )
            except Exception as e:
                print(f"[intentions_post] Error processing {intent_id}: {e}",
                      file=sys.stderr)

    elif isinstance(data, dict) and "response" in data:
        # Single-intent shape (no envelope). Resolve which intent Claude meant.
        intent_id = _resolve_single_triggered_id()
        if intent_id:
            try:
                _apply_action(
                    intent_id,
                    response=data.get("response", ""),
                    action=data.get("action", "notify"),
                    user_messages=user_messages,
                )
            except Exception as e:
                print(f"[intentions_post] Error processing single intent: {e}",
                      file=sys.stderr)
        else:
            # Ambiguous — surface message but leave intents to the sweeper.
            print("[intentions_post] Ambiguous single-intent response — "
                  "cannot resolve target ID. Stale-triggered sweeper will recover.",
                  file=sys.stderr)
            resp = data.get("response", "")
            if resp and data.get("action") != "silent":
                user_messages.append(resp)

    if user_messages:
        combined = "\n\n".join(m for m in user_messages if m and m.strip())
        if combined:
            print(build_card("🎯 Intent", combined, source="intentions"))


if __name__ == "__main__":
    main()
