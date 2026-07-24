from __future__ import annotations

import json

from core.delegation_reconcile import (
    DelegationReconciler,
    main,
    sync_attention_item,
)
from core.delegation_verify import Verification, VerificationError
from core.delegations import DelegationStore


def _prepared(tmp_path, clock):
    store = DelegationStore(
        root=tmp_path,
        db_path=tmp_path / "jarvis.db",
        now=lambda: clock[0],
    )
    delegation, _ = store.create(
        principal_id="owner",
        source="test",
        source_ref="reconcile-1",
        title="核验外部动作",
        operation="message_send",
        target_type="agent",
        target_id="agent-1",
        target_label="Agent 1",
        expected_postcondition={"state": "sent"},
        authority="message_service",
        verification_policy={
            "verifier": "test_readback",
            "verification_timeout_seconds": 60,
        },
        authorized=True,
    )
    step = store.add_step(
        delegation["id"],
        expected_version=1,
        sequence=1,
        kind="test_readback",
        executor="worker",
    )
    store.claim_step(
        delegation["id"],
        step["id"],
        expected_version=1,
        owner="worker",
        lease_seconds=120,
    )
    store.record_attempt(
        delegation["id"],
        step["id"],
        expected_version=1,
        owner="worker",
        succeeded=True,
        artifact_locator="message:1",
    )
    return store, delegation, step


class _Registry:
    def __init__(self, *, matched=True, error=""):
        self.matched = matched
        self.error = error
        self.calls = 0

    def verify(self, verifier, expected, policy):
        self.calls += 1
        if self.error:
            raise VerificationError(self.error)
        return Verification(
            matched=self.matched,
            authority="message_service",
            resource_locator="message:1",
            evidence_type="authoritative_readback",
            strength="strong",
            expected_summary='{"state":"sent"}',
            observed_summary=(
                '{"state":"sent"}' if self.matched else '{"state":"missing"}'
            ),
            observed_digest="sha256:" + "a" * 64,
            metadata={},
        )


def test_reconciler_completes_only_after_authoritative_match(tmp_path):
    clock = [1_000.0]
    store, delegation, _ = _prepared(tmp_path, clock)
    registry = _Registry(matched=True)

    result = DelegationReconciler(
        store=store, registry=registry, now=lambda: clock[0]
    ).run(send_items=False)

    assert result["verified"] == 1
    assert result["errors"] == []
    assert store.get(delegation["id"])["status"] == "completed"
    assert registry.calls == 1


def test_reconciler_mismatch_stays_unfinished(tmp_path):
    clock = [1_000.0]
    store, delegation, _ = _prepared(tmp_path, clock)

    result = DelegationReconciler(
        store=store,
        registry=_Registry(matched=False),
        now=lambda: clock[0],
    ).run(send_items=False)

    assert result["deferred"] == 1
    assert store.get(delegation["id"])["status"] == "verifying"


def test_reconciler_timeout_escalates_once_to_user(
    tmp_path, monkeypatch
):
    from core import memorial

    clock = [1_000.0]
    store, delegation, _ = _prepared(tmp_path, clock)
    monkeypatch.setattr(memorial, "JARVIS_DIR", tmp_path)
    clock[0] += 61
    registry = _Registry(error="authority unavailable")
    reconciler = DelegationReconciler(
        store=store, registry=registry, now=lambda: clock[0]
    )

    first = reconciler.run(send_items=False)
    second = reconciler.run(send_items=False)
    detail = store.get(delegation["id"])

    assert first["needs_user"] == 1
    assert detail["status"] == "needs_user"
    attention = [
        link
        for link in detail["links"]
        if link["entity_type"] == "memorial"
        and link["relation"] == "needs_attention"
    ]
    assert len(attention) == 1
    item = memorial.get_memorial(attention[0]["entity_id"])
    assert item["title"] == "需要你 · 恢复核验"
    assert [option["label"] for option in item["options"]] == [
        "重新核验",
        "取消委托",
    ]
    assert [option["action"]["type"] for option in item["options"]] == [
        "delegation_retry",
        "delegation_cancel",
    ]
    assert second["needs_user"] == 1
    assert len(
        [
            link
            for link in store.get(delegation["id"])["links"]
            if link["relation"] == "needs_attention"
        ]
    ) == 1


