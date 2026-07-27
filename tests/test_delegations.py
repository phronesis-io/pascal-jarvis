import threading

import pytest

from core.delegation_shadow import capture, classify
from core.delegations import (
    DelegationConflict,
    DelegationError,
    DelegationStore,
    is_confirmable,
    is_retryable,
)


def _store(tmp_path, now=None):
    clock = now or [1_000.0]
    return DelegationStore(
        root=tmp_path,
        db_path=tmp_path / "jarvis.db",
        now=lambda: clock[0],
    )


def _delegation(store, **overrides):
    values = {
        "principal_id": "owner",
        "source": "test",
        "source_ref": "msg-1",
        "title": "Send the report",
        "operation": "message_send",
        "target_type": "contact",
        "target_id": "agent-1",
        "target_label": "Partner",
        "expected_postcondition": {"recipient_id": "agent-1"},
        "authority": "message_service",
        "verification_policy": {"verifier": "message"},
        "authorized": True,
    }
    values.update(overrides)
    return store.create(**values)


def _step(
    store,
    delegation,
    *,
    sequence=1,
    kind="send",
    depends_on=None,
    required=True,
):
    return store.add_step(
        delegation["id"],
        expected_version=delegation["contract_version"],
        sequence=sequence,
        kind=kind,
        executor="worker",
        depends_on=depends_on or [],
        required=required,
    )


def _complete_step(store, delegation, step, *, owner="codex"):
    claim = store.claim_step(
        delegation["id"],
        step["id"],
        expected_version=delegation["contract_version"],
        owner=owner,
    )
    store.record_attempt(
        delegation["id"],
        step["id"],
        expected_version=delegation["contract_version"],
        owner=owner,
        succeeded=True,
        artifact_locator="message:123",
    )
    store.record_evidence(
        delegation["id"],
        step["id"],
        expected_version=delegation["contract_version"],
        evidence_type="readback",
        strength="strong",
        authority="message_service",
        resource_locator="message:123",
        observed_digest="sha256:" + "a" * 64,
        expected_summary="recipient=agent-1",
        observed_summary="recipient=agent-1",
        matched=True,
        actor_id="message",
    )
    return claim


def test_create_is_idempotent_by_source_event(tmp_path):
    store = _store(tmp_path)
    first, created = _delegation(store)
    second, created_again = _delegation(store)
    assert created is True
    assert created_again is False
    assert second["id"] == first["id"]


def test_bound_contract_cannot_be_rebound_while_worker_holds_lease(tmp_path):
    store = _store(tmp_path)
    delegation, _ = _delegation(store)
    step = _step(store, delegation)
    store.claim_step(
        delegation["id"], step["id"], expected_version=1, owner="worker"
    )

    with pytest.raises(DelegationConflict, match="revise the contract"):
        store.bind(
            delegation["id"],
            expected_version=1,
            target_type="contact",
            target_id="agent-2",
            target_label="Other",
            expected_postcondition={"recipient_id": "agent-2"},
            authority="message_service",
            verification_policy={"verifier": "message"},
        )

    detail = store.get(delegation["id"])
    assert detail["target_id"] == "agent-1"
    assert detail["steps"][0]["lease_owner"] == "worker"


def test_initial_unbound_r3_contract_can_be_bound_then_confirmed(tmp_path):
    store = _store(tmp_path)
    delegation, _ = _delegation(
        store,
        target_type="",
        target_id="",
        target_label="",
        authority="",
        verification_policy={},
        risk_tier=3,
        authorized=False,
    )

    bound = store.bind(
        delegation["id"],
        expected_version=1,
        target_type="contact",
        target_id="agent-1",
        target_label="Partner",
        expected_postcondition={"recipient_id": "agent-1"},
        authority="message_service",
        verification_policy={"verifier": "message"},
    )

    assert bound["status"] == "needs_user"
    assert is_confirmable(store.get(delegation["id"])) is True
    assert store.confirm(
        delegation["id"], expected_version=1, principal_id="owner"
    )["status"] == "bound"


