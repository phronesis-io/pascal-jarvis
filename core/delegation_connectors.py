"""Bridge proven connector receipts into the generic Delegation contract."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from core.delegations import DelegationConflict, DelegationStore


def _digest(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def reserve_connector_action(
    *,
    source: str,
    source_ref: str,
    title: str,
    operation: str,
    target_type: str,
    target_id: str,
    target_label: str,
    authority: str,
    verifier: str,
    expected: dict[str, Any],
    verification_policy: dict[str, Any] | None = None,
    matter_id: str = "",
    principal_id: str = "owner",
    root: str | Path | None = None,
    store: DelegationStore | None = None,
) -> tuple[DelegationStore, dict[str, Any], dict[str, Any] | None]:
    """Persist a connector action before its external mutation starts."""
    store = store or DelegationStore(root=root)
    policy = {
        "verifier": verifier,
        **dict(verification_policy or {}),
    }
    delegation, _ = store.create(
        principal_id=principal_id,
        source=source,
        source_ref=source_ref,
        title=title,
        operation=operation,
        matter_id=matter_id,
        risk_tier=2,
        target_type=target_type,
        target_id=target_id,
        target_label=target_label,
        expected_postcondition=expected,
        authority=authority,
        verification_policy=policy,
        capture_mode="explicit",
        authorized=True,
    )
    detail = store.get(delegation["id"])
    if detail["status"] == "completed":
        return store, detail, None
    if (
        detail["expected_postcondition"] != expected
        or detail["verification_policy"] != policy
    ):
        store.revise_contract(
            delegation["id"],
            expected_version=detail["contract_version"],
            expected_postcondition=expected,
            verification_policy=policy,
            actor_id="connector-bridge",
        )
        detail = store.get(delegation["id"])
    step = detail["steps"][0] if detail["steps"] else store.add_step(
        delegation["id"],
        expected_version=detail["contract_version"],
        sequence=1,
        kind=verifier,
        executor=source,
    )
    return store, store.get(delegation["id"]), step


def record_connector_receipt(
    *,
    source: str,
    source_ref: str,
    title: str,
    operation: str,
    target_type: str,
    target_id: str,
    target_label: str,
    authority: str,
    verifier: str,
    expected: dict[str, Any],
    observed: dict[str, Any],
    matched: bool,
    resource_locator: str,
    verification_policy: dict[str, Any] | None = None,
    matter_id: str = "",
    principal_id: str = "owner",
    root: str | Path | None = None,
    store: DelegationStore | None = None,
) -> dict[str, Any]:
    """Create/adopt one connector action and persist its authoritative receipt.

    Connector-specific idempotency still owns the external mutation.  This
    bridge is the common control-plane projection and therefore never retries
    the mutation itself.
    """
    store, detail, step = reserve_connector_action(
        source=source,
        source_ref=source_ref,
        title=title,
        operation=operation,
        target_type=target_type,
        target_id=target_id,
        target_label=target_label,
        authority=authority,
        verifier=verifier,
        expected=expected,
        verification_policy=verification_policy,
        matter_id=matter_id,
        principal_id=principal_id,
        root=root,
        store=store,
    )
    if detail["status"] == "completed":
        return detail
    if step is None:
        raise DelegationConflict("connector action has no executable step")
    if step["status"] == "pending":
        try:
            store.claim_step(
                detail["id"],
                step["id"],
                expected_version=detail["contract_version"],
                owner=source,
                lease_seconds=120,
            )
            store.record_attempt(
                detail["id"],
                step["id"],
                expected_version=detail["contract_version"],
                owner=source,
                succeeded=True,
                artifact_locator=resource_locator,
            )
        except DelegationConflict:
            pass
    store.record_evidence(
        detail["id"],
        step["id"],
        expected_version=detail["contract_version"],
        evidence_type="connector_readback",
        strength="strong",
        authority=authority,
        resource_locator=resource_locator,
        observed_digest=_digest(observed),
        expected_summary=json.dumps(expected, ensure_ascii=False, sort_keys=True),
        observed_summary=json.dumps(observed, ensure_ascii=False, sort_keys=True),
        matched=matched,
        metadata={"connector": source},
        actor_id=verifier,
    )
    return store.get(detail["id"])


def project_eigenflux_message_receipt(
    receipt: Any,
    *,
    root: str | Path | None = None,
    store: DelegationStore | None = None,
) -> dict[str, Any]:
    """Project one durable EigenFlux action under its stable idempotency key."""
    key = str(getattr(receipt, "idempotency_key", "") or "")
    if not key:
        raise ValueError("EigenFlux message receipt has no idempotency key")
    recipient_id = str(getattr(receipt, "recipient_id", "") or "")
    state = str(getattr(receipt, "state", "") or "")
    msg_id = str(getattr(receipt, "msg_id", "") or "")
    conv_id = str(getattr(receipt, "conv_id", "") or "")
    return record_connector_receipt(
        source="eigenflux-message",
        source_ref=f"attempt:{key}",
        title=(
            "发送消息给 "
            + str(getattr(receipt, "recipient_name", "") or recipient_id)
        ),
        operation="message_send",
        target_type="agent",
        target_id=recipient_id,
        target_label=str(
            getattr(receipt, "recipient_name", "") or recipient_id
        ),
        authority="eigenflux_message_history",
        verifier="eigenflux_message",
        expected={"state": "verified", "target_id": recipient_id},
        observed={
            "state": state,
            "target_id": recipient_id,
            "msg_id": msg_id,
            "conv_id": conv_id,
        },
        matched=state == "verified",
        resource_locator=(
            f"eigenflux-message:{msg_id}"
            if msg_id
            else f"eigenflux-conversation:{conv_id}"
        ),
        verification_policy={
            "idempotency_key": key,
            "msg_id": msg_id,
        },
        root=root,
        store=store,
    )


def repair_eigenflux_message_projections(
    *,
    root: str | Path | None = None,
    store: DelegationStore | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """Rebuild missing control-plane projections from the durable action log."""
    from core.eigenflux_messages import MessageReceipt

    store = store or DelegationStore(root=root)
    repaired = 0
    errors: list[dict[str, str]] = []
    try:
        with closing(sqlite3.connect(str(store.db_path), timeout=5)) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(
                """
                SELECT a.* FROM verified_external_actions AS a
                 WHERE a.action_type='eigenflux_message'
                   AND NOT EXISTS (
                       SELECT 1 FROM delegations AS d
                        WHERE d.source='eigenflux-message'
                          AND d.source_ref=(
                              'attempt:' || a.idempotency_key
                          )
                   )
                 ORDER BY updated_epoch ASC LIMIT ?
                """,
                (max(1, min(int(limit), 500)),),
            ).fetchall()
    except sqlite3.Error as exc:
        if "no such table" in str(exc).lower():
            return {"repaired": 0, "errors": []}
        return {
            "repaired": 0,
            "errors": [{"idempotency_key": "", "error": str(exc)[:200]}],
        }
    for row in rows:
        receipt = MessageReceipt(
            state=str(row["state"]),
            recipient_name=str(row["target_name"]),
            recipient_id=str(row["target_id"]),
            idempotency_key=str(row["idempotency_key"]),
            msg_id=str(row["msg_id"]),
            conv_id=str(row["conv_id"]),
            duplicate=True,
            detail=str(row["last_error"]),
        )
        try:
            project_eigenflux_message_receipt(
                receipt, root=root, store=store
            )
            repaired += 1
        except Exception as exc:
            errors.append(
                {
                    "idempotency_key": receipt.idempotency_key,
                    "error": str(exc)[:200],
                }
            )
    return {"repaired": repaired, "errors": errors}
