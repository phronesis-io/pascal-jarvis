"""Protocol tests for provider-neutral Matter execution handoffs."""

from __future__ import annotations

import json
import stat
import subprocess
import threading
from pathlib import Path

import pytest

import core.db as db_module
from core.conversation_context import clear_derived_context
from core.delegations import DelegationStore
from core.matter_context import build_context_bundle, write_context_bundle
from core.matter_run_audit import audit_matter_runs
from core.matter_runs import (
    MatterRunConflict,
    MatterRunValidationError,
    abort_run,
    acquire_run,
    bind_context_packet,
    get_run,
    mark_run_running,
    release_run,
    renew_run,
)
from core.matters import add_event, create_matter, get_matter


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "jarvis.db")
    if db_module._connection is not None:
        db_module._connection.close()
    db_module._connection = None
    yield
    if db_module._connection is not None:
        db_module._connection.close()
    db_module._connection = None


def _bound_run(tmp_path: Path, *, now: float = 100.0) -> tuple[dict, dict]:
    matter = create_matter(
        "跨执行器实现",
        summary="统一上下文和结果回证",
        next_action="完成协议实现",
    )
    run = acquire_run(
        matter["id"],
        executor="codex",
        task="实现并测试",
        workspace=tmp_path,
        lease_seconds=300,
        now=now,
    )
    bundle = build_context_bundle(matter["id"], run=run)
    context_path = write_context_bundle(
        matter["id"], output=tmp_path / "context.md", run=run
    )
    bind_context_packet(
        run["id"],
        packet_id=bundle["packet_id"],
        context_digest=bundle["digest"],
        context_path=context_path,
        now=now + 1,
    )
    return run, bundle


def test_context_packet_v2_is_bounded_traceable_and_drops_raw_decision_payload(
    tmp_path,
):
    matter = create_matter("隐私交接", summary="只交接当前共识")
    decision = add_event(
        matter["id"],
        "owner_decision",
        "采用 Codex 前台",
        actor="owner",
        payload={"private_body": "never-forward-this", "token": "secret"},
    )
    run = acquire_run(
        matter["id"], executor="codex", workspace=tmp_path, now=100.0
    )

    bundle = build_context_bundle(matter["id"], run=run)
    encoded = json.dumps(bundle, ensure_ascii=False)

    assert bundle["schema"] == "jarvis.context-packet.v2"
    assert bundle["packet_id"].startswith("ctx_")
    assert bundle["digest"].startswith("sha256:")
    assert bundle["context_generation"] == 0
    assert bundle["run"]["id"] == run["id"]
    assert bundle["authority"]["may_complete_matter"] is False
    assert bundle["receipt_contract"]["external_effects"] == (
        "authoritative_evidence_reference_required"
    )
    assert bundle["confirmed_decisions"][0]["source_ref"] == (
        f"matter_event:{decision['id']}"
    )
    assert "never-forward-this" not in encoded
    assert '"token"' not in encoded


def test_context_packet_files_are_private_and_match_the_bound_digest(tmp_path):
    run, bundle = _bound_run(tmp_path)
    output = tmp_path / "private" / "context.md"

    path = write_context_bundle(run["matter_id"], output=output, run=run)
    stored = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))

    assert stored["digest"] == bundle["digest"]
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.with_suffix(".json").stat().st_mode) == 0o600


def test_abort_always_releases_the_lease_with_a_system_receipt(tmp_path):
    run, _ = _bound_run(tmp_path)

    aborted = abort_run(run["id"], error="provider_process_failed:OSError", now=102.0)

    assert aborted["status"] == "failed"
    assert aborted["receipt"]["execution"]["outcome"] == "aborted"
    assert aborted["receipt"]["narrative_trust"] == "system_observation"
    assert aborted["receipt"]["matter_completed"] is False
    with pytest.raises(MatterRunConflict, match="terminal"):
        mark_run_running(run["id"], now=103.0)