def test_completion_requires_required_step_and_qualifying_evidence(tmp_path):
    store = _store(tmp_path)
    delegation, _ = _delegation(store)
    step = _step(store, delegation)
    claim = store.claim_step(
        delegation["id"],
        step["id"],
        expected_version=1,
        owner="codex",
    )
    assert claim.lease_owner == "codex"
    store.record_attempt(
        delegation["id"],
        step["id"],
        expected_version=1,
        owner="codex",
        succeeded=True,
    )
    store.record_evidence(
        delegation["id"],
        step["id"],
        expected_version=1,
        evidence_type="tool_output",
        strength="weak",
        authority="worker",
        resource_locator="",
        observed_digest="sha256:" + "b" * 64,
        expected_summary="sent",
        observed_summary="worker says sent",
        matched=True,
    )
    assert store.get(delegation["id"])["status"] == "verifying"

    store.record_evidence(
        delegation["id"],
        step["id"],
        expected_version=1,
        evidence_type="readback",
        strength="strong",
        authority="message_service",
        resource_locator="message:1",
        observed_digest="sha256:" + "c" * 64,
        expected_summary="recipient=agent-1",
        observed_summary="recipient=agent-1",
        matched=True,
        actor_id="message",
    )
    detail = store.get(delegation["id"])
    assert detail["status"] == "completed"
    assert detail["completed_at"] == 1_000
    assert detail["events"][-1]["event_type"] == "delegation.completed"


def test_worker_claimed_strong_evidence_cannot_forge_completion(tmp_path):
    store = _store(tmp_path)
    delegation, _ = _delegation(store)
    step = _step(store, delegation)
    store.claim_step(
        delegation["id"], step["id"], expected_version=1, owner="worker"
    )
    store.record_attempt(
        delegation["id"],
        step["id"],
        expected_version=1,
        owner="worker",
        succeeded=True,
    )

    store.record_evidence(
        delegation["id"],
        step["id"],
        expected_version=1,
        evidence_type="worker_claim",
        strength="strong",
        authority="made_up",
        resource_locator="",
        observed_digest="sha256:" + "9" * 64,
        expected_summary="sent",
        observed_summary="worker says sent",
        matched=True,
        actor_id="worker",
    )

    detail = store.get(delegation["id"])
    assert detail["status"] == "verifying"
    assert detail["steps"][0]["status"] == "verifying"
    assert detail["events"][-1]["metadata"]["trusted_verifier"] is False


def test_correct_authority_with_wrong_actor_does_not_complete(tmp_path):
    store = _store(tmp_path)
    delegation, _ = _delegation(store)
    step = _step(store, delegation)
    store.claim_step(
        delegation["id"], step["id"], expected_version=1, owner="worker"
    )
    store.record_attempt(
        delegation["id"],
        step["id"],
        expected_version=1,
        owner="worker",
        succeeded=True,
    )

    store.record_evidence(
        delegation["id"],
        step["id"],
        expected_version=1,
        evidence_type="worker_claim",
        strength="strong",
        authority="message_service",
        resource_locator="message:1",
        observed_digest="sha256:" + "8" * 64,
        expected_summary="sent",
        observed_summary="sent",
        matched=True,
        actor_id="worker",
    )

    assert store.get(delegation["id"])["status"] == "verifying"


def test_mismatch_never_completes(tmp_path):
    store = _store(tmp_path)
    delegation, _ = _delegation(store)
    step = _step(store, delegation)
    store.claim_step(
        delegation["id"], step["id"], expected_version=1, owner="codex"
    )
    store.record_attempt(
        delegation["id"],
        step["id"],
        expected_version=1,
        owner="codex",
        succeeded=True,
    )
    store.record_evidence(
        delegation["id"],
        step["id"],
        expected_version=1,
        evidence_type="readback",
        strength="strong",
        authority="message_service",
        resource_locator="message:wrong",
        observed_digest="sha256:" + "d" * 64,
        expected_summary="recipient=agent-1",
        observed_summary="recipient=agent-2",
        matched=False,
        actor_id="message",
    )
    assert store.get(delegation["id"])["status"] == "verifying"


def test_external_wait_blocks_completion_until_cleared(tmp_path):
    store = _store(tmp_path)
    delegation, _ = _delegation(store)
    step = _step(store, delegation)
    store.claim_step(
        delegation["id"], step["id"], expected_version=1, owner="codex"
    )
    store.record_attempt(
        delegation["id"],
        step["id"],
        expected_version=1,
        owner="codex",
        succeeded=True,
    )
    store.mark_waiting(
        delegation["id"],
        expected_version=1,
        waiting_on="conversation:123",
    )
    store.record_evidence(
        delegation["id"],
        step["id"],
        expected_version=1,
        evidence_type="readback",
        strength="strong",
        authority="message_service",
        resource_locator="message:123",
        observed_digest="sha256:" + "f" * 64,
        expected_summary="message sent",
        observed_summary="message sent",
        matched=True,
        actor_id="message",
    )
    assert store.get(delegation["id"])["status"] == "awaiting_external"

    revised = store.revise_contract(
        delegation["id"],
        expected_version=1,
        expected_postcondition={"reply_received": True},
    )
    assert revised["contract_version"] == 2
    assert revised["waiting_on"] == ""


