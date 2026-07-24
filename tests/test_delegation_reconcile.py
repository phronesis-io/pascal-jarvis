from __future__ import annotations

from core.delegation_reconcile import DelegationReconciler
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
    assert second["needs_user"] == 1
    assert len(
        [
            link
            for link in store.get(delegation["id"])["links"]
            if link["relation"] == "needs_attention"
        ]
    ) == 1


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
