import os
import subprocess
import time

from core.delivery import DeliveryPipeline
from core.deploy import (
    _dirty_runtime_paths,
    register_runtime,
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


def test_verify_requires_named_component(tmp_path):
    result = verify_runtime(
        root=tmp_path, db_path=tmp_path / "jarvis.db",
        required=["bot"])
    assert result["ok"] is False
    assert "bot: no runtime registration" in result["issues"]


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