def test_contract_revision_makes_old_evidence_non_qualifying(tmp_path):
    store = _store(tmp_path)
    delegation, _ = _delegation(store)
    old_step = _step(store, delegation)
    revised = store.revise_contract(
        delegation["id"],
        expected_version=1,
        target_id="agent-2",
        expected_postcondition={"recipient_id": "agent-2"},
    )
    assert store.get(delegation["id"])["steps"] == []
    with pytest.raises(DelegationConflict):
        store.claim_step(
            delegation["id"],
            old_step["id"],
            expected_version=1,
            owner="codex",
        )
    new_step = _step(store, revised)
    _complete_step(store, revised, new_step)
    assert store.get(delegation["id"])["status"] == "completed"


def test_high_risk_needs_same_principal_confirmation(tmp_path):
    store = _store(tmp_path)
    delegation, _ = _delegation(
        store,
        risk_tier=3,
        authorized=False,
        operation="public_publish",
    )
    assert delegation["status"] == "needs_user"
    with pytest.raises(DelegationConflict):
        store.confirm(
            delegation["id"], expected_version=1, principal_id="someone-else"
        )
    confirmed = store.confirm(
        delegation["id"], expected_version=1, principal_id="owner"
    )
    assert confirmed["status"] == "bound"
    assert confirmed["authorized"] == 1


def test_unapproved_high_risk_delegation_cannot_retry_past_confirmation(
    tmp_path,
):
    store = _store(tmp_path)
    delegation, _ = _delegation(
        store,
        risk_tier=3,
        authorized=False,
        operation="public_publish",
    )

    with pytest.raises(DelegationConflict, match="not retryable"):
        store.retry(delegation["id"], expected_version=1, actor_id="owner")

    detail = store.get(delegation["id"])
    assert detail["status"] == "needs_user"
    assert detail["authorized"] == 0


def test_verification_recovery_cannot_be_misread_as_risk_confirmation(tmp_path):
    store = _store(tmp_path)
    delegation, _ = _delegation(
        store,
        risk_tier=3,
        authorized=True,
        operation="public_publish",
    )
    step = _step(store, delegation)
    store.claim_step(
        delegation["id"], step["id"], expected_version=1, owner="worker"
    )
    store.record_attempt(
        delegation["id"],
        step["id"],
        expected_version=1,
        owner="worker",
        succeeded=True,
    )
    waiting = store.mark_waiting(
        delegation["id"],
        expected_version=1,
        waiting_on="verification_recovery",
        needs_user=True,
        reason_code="verification_budget_exhausted",
    )

    assert is_confirmable(store.get(delegation["id"])) is False
    assert is_retryable(store.get(delegation["id"])) is True
    with pytest.raises(DelegationConflict, match="R3 risk confirmation"):
        store.confirm(
            delegation["id"], expected_version=1, principal_id="owner"
        )
    assert waiting["status"] == "needs_user"
    resumed = store.retry(
        delegation["id"], expected_version=1, actor_id="owner"
    )
    assert resumed["status"] == "verifying"
    assert resumed["waiting_on"] == ""
    assert store.get(delegation["id"])["steps"][0]["status"] == "verifying"


def test_active_verification_cannot_retry_external_mutation(tmp_path):
    store = _store(tmp_path)
    delegation, _ = _delegation(store)
    step = _step(store, delegation)
    store.claim_step(
        delegation["id"], step["id"], expected_version=1, owner="worker"
    )
    verifying = store.record_attempt(
        delegation["id"],
        step["id"],
        expected_version=1,
        owner="worker",
        succeeded=True,
        artifact_locator="message:sent",
    )

    assert verifying["status"] == "verifying"
    assert is_retryable(verifying) is False
    with pytest.raises(DelegationConflict, match="not retryable"):
        store.retry(
            delegation["id"],
            expected_version=1,
            actor_id="owner",
        )
    assert store.get(delegation["id"])["steps"][0]["status"] == "verifying"


