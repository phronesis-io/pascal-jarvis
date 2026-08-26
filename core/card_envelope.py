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


def _as_card(text: str) -> dict | None:
    stripped = text.strip()
    if not stripped.startswith("{"):
        return None
    try:
        card = json.loads(stripped)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    if isinstance(card, dict) and "config" in card and "elements" in card:
        return card
    return None


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
