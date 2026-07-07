"""Tests for /health PRIORITY-wedge detection (stability backlog #6a).

6/15 incident: max(last_run) across all tasks masked a brain-dead PRIORITY
task — /health said "ok" for 1h. The endpoint now flags PRIORITY tasks with
consecutive_failures>=3 or a failure-status stale last_success as
priority_wedged, keeping the HTTP contract (degraded → 200).
"""

import http.server
import json
import socket
import threading
import time
import urllib.request

import pytest

import admin as admin_mod
from core.heartbeat import HeartbeatRunner

# A real PRIORITY task name, picked dynamically so nothing here breaks when
# the roster changes.
PRIO = sorted(HeartbeatRunner.PRIORITY_TASKS)[0]


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def isolated_admin(tmp_path, monkeypatch):
    pdir = tmp_path / "project"
    pdir.mkdir()
    mdir = tmp_path / "memory"
    (mdir / "hot").mkdir(parents=True)
    tracker = tmp_path / "active_sessions.json"
    tracker.write_text("{}")

    monkeypatch.setattr(admin_mod, "PROJECT_DIR", pdir)
    monkeypatch.setattr(admin_mod, "MEMORY_DIR", mdir)
    monkeypatch.setattr(admin_mod, "SESSION_TRACKER", tracker)
    monkeypatch.setattr(admin_mod, "ADMIN_TOKEN", "")
    monkeypatch.setattr(admin_mod, "ROOT", tmp_path)

    (tmp_path / "heartbeat_state.json").write_text("{}")
    (tmp_path / "HEARTBEAT.md").write_text(
        f"### {PRIO}\n- interval: 10m\n- prompt: test\n"
        "### test-task\n- interval: 10m\n- prompt: test\n")
    return tmp_path


@pytest.fixture
def server(isolated_admin):
    port = _free_port()
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), admin_mod.Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    time.sleep(0.1)
    yield f"http://127.0.0.1:{port}", isolated_admin
    srv.shutdown()


def _get_health(base):
    # urlopen raises on non-2xx, so a plain read also asserts the HTTP
    # contract: degraded must still be 200.
    resp = urllib.request.urlopen(f"{base}/health", timeout=3)
    return resp.status, json.loads(resp.read())


def _write_state(root, tasks: dict):
    (root / "heartbeat_state.json").write_text(json.dumps(tasks))


def _task_state(*, last_run, last_success, last_status="success",
                consecutive_failures=0):
    return {"last_run": last_run, "last_success": last_success,
            "last_status": last_status,
            "circuit": {"consecutive_failures": consecutive_failures,
                        "total_failures": 0, "total_runs": 0,
                        "disabled_until": 0}}


def test_priority_wedged_by_consecutive_failures(server):
    base, root = server
    now = time.time()
    _write_state(root, {PRIO: _task_state(
        last_run=now, last_success=now, consecutive_failures=3)})
    status, health = _get_health(base)
    assert status == 200
    assert health["status"] == "degraded"
    assert health["priority_wedged"] == [PRIO]


def test_priority_wedged_by_stale_failing_success(server):
    base, root = server
    now = time.time()
    # last_success older than 2×10min interval AND last_status is a failure.
    _write_state(root, {PRIO: _task_state(
        last_run=now, last_success=now - 3 * 600, last_status="failed")})
    status, health = _get_health(base)
    assert status == 200
    assert health["status"] == "degraded"
    assert PRIO in health["priority_wedged"]


def test_priority_stale_success_without_failure_status_not_flagged(server):
    # The 6/15 trap in reverse: empty_pre keeps last_success fresh on failing
    # cycles, so the staleness arm is gated on a failure last_status — a stale
    # success with last_status=success must NOT flag.
    base, root = server
    now = time.time()
    _write_state(root, {PRIO: _task_state(
        last_run=now, last_success=now - 3 * 600, last_status="success")})
    _, health = _get_health(base)
    assert health["status"] == "ok"
    assert "priority_wedged" not in health


def test_healthy_priority_task_ok(server):
    base, root = server
    now = time.time()
    _write_state(root, {PRIO: _task_state(last_run=now, last_success=now)})
    status, health = _get_health(base)
    assert status == 200
    assert health["status"] == "ok"
    assert "priority_wedged" not in health


def test_non_priority_wedged_task_not_flagged(server):
    base, root = server
    now = time.time()
    assert "test-task" not in HeartbeatRunner.PRIORITY_TASKS
    _write_state(root, {"test-task": _task_state(
        last_run=now, last_success=now - 3 * 600, last_status="failed",
        consecutive_failures=4)})
    _, health = _get_health(base)
    assert health["status"] == "ok"
    assert "priority_wedged" not in health


def test_empty_state_file_no_crash(server):
    base, root = server
    _write_state(root, {})
    status, health = _get_health(base)
    assert status == 200
    assert health["status"] == "ok"


def test_absent_state_file_no_crash(server):
    base, root = server
    (root / "heartbeat_state.json").unlink()
    status, health = _get_health(base)
    assert status == 200
    assert health["status"] == "ok"
