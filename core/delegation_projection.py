"""One-way projections from authoritative Delegation state."""

from __future__ import annotations

from typing import Any

from core.delegations import ACTIVE_STATUSES, TERMINAL_STATUSES


def _next_action(delegation: dict[str, Any]) -> str:
    title = str(delegation.get("title") or "这项委托")
    status = str(delegation.get("status") or "")
    prefixes = {
        "captured": "待明确",
        "needs_clarification": "需要你补充",
        "needs_user": "需要你确认",
        "bound": "等待执行",
        "executing": "正在推进",
        "verifying": "正在核验",
        "awaiting_external": "等待外部结果",
        "blocked": "推进受阻",
        "failed": "执行失败，待重试",
    }
    return f"{prefixes.get(status, '继续推进')}：{title}"


def sync_projection(store, delegation_id: str) -> dict[str, Any]:
    """Project one Delegation without allowing projections to write it back."""
    detail = store.get(delegation_id)
    if detail.get("capture_mode") == "shadow":
        return {
            "delegation_id": delegation_id,
            "status": detail["status"],
            "matter_id": str(detail.get("matter_id") or ""),
            "issues": [],
            "skipped": "shadow",
        }
    matter_id = str(detail.get("matter_id") or "")
    issues: list[str] = []
    projection_source = (
        f"delegation:{delegation_id}:{detail['status']}"
    )

    if matter_id:
        try:
            from core.matters import get_matter, link_entity, update_matter

            matter = get_matter(
                matter_id, include_links=False, include_events=False
            )
            if matter is not None:
                link_entity(
                    matter_id,
                    "delegation",
                    delegation_id,
                    provider="jarvis",
                    title=str(detail.get("title") or ""),
                    metadata={
                        "status": detail["status"],
                        "contract_version": detail["contract_version"],
                    },
                    actor="delegation",
                )
                active = [
                    row
                    for row in store.list(matter_id=matter_id, limit=100)
                    if row["status"] in ACTIVE_STATUSES
                ]
                if active:
                    desired = _next_action(active[0])
                elif detail["status"] == "completed":
                    desired = (
                        f"已核验完成：{detail['title']}；确认事项是否可以办结"
                    )
                elif detail["status"] in TERMINAL_STATUSES:
                    desired = (
                        f"委托已{detail['status']}：{detail['title']}；"
                        "决定是否调整或重试"
                    )
                else:
                    desired = str(matter.get("next_action") or "")
                if desired and matter.get("next_action") != desired:
                    update_matter(
                        matter_id,
                        actor="delegation",
                        next_action=desired,
                    )
        except Exception as exc:
            issues.append(f"matter:{exc}")

    if detail["status"] in TERMINAL_STATUSES:
        try:
            from core.continuity import complete_entity_handoffs

            complete_entity_handoffs(
                "delegation",
                delegation_id,
                completion_source=projection_source,
            )
        except Exception as exc:
            issues.append(f"handoff:{exc}")
        for link in detail.get("links", []):
            if link.get("entity_type") != "intent":
                continue
            try:
                from core.intent_lifecycle import cancel_intent

                cancel_intent(
                    str(link["entity_id"]),
                    f"delegation {delegation_id} reached {detail['status']}",
                    source=projection_source,
                )
            except Exception as exc:
                issues.append(f"intent:{link.get('entity_id')}:{exc}")
    elif detail.get("last_error_code") == "duplicate_receipt_released":
        completed_source = f"delegation:{delegation_id}:completed"
        terminal_at = None
        for event in reversed(detail.get("events", [])):
            if event.get("reason_code") != "duplicate_receipt_released":
                continue
            terminal_at = event.get("metadata", {}).get("terminal_at")
            break
        try:
            from core.continuity import reopen_entity_handoffs

            reopen_entity_handoffs(
                "delegation",
                delegation_id,
                completion_source=completed_source,
                legacy_completed_at=terminal_at,
            )
        except Exception as exc:
            issues.append(f"handoff-reopen:{exc}")
        for link in detail.get("links", []):
            if link.get("entity_type") != "intent":
                continue
            try:
                from core.intent_lifecycle import restore_cancelled_intent

                restore_cancelled_intent(
                    str(link["entity_id"]),
                    source=completed_source,
                    legacy_reason=(
                        f"delegation {delegation_id} reached completed"
                    ),
                )
            except Exception as exc:
                issues.append(
                    f"intent-reopen:{link.get('entity_id')}:{exc}"
                )
    return {
        "delegation_id": delegation_id,
        "status": detail["status"],
        "matter_id": matter_id,
        "issues": issues,
    }
