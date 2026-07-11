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
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from core.intentions import (
    mark_executed, mark_failed, get_intent, record_closure,
    read_inflight, read_inflight_breaches, reconcile_inflight,
    mark_breaches_shown, validate_envelope, note_closure_touch,
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
    # claim-of-write tokens (F2): a file-product intent saying "已写入" is a
    # claim, not a product — must never gate executed or reach a card.
    "已写入", "已记录", "written", "logged",
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


def _strip_fences(text: str) -> str:
    """Peel a leading/trailing ``` code fence (with optional language tag)."""
    s = (text or "").strip()
    s = re.sub(r"^```[\w-]*\s*", "", s)
    s = re.sub(r"\s*```\s*$", "", s)
    return s.strip()


def _is_empty_product(product: str) -> bool:
    """True when a fence-stripped response is a dead-call husk: empty,
    stray backticks, or the runner's HEARTBEAT_OK protocol token leaking into
    an intent slot. Deliberately narrower than _is_contentless — internal
    intents legitimately report bare status tokens ('sent', '已发送') and
    those must still count as executed; a husk means the model produced
    NOTHING for this occurrence (the 7/7-7/8 fallback-envelope shape that
    fake-executed the 小时报/日报 with zero delivered content)."""
    s = product.strip("`").strip()
    return not s or s.upper() == "HEARTBEAT_OK"


def _is_quiet_sentinel(product: str) -> bool:
    """True for a bare/fenced HEARTBEAT_OK in an intent slot.

    For the file-product intent (小时报) this token is DOCUMENTED protocol:
    its own prompt says 「仅当本条含可决策/有冲突/出成果的料时才推飞书，否则
    静默累积、HEARTBEAT_OK」 — a model alive enough to build a valid envelope
    and put this token in the slot is reporting a quiet hour, not a dead
    call (a dead call's whole reply is HEARTBEAT_OK/empty and takes the
    __NO_ENVELOPE__ path, never reaching a slot). The shape is admittedly
    indistinguishable from a degraded fallback that happens to emit the same
    token in-envelope; treating it as done-with-no-product loses at most one
    hour's report in that rare case, while treating it as a husk would
    re-fire every genuine quiet hour for 6h at intention-check's 1-minute
    cadence (hundreds of paid calls per occurrence)."""
    return product.strip("`").strip().upper() == "HEARTBEAT_OK"


# Heading-shape parsers for the product-log hour dedup (F-16): date and
# first HH:MM wherever they sit in a '### ' heading line.
_HEAD_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_HEAD_TIME = re.compile(r"(\d{1,2}):\d{2}")


def _heading_matches_hour(line: str, day: str, hour: int) -> bool:
    """True when a '### ' heading line carries this execution date-hour —
    wherever the date/time sit in the line. Observed real in-call shapes:
    '### 2026-07-07 22:08' (bare), '### 小时报 21:09 (2026-07-02)'
    (name-first), '### 2026-06-22 (小时报 12:29)', '### 2026-07-04 23:00
    小时报'. A heading with no time (or another date) never matches."""
    m_d = _HEAD_DATE.search(line)
    if not m_d or m_d.group(0) != day:
        return False
    m_t = _HEAD_TIME.search(line)
    return bool(m_t) and int(m_t.group(1)) == hour


# Canonical memory root for deterministic product writes. bot.sh exports
# MEMORY_DIR (the heartbeat memory tier) to every heartbeat child — the ONE
# root the 小时报 timeline actually lives in; when the model chose where to
# write in-call, entries landed scattered across all three memory roots.
_MEMORY_DIR = Path(os.environ.get(
    "MEMORY_DIR", str(Path.home() / ".jarvis" / "memory")))

# Deterministic product write (F2 audit 2026-07-08): intents listed here
# declare a target log — the post script itself appends the delivered content,
# so "did it happen" is verifiable on disk instead of trusting the model's
# in-call file write (which died with the 7/7 provider failover while every
# occurrence was still marked executed; 小时报 fake-fresh ~23h). Per-intent
# opt-in ONLY: 27+ unrelated intents share _apply_action and must never have
# their responses blanket-appended anywhere. Target is hourly_log.md, the
# fresh-write buffer (memory_daily_post rotates it into hourly_archive.md
# nightly) and the same file a behaving in-call run writes — the hour-header
# dedup in _append_product_log only works against the file the model uses.
PRODUCT_LOGS = {
    "int_6362ae1606": _MEMORY_DIR / "timeline" / "hourly_log.md",  # 小时报
}


def _append_product_log(intent_id: str, content: str) -> bool:
    """Append the intent's delivered content under an hour header.

    Returns True when this hour's entry is on disk — written now, or already
    written in-call by the model. False on write failure: executed is gated
    on the file write for these intents.

    Dedup (F-16): the old rule required the intent name in a heading line
    starting with the exact '### YYYY-MM-DD HH' prefix, but the model's real
    in-call headings are usually name-first ('### 小时报 21:09 (2026-07-02)')
    or carry the date elsewhere — none matched, so healthy hours got double
    entries in the file loaded into every heartbeat prompt. An hour counts
    as already-written when:
      a) a heading whose parsed date-hour matches the execution hour carries
         the intent name anywhere in the line, or
      b) such a heading's FIRST body line leads with the name ('[小时报] …'
         — the label-on-next-line shape), or
      c) the content's own first 40 chars are already on disk (the
         near-identical double-entry, regardless of heading shape).
    The hour alone is deliberately NOT enough: memory_hourly_post.py appends
    its own bare '### YYYY-MM-DD HH:MM' HOURLY INDEX entries to the SAME
    file every hour (and those bodies may merely MENTION 小时报) — keying on
    the hour alone would swallow the real report behind the index entry.
    """
    target = PRODUCT_LOGS[intent_id]
    try:
        name = (get_intent(intent_id) or {}).get("name") or intent_id
    except Exception:
        name = intent_id
    stamp = time.strftime("%Y-%m-%d %H:%M")
    day, hour = stamp[:10], int(stamp[11:13])
    try:
        existing = target.read_text(encoding="utf-8") if target.exists() else ""
        prefix = content.strip()[:40]
        if len(prefix) >= 20 and prefix in existing:
            return True  # (c) the report text itself already landed
        lines = existing.splitlines()
        for i, line in enumerate(lines):
            if not line.startswith("### ") or not _heading_matches_hour(
                    line, day, hour):
                continue
            if name in line:
                return True  # (a) hour already logged in-call
            for nxt in lines[i + 1:]:
                if nxt.strip():
                    if name in nxt.strip()[:20]:
                        return True  # (b) label on the body's first line
                    break
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "a", encoding="utf-8") as f:
            f.write(f"\n### {stamp} {name}\n{content.strip()}\n")
        return True
    except OSError as e:
        print(f"[intentions_post] product log append failed for {intent_id} "
              f"({target}): {e}", file=sys.stderr)
        return False


