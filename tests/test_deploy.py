import os
import subprocess
import time
from pathlib import Path

import core.deploy as deploy
from core.delivery import DeliveryPipeline
from core.deploy import (
    _dirty_runtime_paths,
    deregister_runtime,
    latest_release_receipt,
    record_release_receipt,
    release_receipt_status,
    register_runtime,
    revision_contains,
    smoke_delivery,
    verify_runtime,
)


def _heartbeat(path):
    path.write_text(
        "# Heartbeat\n\n### test-task\n"
        "- interval: 10m\n"
        "- prompt: |\n"
        "  Test task.\n",
        encoding="utf-8",
    )


def test_register_and_verify_runtime_with_heartbeat_integrity(tmp_path):
    (tmp_path / "core").mkdir()
    source = tmp_path / "core" / "worker.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    heartbeat = tmp_path / "HEARTBEAT.md"
    _heartbeat(heartbeat)
    db_path = tmp_path / "data" / "jarvis.db"

    row = register_runtime(
        "heartbeat-loop",
        pid=os.getpid(),
        root=tmp_path,
        db_path=db_path,
        heartbeat_file=heartbeat,
    )
    assert row["metadata"]["heartbeat_loaded"] is True
    assert row["metadata"]["heartbeat_tasks"] == 1

    result = verify_runtime(
        root=tmp_path, db_path=db_path, required=["heartbeat-loop"])
    assert result["ok"] is True
    assert result["components"][0]["alive"] is True


def test_verify_detects_code_and_heartbeat_changed_after_start(tmp_path):
    (tmp_path / "core").mkdir()
    source = tmp_path / "core" / "worker.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    heartbeat = tmp_path / "HEARTBEAT.md"
    _heartbeat(heartbeat)
    db_path = tmp_path / "jarvis.db"
    register_runtime(
        "heartbeat-loop", pid=os.getpid(), root=tmp_path,
        db_path=db_path, heartbeat_file=heartbeat)

    future = time.time() + 2
    source.write_text("VALUE = 2\n", encoding="utf-8")
    os.utime(source, (future, future))
    heartbeat.write_text(heartbeat.read_text() + "\n", encoding="utf-8")

    result = verify_runtime(
        root=tmp_path, db_path=db_path, required=["heartbeat-loop"])
    assert result["ok"] is False
    assert any("runtime code changed" in issue for issue in result["issues"])
    assert any("HEARTBEAT.md changed" in issue for issue in result["issues"])


def test_verify_ignores_touch_only_mtime_changes(tmp_path):
    (tmp_path / "core").mkdir()
    (tmp_path / "core" / "worker.py").write_text(
        "VALUE = 1\n", encoding="utf-8"
    )
    register_runtime(
        "bot", pid=os.getpid(), root=tmp_path,
        db_path=tmp_path / "jarvis.db",
    )
    code = tmp_path / "core" / "worker.py"
    stat = code.stat()
    os.utime(code, (stat.st_atime + 60, stat.st_mtime + 60))

    result = verify_runtime(
        root=tmp_path,
        db_path=tmp_path / "jarvis.db",
        required=["bot"],
    )

    assert result["ok"] is True


def test_verify_config_restart_allows_config_but_not_code_changes(tmp_path):
    (tmp_path / "core").mkdir()
    source = tmp_path / "core" / "worker.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    config = tmp_path / "jarvis.yaml"
    config.write_text("admin:\n  enabled: false\n", encoding="utf-8")
    heartbeat = tmp_path / "HEARTBEAT.md"
    _heartbeat(heartbeat)
    db_path = tmp_path / "jarvis.db"
    register_runtime(
        "heartbeat-loop",
        pid=os.getpid(),
        root=tmp_path,
        db_path=db_path,
        heartbeat_file=heartbeat,
    )

    future = time.time() + 2
    config.write_text("admin:\n  enabled: true\n", encoding="utf-8")
    heartbeat.write_text(heartbeat.read_text() + "\n", encoding="utf-8")
    os.utime(config, (future, future))
    os.utime(heartbeat, (future, future))

    ordinary = verify_runtime(
        root=tmp_path, db_path=db_path, required=["heartbeat-loop"]
    )
    config_restart = verify_runtime(
        root=tmp_path,
        db_path=db_path,
        required=["heartbeat-loop"],
        allow_config_changes=True,
    )

    assert ordinary["ok"] is False
    assert config_restart["ok"] is True

    source.write_text("VALUE = 2\n", encoding="utf-8")
    os.utime(source, (future + 1, future + 1))
    changed_code = verify_runtime(
        root=tmp_path,
        db_path=db_path,
        required=["heartbeat-loop"],
        allow_config_changes=True,
    )
    assert changed_code["ok"] is False
    assert any(
        "runtime code changed" in issue for issue in changed_code["issues"]
    )