def test_optional_step_failure_does_not_fail_required_outcome(tmp_path):
    store = _store(tmp_path)
    delegation, _ = _delegation(store)
    required = _step(store, delegation, sequence=1)
    optional = _step(store, delegation, sequence=2, required=False)
    store.claim_step(
        delegation["id"], required["id"], expected_version=1, owner="required"
    )
    store.claim_step(
        delegation["id"], optional["id"], expected_version=1, owner="optional"
    )

    store.record_attempt(
        delegation["id"],
        optional["id"],
        expected_version=1,
        owner="optional",
        succeeded=False,
        error_code="optional_export_failed",
    )
    assert store.get(delegation["id"])["status"] == "executing"

    store.record_attempt(
        delegation["id"],
        required["id"],
        expected_version=1,
        owner="required",
        succeeded=True,
    )
    store.record_evidence(
        delegation["id"],
        required["id"],
        expected_version=1,
        evidence_type="readback",
        strength="strong",
        authority="message_service",
        resource_locator="message:required",
        observed_digest="sha256:" + "9" * 64,
        expected_summary="recipient=agent-1",
        observed_summary="recipient=agent-1",
        matched=True,
        actor_id="message",
    )

    detail = store.get(delegation["id"])
    assert detail["status"] == "completed"
    assert next(
        row for row in detail["steps"] if row["id"] == optional["id"]
    )["status"] == "failed"


def test_optional_step_cannot_overwrite_required_verification_state(tmp_path):
    store = _store(tmp_path)
    delegation, _ = _delegation(store)
    required = _step(store, delegation, sequence=1)
    optional = _step(store, delegation, sequence=2, required=False)
    store.claim_step(
        delegation["id"], required["id"], expected_version=1, owner="required"
    )
    store.record_attempt(
        delegation["id"],
        required["id"],
        expected_version=1,
        owner="required",
        succeeded=True,
    )
    assert store.get(delegation["id"])["status"] == "verifying"

    store.claim_step(
        delegation["id"], optional["id"], expected_version=1, owner="optional"
    )
    assert store.get(delegation["id"])["status"] == "verifying"
    store.record_attempt(
        delegation["id"],
        optional["id"],
        expected_version=1,
        owner="optional",
        succeeded=False,
        error_code="optional_export_failed",
    )

    detail = store.get(delegation["id"])
    assert detail["status"] == "verifying"
    assert next(
        row for row in detail["steps"] if row["id"] == required["id"]
    )["status"] == "verifying"


def test_r4_stays_human_operated_even_when_created_as_authorized(tmp_path):
    store = _store(tmp_path)
    delegation, _ = _delegation(
        store,
        risk_tier=4,
        authorized=True,
        operation="legal_commitment",
    )
    step = _step(store, delegation)

    assert delegation["status"] == "needs_user"
    assert delegation["authorized"] == 0
    with pytest.raises(DelegationConflict, match="human-operated"):
        store.confirm(
            delegation["id"], expected_version=1, principal_id="owner"
        )
    with pytest.raises(DelegationConflict):
        store.claim_step(
            delegation["id"],
            step["id"],
            expected_version=1,
            owner="worker",
        )


def test_r3_contract_revision_requires_fresh_approval(tmp_path):
    store = _store(tmp_path)
    delegation, _ = _delegation(
        store,
        risk_tier=3,
        authorized=True,
        operation="public_publish",
    )

    revised = store.revise_contract(
        delegation["id"],
        expected_version=1,
        target_id="agent-2",
        expected_postcondition={"recipient_id": "agent-2"},
    )
    step = _step(store, revised)

    assert revised["status"] == "needs_user"
    assert revised["authorized"] == 0
    with pytest.raises(DelegationConflict):
        store.claim_step(
            delegation["id"],
            step["id"],
            expected_version=2,
            owner="worker",
        )
    confirmed = store.confirm(
        delegation["id"], expected_version=2, principal_id="owner"
    )
    assert confirmed["status"] == "bound"


