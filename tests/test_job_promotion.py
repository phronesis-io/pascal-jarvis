"""Tests for REQ-16 MVP-2/3 primitives: forced session rotation on job
promotion, and the lost-job sweeper."""

import json
import os
import subprocess
import sys
import time

from core.jobs import JobManager
from core.session import SessionManager


# ── SessionManager.force_rotate (promotion rotates the conversation) ─


def test_force_rotate_changes_session_and_bumps_counter(tmp_path):
    tracker = tmp_path / "active_sessions.json"
    sm = SessionManager(tracker, tmp_path)
    sid1, _ = sm.get_session("p2p:user1")
    sid2 = sm.force_rotate("p2p:user1")

    assert sid2 != sid1
    data = json.loads(tracker.read_text())
    assert data["p2p:user1"]["session_id"] == sid2
    assert data["p2p:user1"]["counter"] == 2
    # subsequent lookups stick to the rotated session
    sid3, rotated = sm.get_session("p2p:user1")
    assert sid3 == sid2 and not rotated


def test_force_rotate_on_unknown_conv_creates_entry(tmp_path):
    sm = SessionManager(tmp_path / "tracker.json", tmp_path)
    sid = sm.force_rotate("p2p:new")
    assert sid
    assert sm.get_session("p2p:new")[0] == sid


# ── JobManager.sweep_lost ────────────────────────────────────────────


def _make_running_job(jm, started_secs_ago=600, pid=None):
    job_id = jm.create_job("p2p:u", "test job")
    started = time.strftime("%Y-%m-%d %H:%M:%S",
                            time.localtime(time.time() - started_secs_ago))
    jm.update_job(job_id, pid=pid, started_at=started)
    return job_id


def test_sweep_marks_dead_pid_as_lost(tmp_path):
    jm = JobManager(tmp_path)
    # A PID that existed and is gone: spawn-and-reap a child
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    job_id = _make_running_job(jm, pid=p.pid)

    lost = jm.sweep_lost(grace_seconds=300)
    assert lost == [job_id]
    assert jm.get_job(job_id)["status"] == "lost"
    # second sweep is idempotent
    assert jm.sweep_lost(grace_seconds=300) == []


def test_sweep_spares_alive_pid(tmp_path):
    jm = JobManager(tmp_path)
    job_id = _make_running_job(jm, pid=os.getpid())  # we are definitely alive
    assert jm.sweep_lost(grace_seconds=300) == []
    assert jm.get_job(job_id)["status"] == "running"


def test_sweep_spares_young_jobs(tmp_path):
    jm = JobManager(tmp_path)
    # dead pid but started 10s ago — within grace (pid may not be set yet)
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    job_id = _make_running_job(jm, started_secs_ago=10, pid=p.pid)
    assert jm.sweep_lost(grace_seconds=300) == []
    assert jm.get_job(job_id)["status"] == "running"


def test_sweep_marks_pidless_old_running_job_lost(tmp_path):
    jm = JobManager(tmp_path)
    job_id = _make_running_job(jm, pid=None)  # registered but PID never set
    assert jm.sweep_lost(grace_seconds=300) == [job_id]
