import os
import subprocess
import time

import core.deploy as deploy
from core.delivery import DeliveryPipeline
from core.deploy import (
    _dirty_runtime_paths,
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
