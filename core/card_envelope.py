"""Bare JSON cards printed by task post-hooks → executable ``CARD:`` envelopes.

A post-hook that owns its transport (mail-triage, intentions, exercise-week,
eigenflux-publish) creates the memorial with ``send=False`` and prints
``card_json(mid)`` — one bare JSON object per line. ``memorialize_output``
only ever recognised the case where the WHOLE output was one such object;
with two or more, every line was prose, and under ``require_work_receipt``
the prose block had no receipt and was dropped. 2026-08-25 18:24: six mail
alerts (four CI failures, two Google security notices) were ledgered as
``not_sent`` and never reached Lark; since 8/20 only single-card runs
survived (11 mail memorials, 1 delivered).

A bare line is promoted only when it is a top-level JSON card whose complete
payload is verified by the caller against the current Memorial ledger. A
callback-shaped id alone is not provenance. Anything else keeps the explicit
``CARD:`` requirement.
"""
from __future__ import annotations

import json
from typing import Callable


def is_card_payload(card: object) -> bool:
    """Whether a decoded value is structurally safe for card adoption.

    Model output is untrusted. Merely carrying ``config`` and ``elements``
    keys is not enough: malformed child values used to reach ``.get`` calls
    in Memorial and abort the whole post-hook batch.
    """
    if not isinstance(card, dict):
        return False
    if not isinstance(card.get("config"), dict):
        return False
    elements = card.get("elements")
    if not isinstance(elements, list):
        return False
    header = card.get("header")
    if header is not None:
        if not isinstance(header, dict):
            return False
        title = header.get("title")
        if title is not None and not isinstance(title, dict):
            return False
    for element in elements:
        if not isinstance(element, dict):
            return False
        text = element.get("text")
        if text is not None and not isinstance(text, dict):
            return False
        actions = element.get("actions")
        if actions is None:
            continue
        if not isinstance(actions, list):
            return False
        for action in actions:
            if not isinstance(action, dict):
                return False
            action_text = action.get("text")
            if action_text is not None and not isinstance(action_text, dict):
                return False
            value = action.get("value")
            if value is not None and not isinstance(value, dict):
                return False
    return True


def memorial_action_id(card: object) -> str:
    """Extract one Memorial callback id from potentially untrusted JSON."""
    if not isinstance(card, dict):
        return ""
    elements = card.get("elements")
    if not isinstance(elements, list):
        return ""
    for element in elements:
        if not isinstance(element, dict):
            continue
        actions = element.get("actions")
        if not isinstance(actions, list):
            continue
        for action in actions:
            if not isinstance(action, dict):
                continue
            value = action.get("value")
            if not isinstance(value, dict):
                continue
            if value.get("action") == "memorial" and value.get("id"):
                return str(value["id"])
    return ""


def _as_card(text: str) -> dict | None:
    stripped = text.strip()
    if not stripped.startswith("{"):
        return None
    try:
        card = json.loads(stripped)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    if is_card_payload(card):
        return card
    return None


def trusted_ledger_card_id(
    card: dict,
    memorial_id_of: Callable[[dict], str],
    expected_card_of: Callable[[str], dict | None],
) -> str:
    """Return an id only when ``card`` exactly matches its ledger rendering."""
    try:
        memorial_id = memorial_id_of(card)
    except (AttributeError, KeyError, TypeError, ValueError):
        return ""
    if not memorial_id:
        return ""
    try:
        expected = expected_card_of(memorial_id)
    except (KeyError, json.JSONDecodeError, TypeError, ValueError):
        return ""
    return memorial_id if isinstance(expected, dict) and card == expected else ""


def strip_memorial_actions(card: dict) -> None:
    """Remove ledger callbacks from a card that failed provenance checks."""
    elements = card.get("elements", [])
    if not isinstance(elements, list):
        return
    for element in elements:
        if not isinstance(element, dict):
            continue
        actions = element.get("actions")
        if not isinstance(actions, list):
            continue
        element["actions"] = [
            action for action in actions
            if not (
                isinstance(action, dict)
                and isinstance(action.get("value"), dict)
                and action["value"].get("action") == "memorial"
            )
        ]


def envelope_bare_cards(output_lines: list[str],
                        trusted_memorial_id_of: Callable[[dict], str]) -> list[str]:
    """Return ``output_lines`` with bare ledger cards wrapped as ``CARD:``."""
    first_nonempty = next((line for line in output_lines if line.strip()), "")
    top_level = first_nonempty == first_nonempty.lstrip(" \t")
    # One standalone legacy card (with or without a memorial id) stays
    # backward-compatible exactly as before.
    whole = _as_card("\n".join(output_lines)) if top_level else None
    if whole is not None:
        return ["CARD:" + json.dumps(whole, ensure_ascii=False)]
    out: list[str] = []
    for line in output_lines:
        card = _as_card(line) if line == line.lstrip(" \t") else None
        if card is not None and trusted_memorial_id_of(card):
            out.append("CARD:" + json.dumps(card, ensure_ascii=False))
        else:
            out.append(line)
    return out
