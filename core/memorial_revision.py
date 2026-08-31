"""Append-only revision of one pending memorial and its delivery surface."""

from __future__ import annotations

import json
import time

from core.card import extract_card_text
from core.timeutil import now_local_str


def revise_pending(
    memorial_id: str,
    *,
    title: str | None = None,
    body: str | None = None,
    context: str | None = None,
    options: list[dict] | None = None,
    recommend: dict | None = None,
    work_receipt: str | None = None,
    authoring_audit_text: str | None = None,
) -> bool:
    """Revise one pending card without creating another notification.

    Importing the compatibility facade at call time keeps this module on the
    orchestration side of the ledger boundary without making memorial import
    it back. The revision receipt preserves both versions; queued envelopes
    are rewritten and delivered cards are PATCHed in place.
    """
    from core import memorial

    current = memorial.get_memorial(memorial_id)
    if current is None or current.get("status") != "pending":
        return False
    event: dict = {
        "ev": "revise",
        "id": str(memorial_id),
        "ts": now_local_str(),
        "epoch": int(time.time()),
    }
    for key, value in (("title", title), ("body", body), ("context", context)):
        if value is not None:
            event[key] = str(value)
    if options is not None:
        event["options"] = memorial._normalize_options(options, None)
        event["recommend"] = memorial._normalize_recommendation(
            recommend, event["options"]
        )
    elif recommend is not None:
        event["recommend"] = memorial._normalize_recommendation(
            recommend, current.get("options", [])
        )
    if work_receipt is not None:
        event["work_receipt"] = " ".join(str(work_receipt).split())[
            :memorial.MAX_WORK_RECEIPT_CHARS
        ]
    if authoring_audit_text is not None:
        event["authoring_audit_text"] = str(authoring_audit_text)
    memorial._append_line(memorial._ledger_path(), event)
    revised = memorial.get_memorial(memorial_id)
    if revised is None:
        return False
    rendered = memorial._render_card(revised)
    try:
        from core.delivery import DeliveryPipeline

        DeliveryPipeline(memorial.runtime_root()).replace_memorial_payload(
            memorial_id,
            card_json=rendered,
            text=extract_card_text(rendered),
        )
    except Exception as exc:
        memorial._ops_log(
            "revision_delivery_sync_failed",
            level="warn",
            memorial_id=memorial_id,
            error_type=type(exc).__name__,
        )
    if revised.get("delivery_status") in {"delivered", "read", "acted"}:
        memorial._sync_lark_card(memorial_id, json.loads(rendered))
    return True