def test_unbound_and_shadow_delegations_cannot_reach_workers(tmp_path):
    store = _store(tmp_path)
    unbound, _ = store.create(
        principal_id="owner",
        source="test",
        source_ref="unbound",
        title="Unbound",
        operation="message_send",
        authorized=True,
    )
    step = _step(store, unbound)
    with pytest.raises(DelegationConflict, match="not executable"):
        store.claim_step(
            unbound["id"],
            step["id"],
            expected_version=1,
            owner="worker",
        )

    shadow, _ = store.record_shadow_prediction(
        principal_id="owner",
        source="lark",
        source_ref="shadow-no-step",
        title="Shadow",
        operation="message_send",
        predicted_is_delegation=True,
        predicted_target_risk=2,
        predicted_verifier="lark_message",
    )
    with pytest.raises(DelegationConflict, match="shadow"):
        _step(store, shadow)


def test_dependency_dag_blocks_out_of_order_claim(tmp_path):
    store = _store(tmp_path)
    delegation, _ = _delegation(store, operation="engineering_change")
    first = _step(store, delegation, sequence=1, kind="test")
    second = _step(
        store,
        delegation,
        sequence=2,
        kind="deploy",
        depends_on=[first["id"]],
    )
    with pytest.raises(DelegationConflict):
        store.claim_step(
            delegation["id"], second["id"], expected_version=1, owner="codex"
        )
    _complete_step(store, delegation, first)
    claim = store.claim_step(
        delegation["id"], second["id"], expected_version=1, owner="codex"
    )
    assert claim.step_id == second["id"]


def test_active_lease_prevents_double_claim_and_can_be_renewed(tmp_path):
    clock = [1_000.0]
    store = _store(tmp_path, clock)
    delegation, _ = _delegation(store)
    step = _step(store, delegation)
    claim = store.claim_step(
        delegation["id"],
        step["id"],
        expected_version=1,
        owner="codex",
        lease_seconds=60,
    )
    with pytest.raises(DelegationConflict):
        store.claim_step(
            delegation["id"],
            step["id"],
            expected_version=1,
            owner="claude",
        )
    with pytest.raises(DelegationConflict):
        store.claim_step(
            delegation["id"],
            step["id"],
            expected_version=1,
            owner="codex",
        )
    renewed = store.renew_claim(
        delegation["id"],
        step["id"],
        expected_version=1,
        owner="codex",
        lease_seconds=120,
    )
    assert renewed.lease_expires_at > claim.lease_expires_at


def test_expired_lease_is_released_for_recovery(tmp_path):
    clock = [1_000.0]
    store = _store(tmp_path, clock)
    delegation, _ = _delegation(store)
    step = _step(store, delegation)
    store.claim_step(
        delegation["id"],
        step["id"],
        expected_version=1,
        owner="codex",
        lease_seconds=30,
    )
    clock[0] += 31
    assert store.release_expired_leases() == [step["id"]]
    claim = store.claim_step(
        delegation["id"],
        step["id"],
        expected_version=1,
        owner="claude",
    )
    assert claim.lease_owner == "claude"


