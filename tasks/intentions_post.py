#!/usr/bin/env python3
"""intentions_post.py — Process Claude's intent responses and route actions.

Reads Claude's JSON response from stdin, marks intents as executed,
and emits a Lark card with the combined user-facing messages.

Expected envelope from Claude (multi-intent — the standard shape):
  {"intents": {"<id>": {"response": "...", "action": "notify|silent|chain|failed"}}}

v2 execution-ack (REQ-30): this script now runs on EVERY cycle outcome. The
heartbeat runner invokes it with stdin='__NO_ENVELOPE__' when Claude's reply
was HEARTBEAT_OK / empty / killed / unparseable, and the inflight manifest
(data/.intention_inflight.json, written by intentions_pre.sh after
mark_triggered) is reconciled deterministically: ids the envelope did not
cover get the bounded-retry policy applied immediately — absence of an
envelope is itself a deterministic signal, never silent intent death.

Closure buttons (REQ-34): when a closure follow-up cards its question, the
card carries ✅/❌/🚫 buttons whose value routes through the Lark event
sidecar straight into record_closure — one tap closes the loop, zero LLM.
"""

import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from core.intentions import (
    mark_executed, mark_failed, get_intent, record_closure,
    read_inflight, read_inflight_breaches, reconcile_inflight,
    mark_breaches_shown, validate_envelope,
)
from core.card import build_card
from core.safety import parse_json_response

CARD_LEDGER = ROOT / "data" / ".intent_card_ledger.jsonl"


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


def _root_id(iid: str) -> str:
    """The ROOT intent of a card row: a followup '<X>__fu' belongs to root X.
    Used for semantic dedup — three reworded cards for the same dinner all
    share root int_023339f780 even though their text differs (REQ-59)."""
    return (iid or "").split("__fu")[0]


# How long the same root intent's card is suppressed after one goes out.
CARD_DEDUP_MINUTES = 30


def _recent_card_roots(within_min: int = CARD_DEDUP_MINUTES) -> set[str]:
    """Root intent ids whose card was sent within the window (from the ledger).

    The triple-nag fix's REAL path (REQ-59): byte-level dedup fails because the
    LLM rewords each apology, but they all carry the same root intent. Keying
    on root + a time window kills the 3-cards-in-4-minutes class regardless of
    wording. Never raises.
    """
    if not CARD_LEDGER.exists():
        return set()
    import datetime as _dt
    cutoff = _dt.datetime.now() - _dt.timedelta(minutes=within_min)
    roots: set[str] = set()
    try:
        for line in CARD_LEDGER.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = row.get("ts", "")
            try:
                when = _dt.datetime.fromisoformat(ts)
            except (ValueError, TypeError):
                continue
            if when >= cutoff:
                for iid in (row.get("intent_ids") or []):
                    roots.add(_root_id(iid))
    except OSError:
        return set()
    return roots


def _ledger_append(intent_ids: list[str]) -> None:
    """Record that a card covering these intents went out (REQ-34B).

    heartbeat_loop back-fills the real Lark message_ids after the send, so a
    quote-reply to the card can be matched back to its intent deterministically.
    Never raises.
    """
    if not intent_ids:
        return
    try:
        CARD_LEDGER.parent.mkdir(parents=True, exist_ok=True)
        with open(CARD_LEDGER, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "intent_ids": intent_ids,
                "message_ids": [],
            }, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[intentions_post] ledger append failed: {e}", file=sys.stderr)


