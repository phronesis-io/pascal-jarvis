from __future__ import annotations

from datetime import datetime, timedelta, timezone

import dashboard.db as db_module
import pytest

from core import intentions
from core.continuity import create_handoff, get_handoff
from core.delegations import DelegationStore
from core.matters import (
    create_matter,
    get_matter,
    open_followups,
    unlink_entity,
)


def _future_trigger() -> dict:
    """A date trigger that is always in the future.

    These tests assert that a linked intent stays `pending` — a claim about
    projection, not about time. A hard-coded calendar date silently converts
    that into a claim about the wall clock: on 2026-08-02 the literal
    "2026-08-01T10:00:00+08:00" used here went into the past, intentions
    auto-expired it, and `test_duplicate_receipt_reopens_legacy_terminal_
    projections` began failing `assert 'expired' == 'pending'` on every
    branch — turning the repo's required `test` check red and blocking every
    merge. The date must be relative or the test rots again.
    """
    when = datetime.now(timezone(timedelta(hours=8))) + timedelta(days=30)
    return {"datetime": when.replace(microsecond=0).isoformat()}


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    path = tmp_path / "jarvis.db"
    monkeypatch.setenv("JARVIS_DB_PATH", str(path))
    monkeypatch.setenv("JARVIS_DIR", str(tmp_path))
    monkeypatch.setattr(db_module, "DB_PATH", path)
    if db_module._connection is not None:
        db_module._connection.close()
    db_module._connection = None
    intentions._table_ready = False
    yield
    if db_module._connection is not None:
        db_module._connection.close()
    db_module._connection = None
    intentions._table_ready = False


def _delegation(store, matter_id):
    delegation, _ = store.create(
        principal_id="owner",
        source="test",
        source_ref="projection-1",
        title="把报告发给目标 Agent",
        operation="message_send",
        matter_id=matter_id,
        target_type="agent",
        target_id="agent-1",
        target_label="Target Agent",
        expected_postcondition={"receiver_id": "agent-1"},
        authority="message_service",
        verification_policy={"verifier": "message"},
        authorized=True,
    )
    return delegation


def test_matter_next_action_follows_authoritative_delegation(tmp_path):
    matter = create_matter("跨端报告", next_action="人工旧值")
    store = DelegationStore(
        root=tmp_path,
        db_path=tmp_path / "jarvis.db",
        now=lambda: 1_000,
    )
    delegation = _delegation(store, matter["id"])
    linked = get_matter(matter["id"])
    assert linked["next_action"] == "等待执行：把报告发给目标 Agent"
    assert any(
        link["entity_type"] == "delegation"
        and link["entity_id"] == delegation["id"]
        for link in linked["links"]
    )
    assert open_followups(matter["id"])[0]["entity_id"] == delegation["id"]

    step = store.add_step(
        delegation["id"],
        expected_version=1,
        sequence=1,
        kind="send",
        executor="worker",
    )
    store.claim_step(
        delegation["id"],
        step["id"],
        expected_version=1,
        owner="worker",
    )
    assert get_matter(matter["id"])["next_action"].startswith("正在推进")
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
        evidence_type="readback",
        strength="strong",
        authority="message_service",
        resource_locator="message:1",
        observed_digest="sha256:" + "a" * 64,
        expected_summary='{"receiver_id":"agent-1"}',
        observed_summary='{"receiver_id":"agent-1"}',
        matched=True,
        actor_id="message",
    )

    finished = get_matter(matter["id"])
    assert finished["next_action"].startswith("已核验完成")
    assert open_followups(matter["id"]) == []


def test_terminal_delegation_closes_linked_intent_and_handoff(tmp_path):
    matter = create_matter("撤销委托")
    store = DelegationStore(
        root=tmp_path,
        db_path=tmp_path / "jarvis.db",
        now=lambda: 2_000,
    )
    delegation = _delegation(store, matter["id"])
    intent_id = intentions.create_intent(
        name="等待委托结果",
        trigger_type="date",
        trigger_config=_future_trigger(),
        matter_id=matter["id"],
    )
    store.link(delegation["id"], "intent", intent_id)
    handoff = create_handoff(
        "delegation",
        delegation["id"],
        from_surface="desktop",
        to_surface="mobile",
    )

    store.terminal(
        delegation["id"],
        expected_version=1,
        status="cancelled",
        reason_code="owner_cancelled",
        actor_id="owner",
    )

    assert intentions.get_intent(intent_id)["status"] == "cancelled"
    assert get_handoff(handoff["id"])["status"] == "completed"


