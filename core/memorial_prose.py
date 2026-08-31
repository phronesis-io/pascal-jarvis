"""Text side of ``core.memorial.memorialize_output`` (split out 2026-08-31).

``core/memorial.py`` sits at its maintainability budget, so the prose→card
path lives here: turning a flushed prose block into ledgered cards, rescuing
provenance-verified ledger cards that were demoted to prose, and describing
dropped text by shape only.

Why rescue exists (T26): every multi-card mail-triage run between 8/25 and
8/28 (6+3+5+2 cards) vanished with exactly one ``work_receipt_missing`` and
zero delivery envelopes, while single-card runs lived. A card that
byte-matches its own ledger render carries nothing but its own callbacks and
can never be a Markdown example — whatever put it among prose, it must be
delivered, not dropped by the work-receipt gate.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Callable, Mapping

TrustedIdOf = Callable[[dict], str]


@dataclass(frozen=True)
class ProseRuntime:
    """Explicit capabilities needed to turn prose into memorial cards.

    ``memorial`` owns ledger and delivery policy. Keeping those callbacks on
    this one-way boundary lets the text parser stay in a small module without
    creating a deferred ``memorial <-> memorial_prose`` import cycle.
    """

    split_authored_card_blocks: Callable[[str], list[str]]
    ops_log: Callable[..., None]
    extract_title_line: Callable[[str], tuple[str, str]]
    extract_work_receipt: Callable[[str], tuple[str, str]]
    extract_recommendation: Callable[[str], tuple[str, str]]
    extract_inline_options: Callable[[str], tuple[str, list[dict]]]
    scrub_embedded_authoring_directives: Callable[[str], str]
    source_default_preset: Mapping[str, str]
    split_matters: Callable[[str], list[str]]
    title_for_chunk: Callable[[str, str], tuple[str, str]]
    create: Callable[..., tuple[str, bool]]
    get_memorial: Callable[[str], dict | None]
    should_push_to_lark: Callable[[dict], bool]
    delivery_accepted: Callable[[dict], bool]
    card_json: Callable[[str], str]
    attention_alert: str
    can_infer_alert_from_prose: Callable[[str], bool]
    looks_like_alert: Callable[[str], bool]


def dropped_text_shape(text: str) -> dict:
    """Describe dropped output by line shape only (never card/prose content).

    Enough to tell "the model forgot its receipt" from "cards were demoted to
    prose" after the fact.
    """
    lines = [ln.strip() for ln in str(text or "").splitlines() if ln.strip()]
    first = lines[0] if lines else ""
    if first.startswith("CARD:"):
        first_kind = "envelope"
    elif first.startswith("{"):
        first_kind = "json"
    elif first.startswith(("```", "~~~")):
        first_kind = "fence"
    elif first.startswith(">"):
        first_kind = "quote"
    else:
        first_kind = "prose" if first else "empty"
    return {
        "line_count": len(lines),
        "json_lines": sum(1 for ln in lines if ln.startswith("{")),
        "envelope_lines": sum(1 for ln in lines if ln.startswith("CARD:")),
        "first_line_kind": first_kind,
    }


def parse_ledger_card(line: str,
                      trusted_id_of: TrustedIdOf) -> tuple[str, dict | None]:
    """Return ``(memorial_id, card)`` when ``line`` is a provenance-verified
    ledger card — bare, indented, or ``CARD:``-enveloped — else ``("", None)``.
    """
    from core.card_envelope import is_card_payload
    stripped = str(line or "").strip()
    payload = stripped[5:] if stripped.startswith("CARD:") else stripped
    if not payload.startswith("{"):
        return "", None
    try:
        card = json.loads(payload)
    except (json.JSONDecodeError, TypeError, ValueError):
        return "", None
    if not is_card_payload(card):
        return "", None
    memorial_id = trusted_id_of(card)
    return (memorial_id, card) if memorial_id else ("", None)


def demotion_reason(*, prose_ahead: bool, markdown_protected: bool,
                    bad_envelope_ahead: bool) -> str:
    """Name why the content-not-callback rule would have demoted a card."""
    if bad_envelope_ahead:
        return "bad_envelope_ahead"
    if markdown_protected:
        return "markdown_protected"
    if prose_ahead:
        return "prose_ahead"
    return ""


def memorialize_prose(text: str, *, source: str, require_work_receipt: bool,
                      rendered: list[str], runtime: ProseRuntime) -> None:
    """Turn one flushed prose block into memorial cards (appends to
    ``rendered`` the card JSON of every card that should reach Lark)."""
    from core.safety import strip_task_framing
    text = str(text or "").strip()
    if not text:
        return
    try:
        json.loads(text)
        return  # raw internal JSON is never a user-visible card
    except (json.JSONDecodeError, TypeError, ValueError):
        pass
    # Echoed prompt framing ("=== TASK: x ===", "[CHECKIN]", "[ts] task")
    # is never card content — same class fix as checkin_post (REQ-104).
    text = strip_task_framing(text)
    if not text:
        return
    authored_blocks = runtime.split_authored_card_blocks(text)
    if len(authored_blocks) > 1:
        runtime.ops_log(
            "card_split", source=source,
            split_kind="concatenated_directives",
            card_count=len(authored_blocks),
        )
        for authored_block in authored_blocks:
            memorialize_prose(
                authored_block, source=source,
                require_work_receipt=require_work_receipt, rendered=rendered,
                runtime=runtime)
        return
    explicit_title, text = runtime.extract_title_line(text)
    text, work_receipt = runtime.extract_work_receipt(text)
    if require_work_receipt and not work_receipt:
        runtime.ops_log(
            "work_receipt_missing", level="warn", source=source,
            **dropped_text_shape(text),
        )
        return
    if not text and explicit_title:
        text = explicit_title
    if not text:
        return
    # Buttons follow the card: an OPTIONS line authored by the task wins;
    # otherwise fall back to what this source is usually asking for, and
    # only then to「已阅」.
    # RECOMMEND may legally follow OPTIONS. Remove it first so the
    # trailing-line OPTIONS parser still sees the authored buttons, then
    # carry the recommendation explicitly into create().
    text, authored_recommend = runtime.extract_recommendation(text)
    body, inline_options = runtime.extract_inline_options(text)
    body = runtime.scrub_embedded_authoring_directives(body)
    preset = (None if inline_options
              else runtime.source_default_preset.get(source, "fyi"))
    # 一张卡一件事 (REQ-117): the prompt contract is the first line of
    # defense; this is the mechanical backstop for bodies that merged
    # several matters anyway. A card whose author wrote its own OPTIONS
    # line designed ONE interactive ask — never split that.
    chunks = [body] if inline_options else runtime.split_matters(body)
    if len(chunks) > 1:
        runtime.ops_log(
            "card_split", source=source,
            split_kind="prose_body", card_count=len(chunks),
        )
    for chunk in chunks:
        if explicit_title and len(chunks) == 1:
            chunk_title, chunk_body = explicit_title, chunk
        else:
            chunk_title, chunk_body = runtime.title_for_chunk(chunk, source)
        mid, _ = runtime.create(
            source, chunk_title, chunk_body,
            options=inline_options, preset=preset,
            recommend=authored_recommend,
            work_receipt=work_receipt,
            require_work_receipt=require_work_receipt,
            authoring_protocol=True, send=False,
            attention=(runtime.attention_alert
                       if runtime.can_infer_alert_from_prose(source)
                       and runtime.looks_like_alert(chunk_body)
                       and not inline_options and preset == "fyi"
                       else ""),
        )
        state = runtime.get_memorial(mid) or {}
        if not runtime.should_push_to_lark(state):
            continue  # ledger-only (REQ-119)
        if runtime.delivery_accepted(state):
            continue
        rendered.append(runtime.card_json(mid))