def test_phase_zero_audit_separates_legacy_advice_from_current_invariants(tmp_path):
    matter = create_matter("旧会话记录", next_action="人工复核")
    add_event(
        matter["id"], "work_session_completed", "旧模型说做完了",
        payload={"provider": "claude"},
    )

    advisory = audit_matter_runs(now=100.0)
    assert advisory["legacy_unreceipted_session_events"] == 1
    assert advisory["healthy"] is True

    acquire_run(
        matter["id"], executor="codex", workspace=tmp_path,
        lease_seconds=30, now=100.0,
    )
    unhealthy = audit_matter_runs(now=131.0)
    assert unhealthy["stale_active_leases"] == 1
    assert unhealthy["healthy"] is False


def test_acquire_is_atomic_and_an_expired_lease_is_recovered(tmp_path):
    matter = create_matter("原子租约")
    first = acquire_run(
        matter["id"], executor="codex", workspace=tmp_path,
        lease_seconds=30, now=100.0,
    )

    with pytest.raises(MatterRunConflict, match="active run"):
        acquire_run(
            matter["id"], executor="claude", workspace=tmp_path,
            lease_seconds=30, now=110.0,
        )

    second = acquire_run(
        matter["id"], executor="claude", workspace=tmp_path,
        lease_seconds=30, now=131.0,
    )

    assert second["id"] != first["id"]
    assert second["run_sequence"] == first["run_sequence"] + 1
    assert second["context_generation"] == first["context_generation"]
    assert get_run(first["id"])["status"] == "expired"


def test_live_run_lease_can_renew_but_cannot_be_revived(tmp_path):
    run, _ = _bound_run(tmp_path, now=100.0)

    renewed = renew_run(run["id"], lease_seconds=500, now=110.0)
    assert renewed["lease_expires_epoch"] == 610.0

    with pytest.raises(MatterRunConflict, match="expired"):
        renew_run(run["id"], lease_seconds=60, now=611.0)


def test_release_verifies_artifacts_is_idempotent_and_does_not_finish_matter(
    tmp_path,
):
    run, bundle = _bound_run(tmp_path)
    artifact = tmp_path / "result.md"
    artifact.write_text("verified result", encoding="utf-8")
    mark_run_running(
        run["id"], session_id="codex-session", model="gpt-test", now=102.0
    )

    with pytest.raises(MatterRunConflict, match="generation"):
        release_run(
            run["id"], context_generation=99,
            context_digest=bundle["digest"], now=102.5,
        )
    with pytest.raises(MatterRunConflict, match="digest"):
        release_run(
            run["id"], context_generation=run["context_generation"],
            context_digest="sha256:wrong", now=102.5,
        )

    receipt = release_run(
        run["id"],
        context_generation=run["context_generation"],
        context_digest=bundle["digest"],
        narrative="模型说实现完成，测试通过",
        exit_code=0,
        artifacts=["result.md"],
        now=103.0,
    )
    replay = release_run(
        run["id"],
        context_generation=run["context_generation"],
        context_digest=bundle["digest"],
        narrative="模型说实现完成，测试通过",
        exit_code=0,
        artifacts=["result.md"],
        now=104.0,
    )

    assert receipt == replay
    assert receipt["schema"] == "jarvis.result-receipt.v1"
    assert receipt["matter_completed"] is False
    assert receipt["narrative_trust"] == "unverified_model_report"
    assert receipt["matter_state_at_release"]["next_action"] == "完成协议实现"
    assert receipt["artifacts"][0]["sha256"]
    assert receipt["artifacts"][0]["path"] == "result.md"
    assert get_matter(run["matter_id"])["status"] == "active"
    assert any(
        event["event_type"] == "matter_run_released"
        for event in get_matter(run["matter_id"])["events"]
    )

    with pytest.raises(MatterRunConflict, match="different receipt"):
        release_run(
            run["id"],
            context_generation=run["context_generation"],
            context_digest=bundle["digest"],
            narrative="changed claim",
            exit_code=0,
            artifacts=["result.md"],
            now=105.0,
        )


