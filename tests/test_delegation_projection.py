from __future__ import annotations

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
        trigger_config={"datetime": "2026-08-01T10:00:00+08:00"},
        matter_id=matter["id"],
    )
    store.link(delegation["id"], "intent", intent_id)
    handoff = create_handoff(
        "delegation",
        delegation["id"],
        from_surface="desktop",
        to_surface="mobile",
        notify=False,
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
        trigger_config={"datetime": "2026-08-01T10:00:00+08:00"},
        matter_id=matter["id"],
    )
    store.link(delegation["id"], "intent", intent_id)
    handoff = create_handoff(
        "delegation",
        delegation["id"],
        from_surface="desktop",
        to_surface="mobile",
        notify=False,
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
        trigger_config={"datetime": "2026-08-01T10:00:00+08:00"},
        matter_id=matter["id"],
    )
    store.link(delegation["id"], "intent", intent_id)
    handoff = create_handoff(
        "delegation",
        delegation["id"],
        from_surface="desktop",
        to_surface="mobile",
        notify=False,
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