def _root_id(iid: str) -> str:
    """The ROOT intent of a card row: a followup '<X>__fu' belongs to root X.
    Used for semantic dedup — three reworded cards for the same dinner all
    share root int_023339f780 even though their text differs (REQ-59)."""
    return (iid or "").split("__fu")[0]


# How long the same root intent's card is suppressed after one goes out.
CARD_DEDUP_MINUTES = 30


def _recent_card_roots(within_min: int = CARD_DEDUP_MINUTES) -> set[str]:
    """NAG-class root intents whose card went out within the window (REQ-59).

    Reads the ledger's `card_roots` field — the closure-ask roots a card
    actually surfaced — NOT `intent_ids` (which also lists silent/prompt
    slots and reply-matching ids). Red-team fix: keying dedup on all covered
    ids made a silent slot (or a sub-30-min recurring occurrence) look
    'carded', then suppressed that root's NEXT genuine notify. Dedup must see
    only the reworded-nag class (closure asks), keyed on root + time window.
    Never raises.
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
                for r in (row.get("card_roots") or []):
                    if r:
                        roots.add(r)
    except OSError:
        return set()
    return roots


def _ledger_append(intent_ids: list[str], card_roots: list[str] | None = None) -> None:
    """Record that a card covering these intents went out.

    intent_ids = all ids the card covers (REQ-34B reply matching — heartbeat_loop
    back-fills message_ids so a quote-reply maps back to its intent).
    card_roots = the NAG-class roots this card actually surfaced (closure asks),
    the ONLY thing REQ-59 dedup keys on. Never raises.
    """
    if not intent_ids:
        return
    try:
        CARD_LEDGER.parent.mkdir(parents=True, exist_ok=True)
        with open(CARD_LEDGER, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "intent_ids": intent_ids,
                "card_roots": list(card_roots or []),
                "message_ids": [],
            }, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[intentions_post] ledger append failed: {e}", file=sys.stderr)


def _apply_action(intent_id: str, response: str, action: str,
                  user_messages: list, closure: dict | None = None,
                  button_specs: list | None = None) -> bool:
    """Mark the intent and optionally surface a user message.

    If a `closure` sub-object is present, this row is a FOLLOW-UP recording a
    result onto its parent — record_closure does the write and the row NEVER
    cards (recording is internal; healing/autonomous follow-ups are silent by
    construction, and even an external follow-up that records must not also
    nag). A follow-up that is still *asking* (no answer yet) carries no closure
    field, so its response cards normally — and gets one-tap closure buttons
    (REQ-34) targeting its parent.

    Returns False when the occurrence was NOT resolved: a contentless envelope
    slot (F2 guard) must leave the row 'triggered' and drop out of `covered`,
    so reconcile_inflight applies the bounded-retry policy and the occurrence
    re-fires — instead of a dead Claude call / fallback husk counting as
    executed (7/7-7/8: 小时报 fake-executed ~23h straight, 日报 skipped, zero
    product, health all green).
    """
    if action == "failed":
        mark_failed(intent_id, error=response)
        return True
    is_closure = bool(closure and isinstance(closure, dict) and closure.get("parent"))
    product = _strip_fences(response)
    # Executed requires real product. Closure rows are exempt — the
    # record_closure write below IS the product (response may be empty).
    quiet_hour = False
    if not is_closure:
        if intent_id in PRODUCT_LOGS:
            if _is_quiet_sentinel(product):
                # Deliberate quiet-hour sentinel (documented in the 小时报's
                # own prompt): nothing to report this hour. Executed with NO
                # product — no junk log entry (the 7/7-7/8 leak wrote a bare
                # 'HEARTBEAT_OK' line into the timeline AND dedup-blocked the
                # hour's real report), no card, and no endless re-fire of a
                # genuinely quiet occurrence.
                quiet_hour = True
            # File-product intent: the log entry is the product; a bare
            # status token ('已写入') or a husk can't be one — gate executed
            # on the deterministic write, never on the model's claim.
            elif (_is_contentless(product) or _is_empty_product(product)
                    or not _append_product_log(intent_id, product)):
                print(f"[intentions_post] {intent_id}: no appendable product "
                      f"(action={action}) — left for reconcile retry",
                      file=sys.stderr)
                return False
        elif _is_empty_product(product):
            print(f"[intentions_post] {intent_id}: contentless response "
                  f"(action={action}) — not executed, left for reconcile retry",
                  file=sys.stderr)
            return False
    # notify | silent | chain | (anything else) → executed
    mark_executed(intent_id, result=response)
    if is_closure:
        try:
            record_closure(str(closure["parent"]).strip(),
                           outcome=closure.get("outcome", "done"),
                           result=closure.get("result", ""),
                           via="followup")
        except Exception as e:
            print(f"[intentions_post] closure record failed: {e}", file=sys.stderr)
        return True  # closure rows never card
    # quiet_hour: HEARTBEAT_OK is protocol, never user-facing content.
    if action != "silent" and response and not _is_contentless(response) \
            and not quiet_hour:
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
    return True


def _closure_buttons(button_specs: list) -> list[dict]:
    """Build the ✅/❌/🚫 button row(s) for asking follow-ups.

    Legacy path — only used for the rare 2-intent combined card (see
    _emit_closure_card): memorial decide() locks the whole card on first
    tap, so two intents' buttons on one memorial would deadlock each other."""
    buttons = []
    for spec in button_specs[:2]:  # at most 2 intents' rows — cards stay small
        pid = spec["parent"]
        prefix = "" if len(button_specs) == 1 else f"{spec['name'][:8]}·"
        buttons += [
            {"text": f"{prefix}✅ 做了",
             # result must be non-empty: REQ-90③ coerces done+empty-result to
             # 'na', and a ✅ tap IS evidence — mirror the ❌ button's pattern.
             "value": {"action": "intent_close", "id": pid, "outcome": "done",
                        "result": "做了（按钮记录）"}},
            {"text": f"{prefix}❌ 没做",
             "value": {"action": "intent_close", "id": pid, "outcome": "recorded",
                        "result": "没做（按钮记录）"}},
            {"text": f"{prefix}🚫 不用追了",
             "value": {"action": "intent_close", "id": pid, "outcome": "na"}},
        ]
    return buttons