@pytest.mark.parametrize("artifact", ("../outside.txt", "missing.txt"))
def test_release_rejects_unverifiable_artifacts(tmp_path, artifact):
    run, bundle = _bound_run(tmp_path)
    (tmp_path.parent / "outside.txt").write_text("outside", encoding="utf-8")

    with pytest.raises(MatterRunValidationError):
        release_run(
            run["id"],
            context_generation=run["context_generation"],
            context_digest=bundle["digest"],
            artifacts=[artifact],
            now=102.0,
        )


def test_release_rejects_a_stale_logical_context_generation(tmp_path):
    run, bundle = _bound_run(tmp_path)
    clear_derived_context(f"matter:{run['matter_id']}", tmp_path)

    with pytest.raises(MatterRunConflict, match="context generation"):
        release_run(
            run["id"],
            context_generation=run["context_generation"],
            context_digest=bundle["digest"],
            now=102.0,
        )


def test_external_effect_requires_current_trusted_delegation_evidence(tmp_path):
    run, bundle = _bound_run(tmp_path)

    with pytest.raises(MatterRunValidationError, match="evidence"):
        release_run(
            run["id"],
            context_generation=run["context_generation"],
            context_digest=bundle["digest"],
            effects=[{"delegation_id": "dlg_missing", "evidence_id": "dev_missing"}],
            now=102.0,
        )


def test_external_effect_accepts_only_qualifying_authoritative_evidence(tmp_path):
    run, bundle = _bound_run(tmp_path, now=100.0)
    store = DelegationStore(db_path=db_module.DB_PATH, root=tmp_path, now=lambda: 101.0)
    delegation, _ = store.create(
        principal_id="owner",
        source="test",
        source_ref="effect-1",
        title="写入结果",
        operation="write.file",
        matter_id=run["matter_id"],
        target_type="file",
        target_id="result.md",
        expected_postcondition={"exists": True},
        authority="filesystem",
        verification_policy={"verifier": "local_file"},
        authorized=True,
    )
    step = store.add_step(
        delegation["id"], expected_version=1, sequence=1, kind="local_file"
    )
    claim = store.claim_step(
        delegation["id"], step["id"], expected_version=1,
        owner="codex", lease_seconds=60,
    )
    store.record_attempt(
        delegation["id"], step["id"], expected_version=1,
        owner=claim.lease_owner, succeeded=True,
    )
    evidence = store.record_evidence(
        delegation["id"], step["id"], expected_version=1,
        evidence_type="authoritative_readback", strength="strong",
        authority="filesystem", resource_locator="file:result.md",
        observed_digest="sha256:abc", expected_summary="exists",
        observed_summary="exists", matched=True, actor_id="local_file",
    )

    receipt = release_run(
        run["id"],
        context_generation=run["context_generation"],
        context_digest=bundle["digest"],
        effects=[{
            "delegation_id": delegation["id"],
            "evidence_id": evidence["id"],
        }],
        now=102.0,
    )

    assert receipt["effects"] == [{
        "delegation_id": delegation["id"],
        "evidence_id": evidence["id"],
        "authority": "filesystem",
        "resource_locator": "file:result.md",
        "observed_digest": "sha256:abc",
    }]


