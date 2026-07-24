"""Bounded reconciliation and user-attention projection for Delegations."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from core.delegations import (
    DelegationStore,
    is_confirmable,
    is_retryable,
)
from core.delegation_verify import VerificationError, VerifierRegistry, verify_step


def _attention_link(detail: dict[str, Any]) -> str:
    candidates = [
        link
        for link in detail.get("links", [])
        if link.get("entity_type") == "memorial"
        and link.get("relation") == "needs_attention"
    ]
    if not candidates:
        return ""
    _, newest = max(
        enumerate(candidates),
        key=lambda item: (float(item[1].get("created_at") or 0), item[0]),
    )
    return str(newest["entity_id"])


def sync_attention_item(
    detail: dict[str, Any],
    *,
    store: DelegationStore,
    send: bool = True,
) -> str:
    """Maintain at most one Item for a Delegation that genuinely needs Pascal."""
    from core import memorial

    if "links" not in detail:
        detail = store.get(str(detail["id"]))
    existing = _attention_link(detail)
    needs_attention = detail["status"] in {
        "needs_user",
        "needs_clarification",
        "failed",
    }
    attention_state = ":".join(
        (
            str(detail.get("status") or ""),
            str(detail.get("waiting_on") or ""),
            str(detail.get("last_error_code") or ""),
        )
    )
    if not needs_attention:
        if existing:
            memorial.resolve(
                existing,
                "已处理",
                f"委托现在是：{detail['status']}",
            )
        return existing
    if existing:
        state = memorial.get_memorial(existing)
        context: dict[str, Any] = {}
        if state is not None:
            try:
                context = json.loads(str(state.get("context") or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                context = {}
        current_item = bool(
            state is not None
            and state.get("status") == "pending"
            and context.get("kind") == "delegation_attention"
            and str(context.get("delegation_id") or "") == str(detail["id"])
            and int(context.get("contract_version") or 0)
            == int(detail["contract_version"])
            and str(context.get("attention_state") or "") == attention_state
        )
        if current_item:
            return existing
        if state is not None:
            memorial.resolve(
                existing,
                "已失效",
                f"委托契约已更新到 v{detail['contract_version']}",
            )

    item_title = "需要你 · 委托确认"
    if int(detail.get("risk_tier") or 0) >= 4:
        options = [
            {
                "key": "cancel",
                "label": "关闭委托",
                "action": {
                    "type": "delegation_cancel",
                    "params": {
                        "id": detail["id"],
                        "version": str(detail["contract_version"]),
                    },
                },
            }
        ]
        body = (
            f"{detail['title']}\n\n"
            "这是 R4 高风险事项，Jarvis 不会代为执行。请由你本人在权威系统"
            "中完成；这个委托只能作为提醒保留或由你关闭。"
        )
    elif is_confirmable(detail):
        options = [
            {
                "key": "confirm",
                "label": "确认执行",
                "action": {
                    "type": "delegation_confirm",
                    "params": {
                        "id": detail["id"],
                        "version": str(detail["contract_version"]),
                        "principal": detail["principal_id"],
                    },
                },
            },
            {
                "key": "cancel",
                "label": "取消",
                "action": {
                    "type": "delegation_cancel",
                    "params": {
                        "id": detail["id"],
                        "version": str(detail["contract_version"]),
                    },
                },
            },
        ]
        body = (
            f"{detail['title']}\n\n"
            f"目标：{detail.get('target_label') or detail.get('target_id')}\n"
            f"风险：R{detail['risk_tier']}\n"
            "系统会在你确认后执行，并从权威来源回读结果。"
        )
    elif detail["status"] == "failed":
        item_title = "需要你 · 委托失败"
        options = [
            {
                "key": "retry",
                "label": "重试执行",
                "action": {
                    "type": "delegation_retry",
                    "params": {
                        "id": detail["id"],
                        "version": str(detail["contract_version"]),
                    },
                },
            },
            {
                "key": "cancel",
                "label": "取消委托",
                "action": {
                    "type": "delegation_cancel",
                    "params": {
                        "id": detail["id"],
                        "version": str(detail["contract_version"]),
                    },
                },
            },
        ]
        error_code = str(detail.get("last_error_code") or "execution_failed")
        body = (
            f"{detail['title']}\n\n"
            f"执行没有成功（{error_code}）。重试会重新执行未完成步骤；"
            "如果不再需要，请取消委托。"
        )
    elif is_retryable(detail):
        item_title = "需要你 · 恢复核验"
        options = [
            {
                "key": "retry",
                "label": "重新核验",
                "action": {
                    "type": "delegation_retry",
                    "params": {
                        "id": detail["id"],
                        "version": str(detail["contract_version"]),
                    },
                },
            },
            {
                "key": "cancel",
                "label": "取消委托",
                "action": {
                    "type": "delegation_cancel",
                    "params": {
                        "id": detail["id"],
                        "version": str(detail["contract_version"]),
                    },
                },
            },
        ]
        body = (
            f"{detail['title']}\n\n"
            "权威回读在限定时间内没有得到结论。重新核验只恢复读回流程，"
            "不会凭模型判断完成，也不会自动重复外部写操作。"
        )
    else:
        options = [
            {
                "key": "cancel",
                "label": "取消委托",
                "action": {
                    "type": "delegation_cancel",
                    "params": {
                        "id": detail["id"],
                        "version": str(detail["contract_version"]),
                    },
                },
            }
        ]
        body = (
            f"{detail['title']}\n\n"
            "对象或完成条件还不够明确。请在当前飞书对话补充一次，"
            "系统不会猜测目标后执行。"
        )
    memorial_id, _ = memorial.create(
        source="delegation",
        title=item_title,
        body=body,
        options=options,
        matter_id=str(detail.get("matter_id") or ""),
        dedup_key=(
            f"delegation-attention:{detail['id']}:"
            f"v{detail['contract_version']}:{attention_state}"
        ),
        context=json.dumps(
            {
                "kind": "delegation_attention",
                "delegation_id": detail["id"],
                "contract_version": detail["contract_version"],
                "attention_state": attention_state,
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
        send=send,
    )
    store.link(
        detail["id"],
        "memorial",
        memorial_id,
        relation="needs_attention",
    )
    return memorial_id


class DelegationReconciler:
    """Scan only active rows, release stale leases, and retry readbacks."""

    def __init__(
        self,
        root: str | Path | None = None,
        *,
        store: DelegationStore | None = None,
        registry: VerifierRegistry | None = None,
        now=time.time,
    ):
        self.store = store or DelegationStore(root=root)
        self.registry = registry or VerifierRegistry(
            root=self.store.root, db_path=self.store.db_path
        )
        self.now = now

    def run(self, *, limit: int = 50, send_items: bool = True) -> dict[str, Any]:
        limit = max(1, min(int(limit), 200))
        from core.delegation_connectors import (
            repair_eigenflux_message_projections,
        )

        projection_repair = repair_eigenflux_message_projections(
            root=self.store.root,
            store=self.store,
            limit=limit,
        )
        released = self.store.release_expired_leases(limit=limit)
        scanned = 0
        verified = 0
        deferred = 0
        needs_user = 0
        errors: list[dict[str, str]] = []
        statuses = (
            "needs_user",
            "needs_clarification",
            "failed",
            "verifying",
            "awaiting_external",
            "blocked",
            "bound",
        )
        buckets = [
            self.store.list(status=status, limit=limit)
            for status in statuses
        ]
        active_buckets = [bucket for bucket in buckets if bucket]
        rows: list[dict[str, Any]] = []
        # Round-robin preserves urgent-first ordering while guaranteeing that
        # a large attention backlog cannot starve verification and recovery.
        for index in range(limit):
            active_buckets = [bucket for bucket in active_buckets if bucket]
            if not active_buckets:
                break
            bucket = active_buckets[index % len(active_buckets)]
            rows.append(bucket.pop(0))

        for row in rows:
            scanned += 1
            detail = self.store.get(row["id"])
            if detail["status"] == "bound" and detail.get("source") != "taskline":
                continue
            if detail["status"] in {
                "needs_user",
                "needs_clarification",
                "failed",
            }:
                sync_attention_item(detail, store=self.store, send=send_items)
                needs_user += 1
                continue
            if (
                detail.get("source") == "taskline"
                and (
                    not detail["verification_policy"].get("release_sha")
                    or any(
                        step["required"] and step["status"] == "pending"
                        for step in detail["steps"]
                    )
                )
            ):
                try:
                    from core.taskline_bridge import (
                        TasklineBridge,
                        TasklineBridgeError,
                    )

                    detail = TasklineBridge(
                        root=self.store.root,
                        db_path=self.store.db_path,
                    ).refresh_release(str(detail.get("source_ref") or ""))
                except TasklineBridgeError as exc:
                    deferred += 1
                    errors.append(
                        {
                            "delegation_id": detail["id"],
                            "step_id": "",
                            "error": str(exc)[:200],
                        }
                    )
                    continue
                if not detail["verification_policy"].get("release_sha"):
                    deferred += 1
                    continue
            if detail["status"] == "awaiting_external":
                detail = self.store.recover_external_completion(detail["id"])
                if detail["status"] == "completed":
                    verified += 1
                    sync_attention_item(
                        detail, store=self.store, send=send_items
                    )
                    continue
            policy = detail["verification_policy"]
            timeout = max(
                60, int(policy.get("verification_timeout_seconds", 3600))
            )
            candidates = [
                step
                for step in detail["steps"]
                if step["required"]
                and step["status"] in {
                    "verifying",
                    "awaiting_external",
                    "blocked",
                }
            ]
            if not candidates:
                continue
            phase_started_at = min(
                float(
                    step.get("started_at")
                    or detail.get("started_at")
                    or detail["created_at"]
                )
                for step in candidates
            )
            age = self.now() - phase_started_at
            for step in candidates:
                try:
                    result = verify_step(
                        detail["id"],
                        step["id"],
                        store=self.store,
                        registry=self.registry,
                        resume_external=(
                            detail["status"] == "awaiting_external"
                        ),
                    )
                    if result["matched"]:
                        verified += 1
                    else:
                        deferred += 1
                except VerificationError as exc:
                    deferred += 1
                    errors.append(
                        {
                            "delegation_id": detail["id"],
                            "step_id": step["id"],
                            "error": str(exc)[:200],
                        }
                    )
            refreshed = self.store.get(detail["id"])
            if (
                refreshed["status"] in {"verifying", "blocked"}
                and age >= timeout
            ):
                self.store.mark_waiting(
                    detail["id"],
                    expected_version=detail["contract_version"],
                    waiting_on="verification_recovery",
                    needs_user=True,
                    reason_code="verification_budget_exhausted",
                )
                sync_attention_item(
                    self.store.get(detail["id"]),
                    store=self.store,
                    send=send_items,
                )
                needs_user += 1
            else:
                sync_attention_item(
                    refreshed, store=self.store, send=send_items
                )
        return {
            "connector_projections_repaired": projection_repair["repaired"],
            "connector_projection_errors": projection_repair["errors"],
            "released_leases": released,
            "scanned": scanned,
            "verified": verified,
            "deferred": deferred,
            "needs_user": needs_user,
            "errors": errors,
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Reconcile active Delegations")
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--no-send", action="store_true")
    args = parser.parse_args(argv)
    result = DelegationReconciler().run(
        limit=args.limit, send_items=not args.no_send
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