def test_concurrent_claim_has_one_winner(tmp_path):
    store = _store(tmp_path)
    delegation, _ = _delegation(store)
    step = _step(store, delegation)
    winners = []
    failures = []

    def claim(owner):
        try:
            winners.append(
                store.claim_step(
                    delegation["id"],
                    step["id"],
                    expected_version=1,
                    owner=owner,
                ).lease_owner
            )
        except DelegationConflict:
            failures.append(owner)

    threads = [threading.Thread(target=claim, args=(owner,)) for owner in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(winners) == 1
    assert len(failures) == 1


def test_secret_like_evidence_metadata_is_rejected(tmp_path):
    store = _store(tmp_path)
    delegation, _ = _delegation(store)
    step = _step(store, delegation)
    with pytest.raises(DelegationError, match="secret"):
        store.record_evidence(
            delegation["id"],
            step["id"],
            expected_version=1,
            evidence_type="readback",
            strength="strong",
            authority="service",
            resource_locator="object:1",
            observed_digest="sha256:" + "e" * 64,
            expected_summary="",
            observed_summary="",
            matched=True,
            metadata={"access_token": "do-not-store"},
        )


def test_terminal_transition_is_idempotent_and_audited(tmp_path):
    store = _store(tmp_path)
    delegation, _ = _delegation(store)
    cancelled = store.terminal(
        delegation["id"],
        expected_version=1,
        status="cancelled",
        reason_code="owner_cancelled",
        actor_id="owner",
    )
    again = store.terminal(
        delegation["id"],
        expected_version=1,
        status="cancelled",
        reason_code="owner_cancelled",
        actor_id="owner",
    )
    assert cancelled["status"] == again["status"] == "cancelled"
    events = store.get(delegation["id"])["events"]
    assert [event["event_type"] for event in events].count(
        "delegation.cancelled"
    ) == 1


def test_shadow_capture_is_hidden_and_life_expression_is_protected(tmp_path):
    store = _store(tmp_path)
    prediction, delegation_id, created = capture(
        text="请你把这个报告发给我的合作伙伴",
        source="lark",
        source_ref="om-1",
        principal_id="owner",
        store=store,
    )
    assert prediction.is_delegation is True
    assert created is True
    assert store.list() == []
    assert store.list(include_shadow=True)[0]["id"] == delegation_id
    assert classify("我觉得今天大腿放松以后挺舒服，可以尝试恢复运动").is_delegation is False


def test_shadow_metrics_enforce_quality_gates(tmp_path):
    store = _store(tmp_path)
    _, delegation_id, _ = capture(
        text="帮我创建一个日历提醒",
        source="lark",
        source_ref="om-2",
        principal_id="owner",
        store=store,
    )
    store.label_shadow(
        delegation_id,
        actual_is_delegation=True,
        actual_target_risk=2,
        actual_verifier="lark_calendar",
    )
    metrics = store.shadow_metrics()
    assert metrics["labeled"] == 1
    assert metrics["precision"] == 1
    assert metrics["verifier_accuracy"] == 1
    assert metrics["observation_days"] == 0
    assert metrics["connector_class_count"] == 1
    assert metrics["phase1_ready"] is False


def test_shadow_gate_requires_two_weeks_and_five_connector_classes(tmp_path):
    operations = [
        "message_send",
        "friend_relationship",
        "calendar_upsert",
        "document_update",
        "git_push",
    ]

    def populate(path, *, spread_days, operation_count):
        clock = [1_000.0]
        store = _store(path, clock)
        for index in range(50):
            clock[0] = 1_000 + (spread_days * 86400 * index / 49)
            operation = operations[index % operation_count]
            row, _ = store.record_shadow_prediction(
                principal_id="owner",
                source="lark",
                source_ref=f"shadow-{index}",
                title="shadow",
                operation=operation,
                predicted_is_delegation=True,
                predicted_target_risk=2,
                predicted_verifier="authoritative",
            )
            store.label_shadow(
                row["id"],
                actual_is_delegation=True,
                actual_target_risk=2,
                actual_verifier="authoritative",
            )
        return store.shadow_metrics()

    one_day = populate(tmp_path / "one-day", spread_days=1, operation_count=5)
    one_connector = populate(
        tmp_path / "one-connector", spread_days=15, operation_count=1
    )
    qualified = populate(
        tmp_path / "qualified", spread_days=15, operation_count=5
    )

    assert one_day["phase1_ready"] is False
    assert one_connector["phase1_ready"] is False
    assert qualified["observation_days"] >= 14
    assert qualified["connector_class_count"] == 5
    assert qualified["phase1_ready"] is True


def test_metrics_expose_user_and_reliability_states(tmp_path):
    store = _store(tmp_path)
    _delegation(store)
    _delegation(
        store,
        source_ref="msg-2",
        risk_tier=3,
        authorized=False,
        operation="public_publish",
    )
    metrics = store.metrics()
    assert metrics["total"] == 2
    assert metrics["by_status"]["needs_user"] == 1
    assert metrics["duplicate_idempotency_keys"] == 0
    assert metrics["attention_asks"] == 1
    assert metrics["wrong_target_actions"] == 0
    assert metrics["duplicate_external_mutations"] == 0


def test_metrics_qualifying_evidence_matches_completion_evaluator(tmp_path):
    clock = [1_000.0]
    store = _store(tmp_path, clock)
    valid, _ = _delegation(store, source_ref="valid")
    valid_step = _step(store, valid, kind="message")
    _complete_step(store, valid, valid_step)

    expired, _ = _delegation(store, source_ref="expired")
    expired_step = _step(store, expired, kind="message")
    store.claim_step(
        expired["id"], expired_step["id"], expected_version=1, owner="worker"
    )
    store.record_attempt(
        expired["id"],
        expired_step["id"],
        expected_version=1,
        owner="worker",
        succeeded=True,
    )
    store.record_evidence(
        expired["id"],
        expired_step["id"],
        expected_version=1,
        evidence_type="readback",
        strength="strong",
        authority="message_service",
        resource_locator="message:expired",
        observed_digest="sha256:" + "b" * 64,
        expected_summary="expected",
        observed_summary="observed",
        matched=True,
        expires_at=999,
        actor_id="message",
    )

    forged, _ = _delegation(store, source_ref="forged")
    forged_step = _step(store, forged, kind="message")
    store.claim_step(
        forged["id"], forged_step["id"], expected_version=1, owner="worker"
    )
    store.record_attempt(
        forged["id"],
        forged_step["id"],
        expected_version=1,
        owner="worker",
        succeeded=True,
    )
    store.record_evidence(
        forged["id"],
        forged_step["id"],
        expected_version=1,
        evidence_type="readback",
        strength="strong",
        authority="message_service",
        resource_locator="message:forged",
        observed_digest="sha256:" + "c" * 64,
        expected_summary="expected",
        observed_summary="observed",
        matched=True,
        actor_id="other-verifier",
    )

    assert store.metrics()["with_qualifying_evidence"] == 1


def test_attention_query_includes_failed_recovery_decisions(tmp_path):
    store = _store(tmp_path)
    failed, _ = _delegation(store)
    needs_user, _ = _delegation(
        store,
        source_ref="msg-2",
        risk_tier=3,
        authorized=False,
        operation="public_publish",
    )
    step = _step(store, failed)
    store.claim_step(
        failed["id"], step["id"], expected_version=1, owner="worker"
    )
    store.record_attempt(
        failed["id"],
        step["id"],
        expected_version=1,
        owner="worker",
        succeeded=False,
        error_code="transport_failed",
    )

    attention = store.list(needs_attention=True)

    assert [item["id"] for item in attention] == [
        needs_user["id"],
        failed["id"],
    ]


def test_metrics_detect_wrong_target_and_duplicate_external_receipts(tmp_path):
    store = _store(tmp_path)
    delegation, _ = _delegation(store)
    step = _step(store, delegation)
    store.claim_step(
        delegation["id"], step["id"], expected_version=1, owner="codex"
    )
    store.record_attempt(
        delegation["id"],
        step["id"],
        expected_version=1,
        owner="codex",
        succeeded=True,
    )
    store.record_evidence(
        delegation["id"],
        step["id"],
        expected_version=1,
        evidence_type="readback",
        strength="strong",
        authority="message_service",
        resource_locator="message:wrong",
        observed_digest="sha256:" + "d" * 64,
        expected_summary='{"recipient_id":"agent-1"}',
        observed_summary='{"recipient_id":"agent-2"}',
        matched=False,
        actor_id="message",
    )
    for index in (1, 2):
        store.record_evidence(
            delegation["id"],
            step["id"],
            expected_version=1,
            evidence_type="readback",
            strength="strong",
            authority="message_service",
            resource_locator=f"message:{index}",
            observed_digest="sha256:" + str(index) * 64,
            expected_summary='{"recipient_id":"agent-1"}',
            observed_summary='{"recipient_id":"agent-1"}',
            matched=True,
            actor_id="message",
        )

    metrics = store.metrics()
    assert metrics["wrong_target_actions"] == 1
    assert metrics["duplicate_external_mutations"] == 1


def test_metrics_exclude_shadow_predictions_from_user_work(tmp_path):
    store = _store(tmp_path)
    store.record_shadow_prediction(
        principal_id="owner",
        source="lark",
        source_ref="shadow-1",
        title="shadow",
        operation="message_send",
        predicted_is_delegation=True,
        predicted_target_risk=1,
        predicted_verifier="lark_message",
    )

    assert store.metrics()["total"] == 0
    assert store.shadow_metrics()["predictions"] == 1


def test_expired_trusted_evidence_cannot_be_replaced_by_untrusted_actor(
    tmp_path,
):
    clock = [1_000.0]
    store = _store(tmp_path, clock)
    delegation, _ = _delegation(store)
    first = _step(store, delegation, sequence=1, kind="send")
    second = _step(store, delegation, sequence=2, kind="confirm")
    for step, owner in ((first, "one"), (second, "two")):
        store.claim_step(
            delegation["id"],
            step["id"],
            expected_version=1,
            owner=owner,
        )
        store.record_attempt(
            delegation["id"],
            step["id"],
            expected_version=1,
            owner=owner,
            succeeded=True,
        )

    def evidence(step, actor, *, expires_at=None):
        return store.record_evidence(
            delegation["id"],
            step["id"],
            expected_version=1,
            evidence_type="readback",
            strength="strong",
            authority="message_service",
            resource_locator=f"message:{actor}",
            observed_digest="sha256:" + "a" * 64,
            expected_summary="expected",
            observed_summary="observed",
            matched=True,
            expires_at=expires_at,
            actor_id=actor,
        )

    evidence(first, "message", expires_at=1_010)
    clock[0] = 1_011
    forged = evidence(first, "other-verifier")
    evidence(second, "message")

    detail = store.get(delegation["id"])
    first_state = next(
        row for row in detail["steps"] if row["id"] == first["id"]
    )
    assert forged["trusted"] == 0
    assert forged["verifier_id"] == "other-verifier"
    assert first_state["status"] == "verifying"
    assert detail["status"] == "verifying"

    evidence(first, "message")
    assert store.get(delegation["id"])["status"] == "completed"


def test_terminal_failure_cannot_erase_post_mutation_recovery_state(tmp_path):
    store = _store(tmp_path)
    delegation, _ = _delegation(store)
    step = _step(store, delegation)
    store.claim_step(
        delegation["id"], step["id"], expected_version=1, owner="worker"
    )
    store.record_attempt(
        delegation["id"],
        step["id"],
        expected_version=1,
        owner="worker",
        succeeded=True,
    )

    with pytest.raises(DelegationConflict, match="post-mutation"):
        store.terminal(
            delegation["id"],
            expected_version=1,
            status="failed",
            reason_code="scheduler_timeout",
        )

    detail = store.get(delegation["id"])
    assert detail["status"] == "verifying"
    assert detail["steps"][0]["status"] == "verifying"


def test_parallel_required_steps_keep_parent_executing(tmp_path):
    clock = [1_000.0]
    store = _store(tmp_path, clock)
    delegation, _ = _delegation(store)
    short = _step(store, delegation, sequence=1)
    long = _step(store, delegation, sequence=2)
    store.claim_step(
        delegation["id"],
        short["id"],
        expected_version=1,
        owner="short",
        lease_seconds=30,
    )
    store.claim_step(
        delegation["id"],
        long["id"],
        expected_version=1,
        owner="long",
        lease_seconds=300,
    )
    store.record_attempt(
        delegation["id"],
        short["id"],
        expected_version=1,
        owner="short",
        succeeded=True,
    )
    assert store.get(delegation["id"])["status"] == "executing"

    other, _ = _delegation(store, source_ref="parallel-expiry")
    expired = _step(store, other, sequence=1)
    active = _step(store, other, sequence=2)
    store.claim_step(
        other["id"], expired["id"], expected_version=1,
        owner="expired", lease_seconds=30,
    )
    store.claim_step(
        other["id"], active["id"], expected_version=1,
        owner="active", lease_seconds=300,
    )
    clock[0] += 31
    assert store.release_expired_leases() == [expired["id"]]
    assert store.get(other["id"])["status"] == "executing"


def test_negative_labels_do_not_count_as_connector_coverage(tmp_path):
    """A declined classification is not a validated connector (2026-07-27).

    `connector_classes` used to be derived from every labeled row, so the two
    non-connector classifier outcomes — `discussion` and `life_expression` —
    each counted toward the five-connector promotion gate. Phase 0 could then
    graduate having exercised only three real connectors.
    """
    store = _store(tmp_path)

    def labeled(operation, *, is_delegation):
        row, _ = store.record_shadow_prediction(
            principal_id="owner",
            source="lark",
            source_ref=f"shadow-{operation}-{is_delegation}",
            title="shadow",
            operation=operation,
            predicted_is_delegation=is_delegation,
            predicted_target_risk=2 if is_delegation else 0,
            predicted_verifier="authoritative" if is_delegation else "",
        )
        store.label_shadow(
            row["id"],
            actual_is_delegation=is_delegation,
            actual_target_risk=2 if is_delegation else 0,
            actual_verifier="authoritative" if is_delegation else "",
        )

    for operation in ("message_send", "git_push", "calendar_upsert"):
        labeled(operation, is_delegation=True)
    for operation in ("discussion", "life_expression"):
        labeled(operation, is_delegation=False)

    metrics = store.shadow_metrics()
    assert metrics["labeled"] == 5
    assert metrics["connector_classes"] == [
        "calendar_upsert", "git_push", "message_send"]
    assert metrics["connector_class_count"] == 3, (
        "declining to classify chit-chat must not count as connector coverage")