def test_failed_attempt_keeps_linked_intent_and_handoff_open(tmp_path):
    matter = create_matter("失败后重试")
    store = DelegationStore(
        root=tmp_path,
        db_path=tmp_path / "jarvis.db",
        now=lambda: 2_000,
    )
    delegation = _delegation(store, matter["id"])
    intent_id = intentions.create_intent(
        name="等待重试结果",
        trigger_type="date",
        trigger_config=_future_trigger(),
        matter_id=matter["id"],
    )
    store.link(delegation["id"], "intent", intent_id)
    handoff = create_handoff(
        "delegation",
        delegation["id"],
        from_surface="desktop",
        to_surface="mobile",
    )
    step = store.add_step(
        delegation["id"],
        expected_version=1,
        sequence=1,
        kind="send",
        executor="worker",
    )
    store.claim_step(
        delegation["id"],
        step["id"],
        expected_version=1,
        owner="worker",
    )
    store.record_attempt(
        delegation["id"],
        step["id"],
        expected_version=1,
        owner="worker",
        succeeded=False,
        error_code="temporary_failure",
    )

    assert store.get(delegation["id"])["status"] == "failed"
    assert intentions.get_intent(intent_id)["status"] == "pending"
    assert get_handoff(handoff["id"])["status"] == "open"


def test_idempotent_create_repairs_matter_projection(tmp_path):
    matter = create_matter("投影修复")
    store = DelegationStore(
        root=tmp_path,
        db_path=tmp_path / "jarvis.db",
        now=lambda: 3_000,
    )
    delegation = _delegation(store, matter["id"])
    linked = get_matter(matter["id"])
    link_id = next(
        link["id"]
        for link in linked["links"]
        if link["entity_type"] == "delegation"
        and link["entity_id"] == delegation["id"]
    )
    unlink_entity(
        matter["id"],
        link_id,
        actor="test",
    )

    replay = _delegation(store, matter["id"])

    assert replay["id"] == delegation["id"]
    repaired = get_matter(matter["id"])
    assert any(
        link["entity_type"] == "delegation"
        and link["entity_id"] == delegation["id"]
        for link in repaired["links"]
    )


def test_terminal_projection_failure_is_durably_retried(
    tmp_path, monkeypatch,
):
    import core.delegation_projection as projection_module
    from core.delegation_reconcile import DelegationReconciler

    matter = create_matter("终态投影重试")
    store = DelegationStore(
        root=tmp_path,
        db_path=tmp_path / "jarvis.db",
        now=lambda: 4_000,
    )
    delegation = _delegation(store, matter["id"])
    intent_id = intentions.create_intent(
        name="等待终态",
        trigger_type="date",
        trigger_config=_future_trigger(),
        matter_id=matter["id"],
    )
    store.link(delegation["id"], "intent", intent_id)
    handoff = create_handoff(
        "delegation",
        delegation["id"],
        from_surface="desktop",
        to_surface="mobile",
    )
    real_sync = projection_module.sync_projection
    monkeypatch.setattr(
        projection_module,
        "sync_projection",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("projection unavailable")
        ),
    )

    store.terminal(
        delegation["id"],
        expected_version=1,
        status="cancelled",
        reason_code="owner_cancelled",
        actor_id="owner",
    )

    assert store.get(delegation["id"])["status"] == "cancelled"
    assert store.pending_projections()[0]["delegation_id"] == delegation["id"]
    assert intentions.get_intent(intent_id)["status"] == "pending"
    assert get_handoff(handoff["id"])["status"] == "open"

    monkeypatch.setattr(projection_module, "sync_projection", real_sync)
    result = DelegationReconciler(store=store).run(send_items=False)

    assert result["projections_repaired"] == 1
    assert result["projection_errors"] == []
    assert store.pending_projections() == []
    assert intentions.get_intent(intent_id)["status"] == "cancelled"
    assert get_handoff(handoff["id"])["status"] == "completed"