def test_repeated_mismatch_does_not_reset_verification_budget(
    tmp_path, monkeypatch,
):
    from core import memorial

    clock = [1_000.0]
    store, delegation, _ = _prepared(tmp_path, clock)
    monkeypatch.setattr(memorial, "JARVIS_DIR", tmp_path)
    reconciler = DelegationReconciler(
        store=store,
        registry=_Registry(matched=False),
        now=lambda: clock[0],
    )

    reconciler.run(send_items=False)
    clock[0] += 30
    reconciler.run(send_items=False)
    clock[0] += 31
    result = reconciler.run(send_items=False)

    assert result["needs_user"] == 1
    assert store.get(delegation["id"])["status"] == "needs_user"


def test_reconciler_refreshes_bound_taskline_delegation(
    tmp_path, monkeypatch,
):
    store = DelegationStore(
        root=tmp_path,
        db_path=tmp_path / "jarvis.db",
    )
    delegation, _ = store.create(
        principal_id="owner",
        source="taskline",
        source_ref="task-bound",
        title="Deploy task",
        operation="engineering_change",
        target_type="repository",
        target_id="jarvis",
        authority="jarvis_runtime",
        verification_policy={
            "verifier": "runtime_deploy",
            "release_sha": "",
        },
        expected_postcondition={"release_sha": "pending:task-bound"},
        authorized=True,
    )
    store.add_step(
        delegation["id"],
        expected_version=1,
        sequence=1,
        kind="runtime_deploy",
        executor="release",
    )
    calls = []

    def refresh(bridge, task_id):
        calls.append((task_id, bridge.db_path))
        return store.get(delegation["id"])

    monkeypatch.setattr(
        "core.taskline_bridge.TasklineBridge.refresh_release", refresh
    )

    DelegationReconciler(store=store).run(send_items=False)

    assert calls == [("task-bound", store.db_path)]


def test_reconciler_starts_pending_taskline_release_step(
    tmp_path, monkeypatch,
):
    release_sha = "a" * 40
    store = DelegationStore(
        root=tmp_path,
        db_path=tmp_path / "jarvis.db",
    )
    delegation, _ = store.create(
        principal_id="owner",
        source="taskline",
        source_ref="task-with-release",
        title="Deploy completed task",
        operation="engineering_change",
        target_type="repository",
        target_id="jarvis",
        authority="jarvis_runtime",
        verification_policy={
            "verifier": "runtime_deploy",
            "release_sha": release_sha,
        },
        expected_postcondition={
            "release_sha": release_sha,
            "runtime_ok": True,
            "components_ok": True,
        },
        authorized=True,
    )
    store.add_step(
        delegation["id"],
        expected_version=1,
        sequence=1,
        kind="runtime_deploy",
        executor="release",
    )
    calls = []

    def refresh(_bridge, task_id):
        calls.append(task_id)
        detail = store.get(delegation["id"])
        step = detail["steps"][0]
        store.claim_step(
            delegation["id"],
            step["id"],
            expected_version=1,
            owner="taskline-release",
        )
        store.record_attempt(
            delegation["id"],
            step["id"],
            expected_version=1,
            owner="taskline-release",
            succeeded=True,
            artifact_locator="https://github.com/example/repo/pull/1",
        )
        return store.get(delegation["id"])

    monkeypatch.setattr(
        "core.taskline_bridge.TasklineBridge.refresh_release", refresh
    )

    DelegationReconciler(
        store=store,
        registry=_Registry(matched=False),
    ).run(send_items=False)

    assert calls == ["task-with-release"]
    assert store.get(delegation["id"])["steps"][0]["status"] == "verifying"


