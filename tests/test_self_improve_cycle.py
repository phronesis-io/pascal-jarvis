"""The daily self-improve cycle: gate discipline and spawn hygiene.

Owner authorization 2026-08-07:「你可以自己定时每几天根据你给我提供的价值，
进行进步」. The heartbeat only hosts the schedule — these tests pin that the
gate can't double-fire, can't overlap a live round, and that a crash after
spawn can't turn into a spawn loop (stamp is written in the same call).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import core.self_improve_cycle as sic  # noqa: E402


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(sic, "JARVIS_DIR", tmp_path)
    monkeypatch.setattr(sic, "WORK_DIR", tmp_path)
    monkeypatch.setattr(sic, "LOG_FILE", str(tmp_path / "cycle.log"))
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "self_improve_prompt.md").write_text(
        "self improve — 按价值数据挖题", encoding="utf-8")
    yield


def _stamp(tmp_path, spawned_at, pid=0):
    (tmp_path / "data").mkdir(exist_ok=True)
    (tmp_path / "data" / "self_improve_cycle.json").write_text(
        json.dumps({"spawned_at": spawned_at, "pid": pid}), encoding="utf-8")


def test_due_when_never_spawned():
    assert sic.due(now_epoch=1_000_000) is True


def test_not_due_inside_the_daily_window(tmp_path):
    _stamp(tmp_path, 1_000_000)
    assert sic.due(now_epoch=1_000_000 + sic.CYCLE_S - 60) is False
    assert sic.due(now_epoch=1_000_000 + sic.CYCLE_S + 60) is True


def test_not_due_while_previous_round_is_alive(tmp_path):
    # Our own pid is definitely alive — a live round blocks a new spawn even
    # long past the window (no overlapping self-improve sessions).
    _stamp(tmp_path, 0, pid=os.getpid())
    assert sic.due(now_epoch=10 * sic.CYCLE_S) is False


def test_spawn_stamps_and_detaches(tmp_path):
    calls = []

    def popen(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(pid=777)

    pid = sic.spawn(popen=popen, now_epoch=2_000_000)
    assert pid == 777
    (argv, kwargs), = calls
    assert argv[:3] == [sys.executable, "-m", "core.self_improve_cycle"]
    assert argv[3] == "run"
    assert kwargs["start_new_session"] is True
    assert kwargs["cwd"] == str(tmp_path)
    state = json.loads(
        (tmp_path / "data" / "self_improve_cycle.json").read_text())
    assert state["spawned_at"] == 2_000_000
    assert state["pid"] == 777
    assert state["status"] == "running"
    assert state["acquire_epoch"] == 2_000_000
    assert state["release_epoch"] == 0
    assert state["run_id"] in argv
    assert len(state["run_digest"]) == 64
    # Stamp written → immediately not due again (crash-loop guard).
    assert sic.due(now_epoch=2_000_100) is False


def test_spawn_admission_rechecks_state_under_the_lock(tmp_path):
    _stamp(tmp_path, 2_000_000, pid=os.getpid())
    path = tmp_path / "data" / "self_improve_cycle.json"
    state = json.loads(path.read_text())
    state.update({"run_id": "already-running", "status": "running",
                  "acquire_epoch": 2_000_000, "release_epoch": 0})
    path.write_text(json.dumps(state))
    calls = []

    pid = sic.spawn(
        popen=lambda *a, **k: calls.append((a, k)), now_epoch=2_000_100)

    assert pid == 0
    assert calls == []
    assert json.loads(path.read_text())["run_id"] == "already-running"


def test_worker_records_release_receipt_for_success(tmp_path):
    run_id = "run-success"
    _stamp(tmp_path, 2_000_000, pid=os.getpid())
    state_path = tmp_path / "data" / "self_improve_cycle.json"
    state = json.loads(state_path.read_text())
    state.update({"run_id": run_id, "status": "running",
                  "acquire_epoch": 2_000_000, "release_epoch": 0,
                  "consecutive_failures": 2})
    state_path.write_text(json.dumps(state))

    result = SimpleNamespace(returncode=0, stdout="finished safely\n", stderr="")
    rc = sic.run_worker(run_id, run=lambda *a, **k: result,
                        now_epoch=2_000_100)

    assert rc == 0
    saved = json.loads(state_path.read_text())
    assert saved["status"] == "succeeded"
    assert saved["release_epoch"] == 2_000_100
    assert saved["consecutive_failures"] == 0
    assert len(saved["output_digest"]) == 64
    receipt = json.loads(
        (tmp_path / "data" / "self_improve_receipts" /
         f"{run_id}.json").read_text())
    assert receipt["status"] == "succeeded"
    usage = [
        json.loads(line)
        for line in (tmp_path / "sched_events.jsonl").read_text().splitlines()
    ]
    assert usage[-1]["event"] == "llm_usage"
    assert usage[-1]["task"] == "self-improve-cycle"
    assert usage[-1]["provider"] == "claude"
    assert usage[-1]["output_chars"] == len("finished safely\n")


def test_worker_marks_empty_success_as_retryable_failure(tmp_path):
    run_id = "run-empty"
    _stamp(tmp_path, 2_000_000, pid=os.getpid())
    state_path = tmp_path / "data" / "self_improve_cycle.json"
    state = json.loads(state_path.read_text())
    state.update({"run_id": run_id, "status": "running",
                  "acquire_epoch": 2_000_000, "release_epoch": 0,
                  "consecutive_failures": 0})
    state_path.write_text(json.dumps(state))

    result = SimpleNamespace(returncode=0, stdout="  ", stderr="")
    assert sic.run_worker(run_id, run=lambda *a, **k: result,
                          now_epoch=2_000_100) == 1
    saved = json.loads(state_path.read_text())
    assert saved["status"] == "empty_success"
    assert saved["consecutive_failures"] == 1
    assert sic.due(now_epoch=2_000_100 + sic.RETRY_S - 1) is False
    assert sic.due(now_epoch=2_000_100 + sic.RETRY_S + 1) is True


def test_late_old_worker_receipt_cannot_overwrite_new_run_state(tmp_path):
    _stamp(tmp_path, 2_000_000, pid=0)
    state_path = tmp_path / "data" / "self_improve_cycle.json"
    state = json.loads(state_path.read_text())
    state.update({"run_id": "new-run", "status": "running",
                  "acquire_epoch": 2_000_000, "release_epoch": 0,
                  "consecutive_failures": 0})
    state_path.write_text(json.dumps(state))

    sic._record_release({
        "run_id": "old-run", "status": "succeeded",
        "acquire_epoch": 1_000_000, "release_epoch": 1_000_100,
        "run_digest": "a" * 64, "output_digest": "b" * 64,
        "output_chars": 10, "exit_code": 0, "error_type": "",
    })

    assert json.loads(state_path.read_text())["run_id"] == "new-run"
    assert (tmp_path / "data" / "self_improve_receipts" /
            "old-run.json").exists()


def test_release_receipt_is_write_once(tmp_path):
    _stamp(tmp_path, 2_000_000, pid=0)
    path = tmp_path / "data" / "self_improve_cycle.json"
    state = json.loads(path.read_text())
    state.update({"run_id": "same-run", "status": "running",
                  "acquire_epoch": 2_000_000, "release_epoch": 0})
    path.write_text(json.dumps(state))
    success = {
        "run_id": "same-run", "status": "succeeded",
        "acquire_epoch": 2_000_000, "release_epoch": 2_000_100,
        "run_digest": "a" * 64, "output_digest": "b" * 64,
        "output_chars": 10, "exit_code": 0, "error_type": "",
    }

    sic._record_release(success)
    sic._record_release({**success, "status": "failed", "exit_code": 1})

    receipt = json.loads(
        (tmp_path / "data" / "self_improve_receipts" /
         "same-run.json").read_text())
    assert receipt["status"] == "succeeded"
    assert json.loads(path.read_text())["status"] == "succeeded"


def test_stale_worker_is_rejected_before_model_execution(tmp_path):
    _stamp(tmp_path, 2_000_000, pid=0)
    state_path = tmp_path / "data" / "self_improve_cycle.json"
    state = json.loads(state_path.read_text())
    state.update({"run_id": "current-run", "status": "running",
                  "acquire_epoch": 2_000_000, "release_epoch": 0})
    state_path.write_text(json.dumps(state))
    calls = []

    assert sic.run_worker(
        "stale-run", run=lambda *a, **k: calls.append(1),
        now_epoch=2_000_100,
    ) == 1

    assert calls == []
    assert json.loads(state_path.read_text())["run_id"] == "current-run"
    receipt = json.loads(
        (tmp_path / "data" / "self_improve_receipts" /
         "stale-run.json").read_text())
    assert receipt["status"] == "rejected"
    assert receipt["error_type"] == "stale_worker_admission"


def test_tick_reconciles_dead_unreleased_worker_before_retry(tmp_path,
                                                             monkeypatch):
    _stamp(tmp_path, 2_000_000, pid=999_999_999)
    state_path = tmp_path / "data" / "self_improve_cycle.json"
    state = json.loads(state_path.read_text())
    state.update({"run_id": "dead-run", "status": "running",
                  "acquire_epoch": 2_000_000, "release_epoch": 0,
                  "consecutive_failures": 0})
    state_path.write_text(json.dumps(state))
    monkeypatch.setattr(sic.time, "time", lambda: 2_000_000 + sic.RETRY_S + 1)
    spawned = []
    monkeypatch.setattr(sic, "spawn",
                        lambda now_epoch=None: spawned.append(now_epoch) or 88)

    sic.tick()

    assert spawned == [2_000_000 + sic.RETRY_S + 1]
    receipt = json.loads(
        (tmp_path / "data" / "self_improve_receipts" /
         "dead-run.json").read_text())
    assert receipt["status"] == "interrupted"


def test_health_only_warns_after_automatic_retries_are_exhausted(tmp_path):
    _stamp(tmp_path, 2_000_000)
    path = tmp_path / "data" / "self_improve_cycle.json"
    state = json.loads(path.read_text())
    state.update({"status": "failed", "consecutive_failures": 1,
                  "release_epoch": 2_000_100})
    path.write_text(json.dumps(state))
    assert sic.health_line() == ""
    state["consecutive_failures"] = sic.FAILURES_BEFORE_WARNING
    path.write_text(json.dumps(state))
    assert "自我改进" in sic.health_line()


def test_missing_prompt_spawns_nothing(tmp_path):
    (tmp_path / "scripts" / "self_improve_prompt.md").unlink()
    called = []
    assert sic.spawn(popen=lambda *a, **k: called.append(1)) == 0
    assert called == []


def test_heartbeat_registers_the_cycle_task():
    from core.heartbeat import parse_heartbeat
    tasks = {t["name"]: t for t in parse_heartbeat(ROOT / "HEARTBEAT.md")}
    task = tasks["self-improve-cycle"]
    assert task["pre"] == "tasks/self_improve_cycle_pre.sh"
    assert task["interval"] == 12 * 3600