def _memorial_closure_options(pid: str) -> list[dict]:
    """单意图闭环问句的三枚批红（对应旧 ✅/❌/🚫 按钮）。

    动作经 memorial.decide → ActionProcessor._do_intent_close 执行；via 必须
    排在 result 之前（result= 之后的参数会被并进 result 文本），via=button
    保持闭环遥测能区分一键批红和 CLI/marker。"""
    return [
        {"key": "done", "label": "✅ 做了",
         "action": {"type": "intent_close",
                    "params": {"id": pid, "outcome": "done", "via": "button",
                               "result": "做了（按钮记录）"}}},
        {"key": "recorded", "label": "❌ 没做",
         "action": {"type": "intent_close",
                    "params": {"id": pid, "outcome": "recorded", "via": "button",
                               "result": "没做（按钮记录）"}}},
        {"key": "na", "label": "🚫 不用追了",
         "action": {"type": "intent_close",
                    "params": {"id": pid, "outcome": "na", "via": "button"}}},
    ]


def _emit_closure_card(combined: str, button_specs: list) -> None:
    """Print the closure-ask card. Single intent (the common case) goes out
    as a native memorial — ledgered, idempotent 批红, auto「聊聊这个」. Two
    intents keep the legacy combined card (memorial locks a card on first
    decide, which would deadlock the second intent's buttons); the delivery
    layer still adopts it with its native buttons preserved."""
    if len(button_specs) == 1:
        try:
            from core import memorial
            spec = button_specs[0]
            # Title = the intent's name (one card says one thing); memorial's
            # header already prefixes 📜 + the source emoji, no 🎯 here.
            title = (str(spec.get("name", "")).strip() or "跟进")[:24]
            mid, _ = memorial.create(
                source="intentions", title=title, body=combined,
                options=_memorial_closure_options(spec["parent"]),
                send=False)
            print(memorial.card_json(mid))
            return
        except Exception as e:
            print(f"[intentions_post] memorial failed, using plain card: {e}",
                  file=sys.stderr)
    buttons = _closure_buttons(button_specs) if button_specs else None
    print(build_card("🎯 Intent", combined, source="intentions",
                     buttons=buttons))


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
        if result["retried"] or result["expired"] or result.get("skipped"):
            print(f"[intentions_post] no-envelope reconcile: "
                  f"retried={result['retried']} expired={result['expired']} "
                  f"skipped={result.get('skipped', [])}",
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
                resolved = _apply_action(
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
            else:
                # F2 guard: a contentless slot is NOT coverage — drop it so
                # reconcile_inflight re-fires the still-'triggered' row.
                if not resolved and intent_id in covered:
                    covered.remove(intent_id)

    elif isinstance(data, dict) and "response" in data:
        # Single-intent shape (no envelope). The manifest replaces the old
        # guess-from-DB resolution: unambiguous only when exactly one id was
        # handed to this cycle.
        intent_id = inflight[0] if len(inflight) == 1 else ""
        if intent_id:
            covered = [intent_id]
            try:
                resolved = _apply_action(
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
                if not resolved:
                    covered = []
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
    if result["retried"] or result["expired"] or result.get("skipped"):
        print(f"[intentions_post] reconcile: retried={result['retried']} "
              f"expired={result['expired']} skipped={result.get('skipped', [])}",
              file=sys.stderr)

    if user_messages:
        combined = "\n\n".join(m for m in user_messages if m and m.strip())
        if combined:
            # Outbox-layer semantic dedup (REQ-59), scoped to the NAG class:
            # closure-ASK cards (those with ✅/❌/🚫 buttons targeting a parent).
            # The 6/15 triple-nag (3 reworded dinner-closure cards in 4 min) all
            # targeted root int_023339f780 — byte dedup missed them, root dedup
            # catches them. Red-team fix: key ONLY on button (closure-ask) roots,
            # NOT all `covered` — else a silent slot or a sub-30-min recurring
            # occurrence riding the cycle would falsely suppress its own next
            # genuine notify. Plain notify / recurring intents (no buttons) are
            # never deduped here; they keep their own cadence.
            nag_roots = {_root_id(s.get("parent", "")) for s in button_specs}
            nag_roots.discard("")
            recent = _recent_card_roots()
            if nag_roots and nag_roots <= recent:
                print(f"[intentions_post] suppressed duplicate closure card for "
                      f"root(s) {sorted(nag_roots)} — already sent within "
                      f"{CARD_DEDUP_MINUTES}min (REQ-59)", file=sys.stderr)
            else:
                _ledger_append(covered, card_roots=sorted(nag_roots))
                _emit_closure_card(combined, button_specs)
                # Proactive closure budget counts Pascal-visible asks, not row
                # creation. Duplicate-suppressed cards do not call this path.
                for spec in button_specs:
                    try:
                        note_closure_touch(spec.get("parent", ""), via="card")
                    except Exception as e:
                        print(f"[intentions_post] note_closure_touch failed: {e}",
                              file=sys.stderr)
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