def test_verify_requires_named_component(tmp_path):
    result = verify_runtime(
        root=tmp_path, db_path=tmp_path / "jarvis.db",
        required=["bot"])
    assert result["ok"] is False
    assert "bot: no runtime registration" in result["issues"]


def test_verify_required_components_ignores_stale_optional_registration(
    tmp_path,
):
    db_path = tmp_path / "jarvis.db"
    register_runtime(
        "bot", pid=os.getpid(), root=tmp_path, db_path=db_path
    )
    register_runtime(
        "admin", pid=999_999_999, root=tmp_path, db_path=db_path
    )

    result = verify_runtime(
        root=tmp_path, db_path=db_path, required=["bot"]
    )

    assert result["ok"] is True
    assert [row["component"] for row in result["components"]] == ["bot"]
    assert not any("admin" in issue for issue in result["issues"])


def test_deploy_smoke_reaches_acted_within_budget(tmp_path):
    result = smoke_delivery(
        root=tmp_path, db_path=tmp_path / "jarvis.db", timeout=3)
    assert result["ok"] is True
    assert result["state"] == "acted"
    row = DeliveryPipeline(
        tmp_path, db_path=tmp_path / "jarvis.db").get(result["delivery_id"])
    assert row["state"] == "acted"


def test_dirty_runtime_paths_preserves_worktree_only_filename(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "jarvis-test@example.com"],
        cwd=tmp_path, check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Jarvis Test"],
        cwd=tmp_path, check=True,
    )
    core = tmp_path / "core"
    core.mkdir()
    source = core / "worker.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "core/worker.py"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "commit", "-qm", "baseline"], cwd=tmp_path, check=True)

    source.write_text("VALUE = 2\n", encoding="utf-8")

    assert _dirty_runtime_paths(tmp_path) == ["core/worker.py"]


def test_verify_reads_dirty_runtime_paths_once(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        deploy,
        "_dirty_runtime_paths",
        lambda _root: calls.append(1) or ["core/worker.py"],
    )

    result = verify_runtime(root=tmp_path, db_path=tmp_path / "jarvis.db")

    assert result["warnings"] == [
        "uncommitted runtime code: core/worker.py"
    ]
    assert calls == [1]


def test_revision_contains_accepts_exact_and_descendant_revision(tmp_path):
    release_sha = "a" * 40
    resident_sha = "b" * 40
    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, "", "")

    assert revision_contains(release_sha, release_sha, root=tmp_path) is True
    assert revision_contains(
        release_sha,
        resident_sha,
        root=tmp_path,
        runner=runner,
    ) is True
    assert calls[0][0] == [
        "git",
        "merge-base",
        "--is-ancestor",
        release_sha,
        resident_sha,
    ]
    assert calls[0][1]["cwd"] == str(tmp_path)


def test_revision_contains_rejects_unrelated_revision(tmp_path):
    release_sha = "c" * 40
    resident_sha = "d" * 40

    def runner(command, **_kwargs):
        return subprocess.CompletedProcess(command, 1, "", "")

    assert revision_contains(
        release_sha,
        resident_sha,
        root=tmp_path,
        runner=runner,
    ) is False


def test_deregister_removes_only_the_retired_component_row(tmp_path):
    """2026-08-21 dashboard retirement: a retired surface never re-registers,
    so without an explicit teardown its dead row fails every unfiltered
    verify forever. Deregistering it must not touch live components."""
    (tmp_path / "core").mkdir()
    (tmp_path / "core" / "worker.py").write_text("VALUE = 1\n", encoding="utf-8")
    db_path = tmp_path / "jarvis.db"
    register_runtime("bot", pid=os.getpid(), root=tmp_path, db_path=db_path)
    register_runtime(
        "dashboard", pid=999_999_999, root=tmp_path, db_path=db_path)

    stale = verify_runtime(root=tmp_path, db_path=db_path)
    assert any("dashboard" in issue for issue in stale["issues"])

    assert deregister_runtime(
        "dashboard", root=tmp_path, db_path=db_path) == {
        "component": "dashboard", "removed": 1}
    # Idempotent: a second teardown is a clean no-op.
    assert deregister_runtime(
        "dashboard", root=tmp_path, db_path=db_path)["removed"] == 0

    after = verify_runtime(root=tmp_path, db_path=db_path)
    assert not any("dashboard" in issue for issue in after["issues"])
    assert [c["component"] for c in after["components"]] == ["bot"]