def _apply_action(intent_id: str, response: str, action: str,
                  user_messages: list, closure: dict | None = None,
                  button_specs: list | None = None) -> None:
    """Mark the intent and optionally surface a user message.

    If a `closure` sub-object is present, this row is a FOLLOW-UP recording a
    result onto its parent — record_closure does the write and the row NEVER
    cards (recording is internal; healing/autonomous follow-ups are silent by
    construction, and even an external follow-up that records must not also
    nag). A follow-up that is still *asking* (no answer yet) carries no closure
    field, so its response cards normally — and gets one-tap closure buttons
    (REQ-34) targeting its parent.
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
                           result=closure.get("result", ""),
                           via="followup")
        except Exception as e:
            print(f"[intentions_post] closure record failed: {e}", file=sys.stderr)
        return  # closure rows never card
    if action != "silent" and response and not _is_contentless(response):
        user_messages.append(response)
        # One-tap closure buttons for a follow-up that is ASKING (REQ-34).
        if button_specs is not None:
            try:
                row = get_intent(intent_id)
                parent_id = (row or {}).get("parent_intent_id")
                if parent_id:
                    button_specs.append(
                        {"parent": parent_id, "name": (row or {}).get("name", "")})
            except Exception as e:
                print(f"[intentions_post] button spec failed: {e}", file=sys.stderr)


def _closure_buttons(button_specs: list) -> list[dict]:
    """Build the ✅/❌/🚫 button row(s) for asking follow-ups."""
    buttons = []
    for spec in button_specs[:2]:  # at most 2 intents' rows — cards stay small
        pid = spec["parent"]
        prefix = "" if len(button_specs) == 1 else f"{spec['name'][:8]}·"
        buttons += [
            {"text": f"{prefix}✅ 做了",
             "value": {"action": "intent_close", "id": pid, "outcome": "done"}},
            {"text": f"{prefix}❌ 没做",
             "value": {"action": "intent_close", "id": pid, "outcome": "recorded",
                        "result": "没做（按钮记录）"}},
            {"text": f"{prefix}🚫 不用追了",
             "value": {"action": "intent_close", "id": pid, "outcome": "na"}},
        ]
    return buttons


def main():
    raw = sys.stdin.read().strip()
    inflight = read_inflight()
    if not raw and not inflight:
        return  # nothing to do and no manifest to reconcile

    if not raw or raw == "__NO_ENVELOPE__":
        # Deterministic no-envelope path (REQ-30b): the runner saw
        # HEARTBEAT_OK / empty / killed / parse-failure (or empty stdin —
        # red-team fix: empty stdin must reconcile, never strand the
        # manifest). Nothing was covered; apply the retry policy to
        # everything inflight. Breaches that rode this cycle are NOT marked
        # shown — no card rendered, so their notify budget is untouched.
        result = reconcile_inflight([])
        if result["retried"] or result["expired"]:
            print(f"[intentions_post] no-envelope reconcile: "
                  f"retried={result['retried']} expired={result['expired']}",
                  file=sys.stderr)
        return

    data = parse_json_response(raw)
    if data is None:
        # Plain text with no extractable JSON. Reconcile the manifest first —
        # deterministic recovery, not "hope the sweeper gets it".
        result = reconcile_inflight([])
        print(f"[intentions_post] Non-JSON response; reconciled manifest "
              f"(retried={len(result['retried'])}, expired={len(result['expired'])}).",
              file=sys.stderr)
        # Never emit raw JSON to the user.
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
    button_specs: list = []
    covered: list[str] = []

    intents_map = data.get("intents") if isinstance(data, dict) else None
    if isinstance(intents_map, dict) and intents_map:
        covered, _missing, errors = validate_envelope(data, inflight)
        for err in errors:
            print(f"[intentions_post] envelope: {err}", file=sys.stderr)
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
                    button_specs=button_specs,
                )
            except Exception as e:
                print(f"[intentions_post] Error processing {intent_id}: {e}",
                      file=sys.stderr)

    elif isinstance(data, dict) and "response" in data:
        # Single-intent shape (no envelope). The manifest replaces the old
        # guess-from-DB resolution: unambiguous only when exactly one id was
        # handed to this cycle.
        intent_id = inflight[0] if len(inflight) == 1 else ""
        if intent_id:
            covered = [intent_id]
            try:
                _apply_action(
                    intent_id,
                    response=data.get("response", ""),
                    action=data.get("action", "notify"),
                    user_messages=user_messages,
                    closure=data.get("closure"),
                    button_specs=button_specs,
                )
            except Exception as e:
                print(f"[intentions_post] Error processing single intent: {e}",
                      file=sys.stderr)
        else:
            print("[intentions_post] Ambiguous single-intent response — "
                  "manifest reconcile will retry the uncovered ids.",
                  file=sys.stderr)
            resp = data.get("response", "")
            if resp and data.get("action") != "silent" and not _is_contentless(resp):
                user_messages.append(resp)

    # Capture which breaches rode THIS cycle's PRE prompt BEFORE reconcile (it
    # may queue fresh breaches we must NOT mark shown — they never rode a card).
    rode_breach_ids = read_inflight_breaches()

    # Deterministic reconcile: whatever the envelope did not cover gets the
    # bounded-retry policy NOW (REQ-30c). Also clears the manifest.
    result = reconcile_inflight(covered)
    if result["retried"] or result["expired"]:
        print(f"[intentions_post] reconcile: retried={result['retried']} "
              f"expired={result['expired']}", file=sys.stderr)

    if user_messages:
        combined = "\n\n".join(m for m in user_messages if m and m.strip())
        if combined:
            # Outbox-layer semantic dedup (REQ-59): if a card for the SAME root
            # intent already went out within the window, suppress this one. The
            # 6/15 triple-nag (3 reworded dinner-closure cards in 4 min) all
            # shared root int_023339f780 — byte dedup missed them, root dedup
            # catches them. Roots = covered ids + the parents the buttons target.
            roots_now = {_root_id(c) for c in covered}
            roots_now |= {_root_id(s.get("parent", "")) for s in button_specs}
            roots_now.discard("")
            recent = _recent_card_roots()
            if roots_now and roots_now <= recent:
                print(f"[intentions_post] suppressed duplicate card for root(s) "
                      f"{sorted(roots_now)} — already sent within "
                      f"{CARD_DEDUP_MINUTES}min (REQ-59)", file=sys.stderr)
            else:
                buttons = _closure_buttons(button_specs) if button_specs else None
                _ledger_append(covered)
                print(build_card("🎯 Intent", combined, source="intentions",
                                 buttons=buttons))
                # A card actually rendered → the apology (if any breach rode
                # this cycle's prompt) was delivered. Bump notify_attempts for
                # exactly those breach ids — NOT reconcile's freshly-queued
                # ones. (Only when a card truly went out, i.e. not suppressed.)
                fresh = set(result.get("breached", []))
                to_mark = [b for b in rode_breach_ids if b not in fresh]
                if to_mark:
                    try:
                        mark_breaches_shown(to_mark)
                    except Exception as e:
                        print(f"[intentions_post] mark_breaches_shown failed: {e}",
                              file=sys.stderr)


if __name__ == "__main__":
    main()