def test_duplicate_receipt_reopens_legacy_terminal_projections(tmp_path):
    from core.eigenflux_messages import EigenFluxMessenger

    store = DelegationStore(
        root=tmp_path,
        db_path=tmp_path / "jarvis.db",
    )
    delegation, _ = store.create(
        principal_id="owner",
        source="eigenflux-message",
        source_ref="attempt:legacy-action",
        title="发送消息给 Family Agent",
        operation="message_send",
        target_type="agent",
        target_id="agent-spouse",
        target_label="Family Agent",
        expected_postcondition={
            "state": "verified",
            "target_id": "agent-spouse",
        },
        authority="eigenflux_message_history",
        verification_policy={
            "verifier": "eigenflux_message",
            "idempotency_key": "legacy-action",
            "msg_id": "duplicate-message",
        },
        authorized=True,
    )
    step = store.add_step(
        delegation["id"],
        expected_version=1,
        sequence=1,
        kind="eigenflux_message",
        executor="eigenflux-message",
    )
    intent_id = intentions.create_intent(
        name="等待消息送达",
        trigger_type="date",
        trigger_config=_future_trigger(),
    )
    store.link(delegation["id"], "intent", intent_id)
    handoff = create_handoff(
        "delegation",
        delegation["id"],
        from_surface="desktop",
        to_surface="mobile",
    )
    store.claim_step(
        delegation["id"],
        step["id"],
        expected_version=1,
        owner="eigenflux-message",
    )
    store.record_attempt(
        delegation["id"],
        step["id"],
        expected_version=1,
        owner="eigenflux-message",
        succeeded=True,
        artifact_locator="eigenflux-message:duplicate-message",
    )
    store.record_evidence(
        delegation["id"],
        step["id"],
        expected_version=1,
        evidence_type="connector_readback",
        strength="strong",
        authority="eigenflux_message_history",
        resource_locator="eigenflux-message:duplicate-message",
        observed_digest="sha256:" + "a" * 64,
        expected_summary='{"state":"verified"}',
        observed_summary='{"state":"verified"}',
        matched=True,
        actor_id="eigenflux_message",
    )
    assert intentions.get_intent(intent_id)["status"] == "cancelled"
    assert get_handoff(handoff["id"])["status"] == "completed"

    with store._tx() as db:
        db.execute(
            """
            CREATE TABLE verified_external_actions (
                idempotency_key TEXT PRIMARY KEY,
                action_type TEXT NOT NULL,
                target_id TEXT NOT NULL,
                target_name TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                contract_version TEXT NOT NULL DEFAULT 'v1',
                state TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                conv_id TEXT NOT NULL DEFAULT '',
                msg_id TEXT NOT NULL DEFAULT '',
                created_epoch REAL NOT NULL,
                updated_epoch REAL NOT NULL,
                last_error TEXT NOT NULL DEFAULT ''
            )
            """
        )
        db.execute(
            """
            INSERT INTO verified_external_actions VALUES
            (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "legacy-action", "eigenflux_message", "agent-spouse",
                "Family Agent", "payload-hash", "v1", "verifying", 1,
                "", "", 1.0, 1.0,
                "duplicate receipt claim released",
            ),
        )
        db.execute(
            "UPDATE intentions SET cancel_source='',"
            "cancel_sources='[]',cancel_previous_status='',"
            "cancel_previous_error='',"
            "cancel_previous_closure_status='' WHERE id=?",
            (intent_id,),
        )
        db.execute(
            "UPDATE surface_handoffs SET metadata='{}' WHERE id=?",
            (handoff["id"],),
        )

    EigenFluxMessenger(
        root=tmp_path,
        db_path=tmp_path / "jarvis.db",
        runner=lambda *_args, **_kwargs: None,
    )._connect().close()

    assert store.get(delegation["id"])["status"] == "verifying"
    assert intentions.get_intent(intent_id)["status"] == "pending"
    assert get_handoff(handoff["id"])["status"] == "open"
