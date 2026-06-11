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

import re
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from core.intentions import mark_executed, mark_failed, list_intents, record_closure
from core.card import build_card
from core.safety import parse_json_response


# Bare status / ack tokens an internal "prompt"-type intent may report as its
# result (e.g. "sent"). These are for the log, never a user-facing card.
_STATUS_TOKENS = {
    "sent", "done", "ok", "okay", "noted", "hello", "hi", "hey",
    "success", "succeeded", "completed", "complete", "executed", "executing",
    "notified", "silent", "notify", "chain", "failed", "none", "null", "n/a",
    "已发送", "已发", "发送成功", "完成", "已完成", "好的", "收到", "无", "成功",
}


def _is_contentless(response: str) -> bool:
    """True if response is a bare status/ack token with no real content.

    Internal "prompt"-type intents sometimes report a status word like "sent";
    those must never be joined into a user-facing card. Real notifications and
    calendar preps are always full sentences, so this only catches degenerate
    one-token outputs — it won't suppress legitimate short messages with
    actual substance.
    """
    s = response.strip().strip(".。!！?？ \t\n").lower()
    if not s:
        return True
    return s in _STATUS_TOKENS


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
                  user_messages: list, closure: dict | None = None) -> None:
    """Mark the intent and optionally surface a user message.

    If a `closure` sub-object is present, this row is a FOLLOW-UP recording a
    result onto its parent — record_closure does the write and the row NEVER
    cards (recording is internal; healing/autonomous follow-ups are silent by
    construction, and even an external follow-up that records must not also
    nag). A follow-up that is still *asking* (no answer yet) carries no closure
    field, so its response cards normally.
    """
    if action == "failed":
        mark_failed(intent_id, error=response)
        return
    # notify | silent | chain | (anything else) → executed
    mark_executed(intent_id, result=response)
    if closure and isinstance(closure, dict) and closure.get("parent"):
        try:
            record_closure(str(closure["parent"]).strip(),
                           outcome=closure.get("outcome", "done"),
                           result=closure.get("result", ""))
        except Exception as e:
            print(f"[intentions_post] closure record failed: {e}", file=sys.stderr)
        return  # closure rows never card
    if action != "silent" and response and not _is_contentless(response):
        user_messages.append(response)


def main():
    raw = sys.stdin.read().strip()
    if not raw:
        return

    data = parse_json_response(raw)
    if data is None:
        # Plain text with no extractable JSON — emit only if it looks human-readable.
        # Never emit raw JSON to the user.
        print("[intentions_post] Non-JSON response. "
              "Stuck intents will auto-reset.", file=sys.stderr)
        # If it's a malformed intents envelope (e.g. {"intents": {"id": , ...}}),
        # bail entirely — stripping braces would still leak fragments. The
        # stale-triggered sweeper recovers the intents.
        if '"intents"' in raw or '"response"' in raw or raw.lstrip().startswith('{'):
            print("[intentions_post] Looks like malformed JSON envelope — "
                  "suppressing to avoid leaking raw JSON to the user.",
                  file=sys.stderr)
            return
        # Strip simple {...} blobs (incl. nested) from otherwise-prose output.
        text = re.sub(r'\{.*\}', '', raw, flags=re.DOTALL).strip()
        if text and not _is_contentless(text):
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
                    closure=result.get("closure"),
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
                    closure=data.get("closure"),
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
            if resp and data.get("action") != "silent" and not _is_contentless(resp):
                user_messages.append(resp)

    if user_messages:
        combined = "\n\n".join(m for m in user_messages if m and m.strip())
        if combined:
            print(build_card("🎯 Intent", combined, source="intentions"))


if __name__ == "__main__":
    main()