def test_failed_delegation_surfaces_one_retry_attention_item(
    tmp_path, monkeypatch,
):
    from core import memorial

    monkeypatch.setattr(memorial, "JARVIS_DIR", tmp_path)
    store = DelegationStore(root=tmp_path, db_path=tmp_path / "jarvis.db")
    delegation, _ = store.create(
        principal_id="owner",
        source="test",
        source_ref="failed-attention",
        title="发送跨 Agent 消息",
        operation="message_send",
        target_type="agent",
        target_id="agent-1",
        authority="message_service",
        verification_policy={"verifier": "message"},
        authorized=True,
    )
    step = store.add_step(
        delegation["id"],
        expected_version=1,
        sequence=1,
        kind="message_send",
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
        error_code="provider_unavailable",
    )
    reconciler = DelegationReconciler(store=store)

    first = reconciler.run(send_items=False)
    second = reconciler.run(send_items=False)
    detail = store.get(delegation["id"])

    assert first["needs_user"] == 1
    assert second["needs_user"] == 1
    attention = [
        link
        for link in detail["links"]
        if link["entity_type"] == "memorial"
        and link["relation"] == "needs_attention"
    ]
    assert len(attention) == 1
    item = memorial.get_memorial(attention[0]["entity_id"])
    assert item["title"] == "需要你 · 委托失败"
    assert [option["label"] for option in item["options"]] == [
        "重试执行",
        "取消委托",
    ]


def test_reconciler_releases_only_expired_active_lease(tmp_path):
    clock = [1_000.0]
    store = DelegationStore(
        root=tmp_path,
        db_path=tmp_path / "jarvis.db",
        now=lambda: clock[0],
    )
    delegation, _ = store.create(
        principal_id="owner",
        source="test",
        source_ref="lease-1",
        title="租约恢复",
        operation="code_change",
        target_type="repo",
        target_id="jarvis",
        expected_postcondition={"tests": "pass"},
        authority="ci",
        verification_policy={"verifier": "git_commit"},
        authorized=True,
    )
    step = store.add_step(
        delegation["id"],
        expected_version=1,
        sequence=1,
        kind="git_commit",
        executor="codex",
    )
    store.claim_step(
        delegation["id"],
        step["id"],
        expected_version=1,
        owner="codex",
        lease_seconds=30,
    )
    clock[0] += 31

    result = DelegationReconciler(
        store=store, registry=_Registry(), now=lambda: clock[0]
    ).run(send_items=False)

    assert result["released_leases"] == [step["id"]]
    detail = store.get(delegation["id"])
    assert detail["status"] == "bound"
    assert detail["steps"][0]["status"] == "pending"


def test_reconciler_prioritizes_user_attention_before_verification(
    tmp_path, monkeypatch
):
    store = DelegationStore(root=tmp_path, db_path=tmp_path / "jarvis.db")
    attention, _ = store.create(
        principal_id="owner",
        source="test",
        source_ref="attention-first",
        title="需要用户确认",
        operation="message_send",
        risk_tier=3,
        target_type="agent",
        target_id="agent-1",
        authority="message_service",
        verification_policy={"verifier": "test_readback"},
    )
    called = []
    monkeypatch.setattr(
        "core.delegation_reconcile.sync_attention_item",
        lambda detail, **_kwargs: called.append(detail["id"]) or "item-1",
    )
    registry = _Registry()

    result = DelegationReconciler(store=store, registry=registry).run(
        limit=1, send_items=False
    )

    assert result["scanned"] == 1
    assert result["needs_user"] == 1
    assert called == [attention["id"]]
    assert registry.calls == 0