def test_release_verifies_a_tracked_file_deletion(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=tmp_path, check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"], cwd=tmp_path, check=True
    )
    deleted = tmp_path / "obsolete.txt"
    deleted.write_text("old", encoding="utf-8")
    subprocess.run(["git", "add", "obsolete.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=tmp_path, check=True)
    deleted.unlink()
    run, bundle = _bound_run(tmp_path)

    receipt = release_run(
        run["id"],
        context_generation=run["context_generation"],
        context_digest=bundle["digest"],
        artifacts=["obsolete.txt"],
        now=102.0,
    )

    assert receipt["artifacts"] == [{
        "path": "obsolete.txt",
        "state": "deleted",
        "sha256": "",
        "size": 0,
    }]


def test_executor_authority_cannot_grant_itself_completion(tmp_path):
    matter = create_matter("权限边界")

    with pytest.raises(MatterRunValidationError, match="cannot be granted"):
        acquire_run(
            matter["id"], executor="codex", workspace=tmp_path,
            authority={"may_complete_matter": True}, now=100.0,
        )


@pytest.mark.parametrize("authority", (
    {"unknown_scope": True},
    {"may_modify_workspace": "yes"},
    {"allowed_tools": ["unsafe tool"]},
))
def test_executor_authority_rejects_malformed_downscopes(tmp_path, authority):
    matter = create_matter("坏权限")

    with pytest.raises(MatterRunValidationError):
        acquire_run(
            matter["id"], executor="codex", workspace=tmp_path,
            authority=authority, now=100.0,
        )


def test_context_binding_is_immutable_and_requires_matching_private_files(tmp_path):
    matter = create_matter("不可变上下文")
    run = acquire_run(
        matter["id"], executor="codex", workspace=tmp_path, now=100.0
    )

    with pytest.raises(MatterRunValidationError, match="not readable"):
        bind_context_packet(
            run["id"], packet_id="ctx_missing", context_digest="sha256:missing",
            context_path=tmp_path / "missing.md", now=101.0,
        )

    bundle = build_context_bundle(matter["id"], run=run)
    path = write_context_bundle(matter["id"], output=tmp_path / "bound.md", run=run)
    bound = bind_context_packet(
        run["id"], packet_id=bundle["packet_id"],
        context_digest=bundle["digest"], context_path=path, now=101.0,
    )
    assert bind_context_packet(
        run["id"], packet_id=bundle["packet_id"],
        context_digest=bundle["digest"], context_path=path, now=102.0,
    )["context_digest"] == bound["context_digest"]

    with pytest.raises(MatterRunConflict, match="different context"):
        bind_context_packet(
            run["id"], packet_id="ctx_other", context_digest="sha256:other",
            context_path=path, now=103.0,
        )

    other = create_matter("被篡改的 Markdown")
    other_run = acquire_run(
        other["id"], executor="codex", workspace=tmp_path, now=200.0
    )
    other_bundle = build_context_bundle(other["id"], run=other_run)
    other_path = write_context_bundle(
        other["id"], output=tmp_path / "tampered.md", run=other_run
    )
    other_path.write_text("tampered instructions", encoding="utf-8")
    with pytest.raises(MatterRunValidationError, match="do not match"):
        bind_context_packet(
            other_run["id"], packet_id=other_bundle["packet_id"],
            context_digest=other_bundle["digest"], context_path=other_path,
            now=201.0,
        )


def test_run_cannot_start_without_context_or_after_expiry(tmp_path):
    matter = create_matter("启动门禁")
    run = acquire_run(
        matter["id"], executor="codex", workspace=tmp_path,
        lease_seconds=30, now=100.0,
    )

    with pytest.raises(MatterRunConflict, match="no bound"):
        mark_run_running(run["id"], now=101.0)
    with pytest.raises(MatterRunConflict, match="expired"):
        mark_run_running(run["id"], now=131.0)


def test_explicit_recovery_releases_every_stale_owner(tmp_path):
    matter = create_matter("恢复扫描")
    run = acquire_run(
        matter["id"], executor="codex", workspace=tmp_path,
        lease_seconds=30, now=100.0,
    )

    from core.matter_runs import recover_expired_runs
    assert recover_expired_runs(now=131.0) == [run["id"]]
    assert get_run(run["id"])["status"] == "expired"
    assert recover_expired_runs(now=132.0) == []


def test_independent_lease_connection_can_race_abort_without_transaction_error(
    tmp_path,
):
    from core.db import independent_connection

    run, _bundle = _bound_run(tmp_path, now=100.0)
    errors = []
    started = threading.Event()

    def renew_many():
        with independent_connection() as connection:
            started.set()
            for _ in range(1000):
                try:
                    renew_run(
                        run["id"], lease_seconds=300, now=102.0,
                        connection=connection,
                    )
                except MatterRunConflict:
                    continue
                except Exception as exc:  # pragma: no cover - assertion below
                    errors.append(exc)

    worker = threading.Thread(target=renew_many)
    worker.start()
    assert started.wait(timeout=2)
    abort_run(run["id"], error="owner stopped execution", now=103.0)
    worker.join(timeout=10)

    assert worker.is_alive() is False
    assert errors == []
    assert get_run(run["id"])["status"] == "failed"


def test_receipt_projection_failure_is_visible_but_does_not_erase_receipt(tmp_path):
    from core.matters import link_entity

    artifact = tmp_path / "shared.md"
    artifact.write_text("shared", encoding="utf-8")
    first = create_matter("先占用产物")
    link_entity(
        first["id"], "artifact", str(artifact), provider="file", title="shared.md"
    )
    second = create_matter("投影冲突", next_action="保留权威收据")
    run = acquire_run(
        second["id"], executor="codex", workspace=tmp_path, now=100.0
    )
    bundle = build_context_bundle(second["id"], run=run)
    path = write_context_bundle(second["id"], output=tmp_path / "second.md", run=run)
    bind_context_packet(
        run["id"], packet_id=bundle["packet_id"],
        context_digest=bundle["digest"], context_path=path, now=101.0,
    )

    receipt = release_run(
        run["id"], context_generation=0, context_digest=bundle["digest"],
        artifacts=["shared.md"], now=102.0,
    )

    assert get_run(run["id"])["receipt"]["digest"] == receipt["digest"]
    assert any(
        event["event_type"] == "matter_run_projection_failed"
        for event in get_matter(second["id"])["events"]
    )


def test_audit_event_projection_failure_never_rolls_back_the_lease(
        tmp_path, monkeypatch, capsys):
    matter = create_matter("投影与权威状态分离")
    monkeypatch.setattr(
        "core.matter_run_projection.add_event",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("locked")),
    )

    run = acquire_run(
        matter["id"], executor="codex", workspace=tmp_path, now=100.0
    )

    assert get_run(run["id"])["status"] == "acquired"
    assert "run_event_projection_failed" in capsys.readouterr().err


def test_receipt_projection_still_links_session_when_matter_snapshot_is_missing(
    tmp_path, monkeypatch,
):
    from core import matter_run_projection

    links = []
    events = []
    monkeypatch.setattr(matter_run_projection, "get_matter", lambda _mid: None)
    monkeypatch.setattr(
        matter_run_projection,
        "link_entity",
        lambda *args, **kwargs: links.append((args, kwargs)),
    )
    monkeypatch.setattr(
        matter_run_projection,
        "project_event",
        lambda *args, **kwargs: events.append((args, kwargs)) or True,
    )
    run = {
        "id": "run_missing_snapshot",
        "matter_id": "matter_missing_snapshot",
        "session_id": "session-1",
        "executor": "codex",
        "run_sequence": 3,
        "workspace": str(tmp_path),
        "model": "gpt-test",
    }

    matter_run_projection.project_receipt(
        run=run,
        receipt={
            "receipt_id": "receipt-1",
            "digest": "sha256:receipt",
            "execution": {"exit_code": 0},
        },
        artifacts=[],
        effects=[],
        final_status="released",
    )

    assert links[0][0][:3] == (
        "matter_missing_snapshot", "session", "session-1",
    )
    assert links[0][1]["title"] == "codex run 3"
    assert links[0][1]["metadata"] == {
        "workspace": str(tmp_path),
        "model": "gpt-test",
        "status": "released",
    }
    assert events[0][0][1] == "matter_run_released"
