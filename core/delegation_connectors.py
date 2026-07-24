"""Bridge proven connector receipts into the generic Delegation contract."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from core.delegations import DelegationConflict, DelegationStore


def _digest(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


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
        return detail
    if (
        detail["expected_postcondition"] != expected
        or detail["verification_policy"] != policy
    ):
        delegation = store.revise_contract(
            delegation["id"],
            expected_version=detail["contract_version"],
            expected_postcondition=expected,
            verification_policy=policy,
            actor_id="connector-bridge",
        )
        detail = store.get(delegation["id"])
    step = detail["steps"][0] if detail["steps"] else store.add_step(
        delegation["id"],
        expected_version=delegation["contract_version"],
        sequence=1,
        kind=verifier,
        executor=source,
    )
    if step["status"] == "pending":
        try:
            store.claim_step(
                delegation["id"],
                step["id"],
                expected_version=delegation["contract_version"],
                owner=source,
                lease_seconds=120,
            )
            store.record_attempt(
                delegation["id"],
                step["id"],
                expected_version=delegation["contract_version"],
                owner=source,
                succeeded=True,
                artifact_locator=resource_locator,
            )
        except DelegationConflict:
            pass
    store.record_evidence(
        delegation["id"],
        step["id"],
        expected_version=delegation["contract_version"],
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
    return store.get(delegation["id"])
