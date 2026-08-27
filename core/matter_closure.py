"""Authoritative, recoverable closure for one durable Matter.

A Result Receipt releases an executor window.  It does not close the Matter.
This module owns the separate transition that is allowed only after explicit
owner confirmation.  The transition converges linked Intents, Items, and
Handoffs before the Matter reaches ``done``; live execution, Jobs, or
Delegations remain hard blockers.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from core.matter_runs import ACTIVE_STATUSES, list_runs, recover_expired_runs
from core.matters import add_event, get_matter, link_entity, open_followups, update_matter


AUTO_RECONCILABLE = {"intent", "memorial"}


@dataclass
class MatterClosureBlocked(ValueError):
    """The closure has authoritative words but dependent work is still live."""

    message: str
    blockers: list[dict[str, Any]]

    def __str__(self) -> str:
        return self.message


class MatterClosureConflict(ValueError):
    """A closed Matter already has a different authoritative receipt."""


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _text(value: Any, *, field: str, limit: int) -> str:
    clean = " ".join(str(value or "").split())
    if len(clean) > limit:
        raise ValueError(f"{field} is too long")
    return clean


def _existing_receipt(matter: dict[str, Any]) -> dict[str, Any] | None:
    for event in matter.get("events", []):
        if event.get("event_type") != "matter_closure_completed":
            continue
        payload = event.get("payload") or {}
        receipt = payload.get("receipt")
        if isinstance(receipt, dict) and receipt.get("closure_id"):
            return receipt
    return None


def _requested_at(matter: dict[str, Any], closure_id: str) -> str:
    matches = [
        str(event.get("created_at") or "")
        for event in matter.get("events", [])
        if event.get("event_type") == "matter_closure_requested"
        and (event.get("payload") or {}).get("closure_id") == closure_id
        and event.get("created_at")
    ]
    return min(matches) if matches else ""


def closure_preview(
    matter_id: str, *, now: float | None = None, recover_expired: bool = True,
) -> dict[str, Any]:
    """Describe exactly what closure would reconcile and what still blocks it."""
    matter = get_matter(matter_id)
    if matter is None:
        raise KeyError(f"matter not found: {matter_id}")
    if recover_expired:
        recover_expired_runs(matter_id=matter_id, now=now)
    active_runs = [
        run for run in list_runs(matter_id=matter_id, limit=20)
        if run.get("status") in ACTIVE_STATUSES
    ]
    followups = open_followups(matter_id)
    reconcilable = [
        item for item in followups
        if item.get("entity_type") in AUTO_RECONCILABLE
    ]
    blockers = [
        {
            "entity_type": "run",
            "entity_id": run.get("id", ""),
            "title": run.get("task") or f"{run.get('executor', 'executor')} run",
            "status": run.get("status", ""),
        }
        for run in active_runs
    ] + [
        item for item in followups
        if item.get("entity_type") not in AUTO_RECONCILABLE
    ]
    return {
        "schema": "jarvis.matter-closure-preview.v1",
        "matter": {
            "id": matter["id"],
            "title": matter["title"],
            "status": matter["status"],
        },
        "reconcilable": reconcilable,
        "blockers": blockers,
        "can_close": not blockers,
    }


def _reconcile_intent(matter_id: str, item: dict[str, Any], outcome: str) -> bool:
    from core.intentions import cancel_intent

    intent_id = str(item.get("entity_id") or "")
    return cancel_intent(
        intent_id,
        reason=f"Matter 已闭环：{outcome}",
        source=f"matter-closure:{matter_id}",
    )


def _reconcile_memorial(matter_id: str, item: dict[str, Any], outcome: str) -> bool:
    from core.memorial import get_memorial, resolve

    memorial_id = str(item.get("entity_id") or "")
    changed = resolve(
        memorial_id,
        "已随事项闭环",
        outcome,
        sync_lark=True,
    )
    state = get_memorial(memorial_id) or {}
    link_entity(
        matter_id,
        "memorial",
        memorial_id,
        provider="jarvis",
        title=state.get("title") or item.get("title") or memorial_id,
        metadata={
            "source": state.get("source", ""),
            "status": state.get("status", ""),
            "resolution": state.get("resolved_label", ""),
            "review_surface": state.get("review_surface", ""),
        },
        actor="matter-closure",
    )
    return changed


def close_matter(
    matter_id: str,
    *,
    outcome: str,
    confirmation_text: str,
    source: str = "codex",
    now: float | None = None,
) -> dict[str, Any]:
    """Close one Matter after explicit owner confirmation.

    This is an idempotent reconciliation saga rather than a cross-store
    transaction: Intent state is SQLite-backed while Items use an append-only
    ledger.  A failed attempt never marks the Matter done; rerunning converges
    the remaining linked objects and writes one stable closure receipt.
    """
    matter_id = _text(matter_id, field="matter_id", limit=120)
    outcome = _text(outcome, field="outcome", limit=1600)
    confirmation = _text(
        confirmation_text, field="owner confirmation", limit=500,
    )
    source = _text(source, field="source", limit=80) or "codex"
    if not confirmation:
        raise ValueError("explicit owner confirmation is required")
    if not outcome:
        outcome = confirmation

    matter = get_matter(matter_id)
    if matter is None:
        raise KeyError(f"matter not found: {matter_id}")
    preview = closure_preview(matter_id, now=now)
    if preview["blockers"]:
        raise MatterClosureBlocked(
            f"Matter closure blocked by {len(preview['blockers'])} live object(s)",
            preview["blockers"],
        )

    closure_identity = {
        "matter_id": matter_id,
        "authority": "owner_confirmation",
        "confirmation": confirmation,
        "outcome": outcome,
    }
    closure_id = f"mcl_{_digest(closure_identity).split(':', 1)[1][:20]}"
    existing = _existing_receipt(get_matter(matter_id) or matter)
    if existing and existing.get("closure_id") != closure_id:
        raise MatterClosureConflict(
            "Matter already has a different authoritative closure receipt"
        )
    if existing and not preview["reconcilable"]:
        return existing

    add_event(
        matter_id,
        "matter_closure_requested",
        "Owner 已确认事项完成；开始收口关联状态",
        actor="owner",
        payload={
            "closure_id": closure_id,
            "authority": "owner_confirmation",
            "confirmation": confirmation,
            "source": source,
            "reconcilable": preview["reconcilable"],
        },
    )

    reconciled: list[dict[str, str]] = []
    failures: list[dict[str, str]] = []
    for item in preview["reconcilable"]:
        entity_type = str(item.get("entity_type") or "")
        entity_id = str(item.get("entity_id") or "")
        try:
            if entity_type == "intent":
                changed = _reconcile_intent(matter_id, item, outcome)
            else:
                changed = _reconcile_memorial(matter_id, item, outcome)
            reconciled.append({
                "entity_type": entity_type,
                "entity_id": entity_id,
                "result": "changed" if changed else "already_terminal",
            })
        except Exception as exc:
            failures.append({
                "entity_type": entity_type,
                "entity_id": entity_id,
                "error_type": type(exc).__name__,
            })

    remaining = open_followups(matter_id)
    if failures or remaining:
        blockers = failures + remaining
        add_event(
            matter_id,
            "matter_closure_incomplete",
            "关联状态尚未全部收口，Matter 保持原状态",
            actor="matter-closure",
            payload={
                "closure_id": closure_id,
                "reconciled": reconciled,
                "blockers": blockers,
            },
        )
        raise MatterClosureBlocked(
            f"Matter closure left {len(blockers)} unresolved object(s)", blockers,
        )

    target_status = "archived" if matter.get("status") == "archived" else "done"
    closed = update_matter(
        matter_id,
        actor="owner",
        status=target_status,
        outcome=outcome,
        next_action="",
    )
    requested_at = _requested_at(get_matter(matter_id) or {}, closure_id)
    receipt = {
        "schema": "jarvis.matter-closure-receipt.v1",
        "closure_id": closure_id,
        "matter_id": matter_id,
        "status": "closed",
        "matter_status": closed["status"],
        "outcome": outcome,
        "authority": "owner_confirmation",
        "confirmation": confirmation,
        "source": source,
        "requested_at": requested_at,
        "closed_at": closed.get("closed_at", ""),
        "reconciled": reconciled,
    }
    receipt["receipt_digest"] = _digest(receipt)
    add_event(
        matter_id,
        "matter_closure_completed",
        "事项及关联提醒已统一闭环",
        actor="matter-closure",
        payload={"receipt": receipt},
    )
    return receipt