def test_contract_revision_replaces_stale_attention_item(
    tmp_path, monkeypatch,
):
    from core import memorial

    monkeypatch.setattr(memorial, "JARVIS_DIR", tmp_path)
    store = DelegationStore(root=tmp_path, db_path=tmp_path / "jarvis.db")
    delegation, _ = store.create(
        principal_id="owner",
        source="test",
        source_ref="attention-revision",
        title="需要确认",
        operation="message_send",
        risk_tier=3,
        target_type="agent",
        target_id="agent-1",
        target_label="Agent 1",
        expected_postcondition={"target_id": "agent-1"},
        authority="message_service",
        verification_policy={"verifier": "message"},
    )
    first_id = sync_attention_item(
        store.get(delegation["id"]), store=store, send=False
    )

    revised = store.revise_contract(
        delegation["id"],
        expected_version=1,
        target_id="agent-2",
        expected_postcondition={"target_id": "agent-2"},
    )
    second_id = sync_attention_item(
        store.get(delegation["id"]), store=store, send=False
    )

    assert second_id != first_id
    assert memorial.get_memorial(first_id)["resolved_label"] == "已失效"
    current = memorial.get_memorial(second_id)
    assert current["status"] == "pending"
    assert json.loads(current["context"])["contract_version"] == 2
    assert sync_attention_item(
        store.get(revised["id"]), store=store, send=False
    ) == second_id


def test_reconciler_recovers_interrupted_external_completion(tmp_path):
    clock = [1_000.0]
    store, delegation, step = _prepared(tmp_path, clock)
    store.mark_waiting(
        delegation["id"],
        expected_version=1,
        waiting_on="message:1",
    )
    with store._tx() as db:
        db.execute(
            """
            UPDATE delegation_steps
               SET status='completed',finished_at=?,updated_at=?
             WHERE id=?
            """,
            (clock[0], clock[0], step["id"]),
        )
        db.execute(
            """
            INSERT INTO delegation_evidence(
                id,delegation_id,step_id,contract_version,evidence_type,
                strength,authority,resource_locator,observed_digest,
                expected_summary,observed_summary,matched,observed_at,
                expires_at,privacy_class,trusted,verifier_id,metadata_json
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "dev_interrupted",
                delegation["id"],
                step["id"],
                1,
                "authoritative_readback",
                "strong",
                "message_service",
                "message:1",
                "sha256:" + "a" * 64,
                '{"state":"sent"}',
                '{"state":"sent"}',
                1,
                clock[0],
                None,
                "private",
                1,
                "test_readback",
                "{}",
            ),
        )

    result = DelegationReconciler(
        store=store, registry=_Registry(), now=lambda: clock[0]
    ).run(send_items=False)

    detail = store.get(delegation["id"])
    assert result["verified"] == 1
    assert detail["status"] == "completed"
    assert detail["waiting_on"] == ""


def test_attention_backlog_does_not_starve_verification(tmp_path, monkeypatch):
    clock = [1_000.0]
    store, delegation, _ = _prepared(tmp_path, clock)
    for index in range(3):
        store.create(
            principal_id="owner",
            source="test",
            source_ref=f"attention-{index}",
            title=f"需要用户确认 {index}",
            operation="message_send",
            risk_tier=3,
            target_type="agent",
            target_id=f"agent-{index}",
            authority="message_service",
            verification_policy={"verifier": "test_readback"},
        )
    monkeypatch.setattr(
        "core.delegation_reconcile.sync_attention_item",
        lambda *_args, **_kwargs: "item",
    )

    result = DelegationReconciler(
        store=store, registry=_Registry(), now=lambda: clock[0]
    ).run(limit=2, send_items=False)

    assert result["scanned"] == 2
    assert result["needs_user"] == 1
    assert result["verified"] == 1
    assert store.get(delegation["id"])["status"] == "completed"


def test_cli_item_readback_errors_do_not_fail_the_global_scheduler(
    monkeypatch, capsys,
):
    monkeypatch.setattr(
        "core.delegation_reconcile.DelegationReconciler.run",
        lambda _self, **_kwargs: {
            "errors": [{"delegation_id": "one", "error": "offline"}],
        },
    )

    assert main(["--no-send"]) == 0
    assert "offline" in capsys.readouterr().out
