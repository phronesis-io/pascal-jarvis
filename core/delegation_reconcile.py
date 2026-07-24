"""Bounded reconciliation and user-attention projection for Delegations."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from core.delegations import DelegationConflict, DelegationStore
from core.delegation_verify import VerificationError, VerifierRegistry, verify_step


def _attention_link(detail: dict[str, Any]) -> str:
    return next(
        (
            str(link["entity_id"])
            for link in detail.get("links", [])
            if link.get("entity_type") == "memorial"
            and link.get("relation") == "needs_attention"
        ),
        "",
    )


def sync_attention_item(
    detail: dict[str, Any],
    *,
    store: DelegationStore,
    send: bool = True,
) -> str:
    """Maintain at most one Item for a Delegation that genuinely needs Pascal."""
    from core import memorial

    existing = _attention_link(detail)
    needs_attention = detail["status"] in {"needs_user", "needs_clarification"}
    if not needs_attention:
        if existing:
            memorial.resolve(
                existing,
                "已处理",
                f"委托现在是：{detail['status']}",
            )
        return existing
    if existing and memorial.get_memorial(existing) is not None:
        return existing

    if detail["status"] == "needs_user" and detail.get("target_id"):
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
        title="需要你 · 委托确认",
        body=body,
        options=options,
        matter_id=str(detail.get("matter_id") or ""),
        dedup_key=f"delegation-attention:{detail['id']}",
        context=json.dumps(
            {
                "kind": "delegation_attention",
                "delegation_id": detail["id"],
                "contract_version": detail["contract_version"],
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
        released = self.store.release_expired_leases(limit=limit)
        scanned = 0
        verified = 0
        deferred = 0
        needs_user = 0
        errors: list[dict[str, str]] = []
        rows: list[dict[str, Any]] = []
        for status in ("verifying", "awaiting_external", "blocked", "needs_user"):
            remaining = limit - len(rows)
            if remaining <= 0:
                break
            rows.extend(self.store.list(status=status, limit=remaining))

        for row in rows:
            scanned += 1
            detail = self.store.get(row["id"])
            if detail["status"] == "needs_user":
                sync_attention_item(detail, store=self.store, send=send_items)
                needs_user += 1
                continue
            policy = detail["verification_policy"]
            timeout = max(
                60, int(policy.get("verification_timeout_seconds", 3600))
            )
            age = self.now() - float(detail["updated_at"])
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
            for step in candidates:
                try:
                    result = verify_step(
                        detail["id"],
                        step["id"],
                        store=self.store,
                        registry=self.registry,
                    )
                    if result["matched"]:
                        verified += 1
                        if detail["status"] == "awaiting_external":
                            try:
                                self.store.resume_external(
                                    detail["id"],
                                    expected_version=detail["contract_version"],
                                )
                                self.store.evaluate_completion(detail["id"])
                            except DelegationConflict:
                                pass
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
    return 0 if not result["errors"] else 3


if __name__ == "__main__":
    sys.exit(main())