def test_release_receipt_persists_one_joined_success_record(
    tmp_path, monkeypatch,
):
    db_path = tmp_path / "jarvis.db"
    sha = "a" * 40
    monkeypatch.setattr(deploy, "git_head", lambda _root=None: sha)
    gate_path = tmp_path / "gate.json"
    gate_path.write_text(
        '{"ok":true,"sha":"' + sha + '","pr":105,'
        '"approval_mode":"owner_release_decision",'
        '"owner_release_decisions":[{"actor":"owner",'
        '"reason":"private release reason"}]}',
        encoding="utf-8",
    )

    result = record_release_receipt(
        gate_evidence=gate_path,
        mode="governed",
        root=tmp_path,
        db_path=db_path,
        verify_fn=lambda **_kwargs: {
            "ok": True,
            "git_head": sha,
            "components": [{"component": "bot", "git_head": sha}],
            "issues": [],
            "warnings": [],
        },
        component_fn=lambda **_kwargs: [
            {"name": "bot", "ok": True, "critical": True, "detail": "alive"},
        ],
        smoke_fn=lambda **_kwargs: {
            "ok": True, "state": "acted", "delivery_id": "smoke-1",
        },
        now_epoch=1234.5,
    )

    assert result["ok"] is True
    assert result["receipt"]["git_head"] == sha
    assert result["receipt"]["gate"]["pr"] == 105
    assert result["receipt"]["gate"]["owner_actors"] == ["owner"]
    assert "private release reason" not in str(result["receipt"])
    assert result["receipt"]["runtime"]["ok"] is True
    assert result["receipt"]["components"][0]["name"] == "bot"
    assert result["receipt"]["smoke"]["state"] == "acted"
    assert latest_release_receipt(
        root=tmp_path, db_path=db_path,
    ) == result["receipt"]


def test_release_receipt_fails_closed_and_does_not_persist_partial_evidence(
    tmp_path, monkeypatch,
):
    db_path = tmp_path / "jarvis.db"
    sha = "b" * 40
    monkeypatch.setattr(deploy, "git_head", lambda _root=None: sha)
    gate = {"ok": True, "sha": sha, "pr": 105}

    result = record_release_receipt(
        gate_evidence=gate,
        mode="governed",
        root=tmp_path,
        db_path=db_path,
        verify_fn=lambda **_kwargs: {
            "ok": False, "git_head": sha, "components": [],
            "issues": ["bot: stale"], "warnings": [],
        },
        component_fn=lambda **_kwargs: [],
        smoke_fn=lambda **_kwargs: {"ok": True, "state": "acted"},
    )

    assert result["ok"] is False
    assert "runtime verification failed" in result["issues"]
    assert latest_release_receipt(root=tmp_path, db_path=db_path) is None


def test_release_receipt_rejects_gate_for_another_revision(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(deploy, "git_head", lambda _root=None: "c" * 40)

    result = record_release_receipt(
        gate_evidence={"ok": True, "sha": "d" * 40},
        mode="runtime",
        root=tmp_path,
        db_path=tmp_path / "jarvis.db",
        verify_fn=lambda **_kwargs: {"ok": True},
        component_fn=lambda **_kwargs: [],
        smoke_fn=lambda **_kwargs: {"ok": True},
    )

    assert result["ok"] is False
    assert result["issues"] == ["release gate SHA does not match HEAD"]


def test_release_receipt_requires_critical_component_evidence(
    tmp_path, monkeypatch,
):
    sha = "e" * 40
    monkeypatch.setattr(deploy, "git_head", lambda _root=None: sha)

    result = record_release_receipt(
        gate_evidence={"ok": True, "sha": sha},
        mode="governed",
        root=tmp_path,
        db_path=tmp_path / "jarvis.db",
        verify_fn=lambda **_kwargs: {"ok": True},
        component_fn=lambda **_kwargs: [],
        smoke_fn=lambda **_kwargs: {"ok": True},
    )

    assert result["ok"] is False
    assert result["issues"] == ["no critical component evidence"]


def test_release_receipt_status_rejects_evidence_for_old_head(
    tmp_path, monkeypatch,
):
    db_path = tmp_path / "jarvis.db"
    old_sha = "1" * 40
    monkeypatch.setattr(deploy, "git_head", lambda _root=None: old_sha)
    recorded = record_release_receipt(
        gate_evidence={"ok": True, "sha": old_sha},
        mode="governed",
        root=tmp_path,
        db_path=db_path,
        verify_fn=lambda **_kwargs: {"ok": True},
        component_fn=lambda **_kwargs: [{"name": "bot", "ok": True}],
        smoke_fn=lambda **_kwargs: {"ok": True},
    )
    assert recorded["ok"] is True

    monkeypatch.setattr(deploy, "git_head", lambda _root=None: "2" * 40)
    status = release_receipt_status(root=tmp_path, db_path=db_path)

    assert status["ok"] is False
    assert status["issues"] == [
        "latest release receipt does not match HEAD",
    ]
