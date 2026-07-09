"""PID-reuse guard in core.jobs (F23).

cancel_job/sweep_lost used to act on the bare PID: after the job dies the
OS can hand that number to an unrelated process, so cancel could killpg an
innocent process group and sweep_lost could keep a dead job "running"
forever. The guard records the process start time (pid_lstart) when the
PID is registered and verifies it before signalling. Each side errs safe:
cancel never kills without a proven match (except legacy entries recorded
before the field existed), sweep never declares a job dead on a flaky ps.
"""

import json
import os
import signal
import subprocess

import core.jobs as jobs_mod
from core.jobs import JobManager

OLD = "2026-07-08 10:00:00"  # started_at safely past sweep grace


def _job(jm, pid=None, lstart_stub=None, monkeypatch=None):
    job_id = jm.create_job("p2p:u", "identity test job")
    if lstart_stub is not None:
        monkeypatch.setattr(jobs_mod, "_pid_lstart", lambda p: lstart_stub)
    if pid is not None:
        jm.update_job(job_id, pid=pid, started_at=OLD)
    else:
        jm.update_job(job_id, started_at=OLD)
    return job_id


def _strip_lstart(jm, job_id):
    """Turn an entry into a legacy (pre-guard) one: pid without pid_lstart."""
    reg = json.loads(jm.registry_path.read_text())
    reg[job_id].pop("pid_lstart", None)
    jm.registry_path.write_text(json.dumps(reg))


def _record_kills(monkeypatch):
    calls = []
    monkeypatch.setattr(os, "getpgid", lambda p: 111 if p == 0 else 999)
    monkeypatch.setattr(
        os, "killpg", lambda pgid, sig: calls.append(("killpg", pgid, sig)))
    monkeypatch.setattr(
        os, "kill", lambda pid, sig: calls.append(("kill", pid, sig)))
    return calls


# ── registration captures identity ──────────────────────────────────


def test_set_pid_records_lstart(tmp_path, monkeypatch):
    jm = JobManager(tmp_path / "jobs")
    job_id = _job(jm, pid=4242, lstart_stub="T1", monkeypatch=monkeypatch)
    assert jm.get_job(job_id)["pid_lstart"] == "T1"


def test_update_without_pid_records_nothing(tmp_path, monkeypatch):
    jm = JobManager(tmp_path / "jobs")
    monkeypatch.setattr(jobs_mod, "_pid_lstart", lambda p: "T1")
    job_id = _job(jm, pid=None)
    assert "pid_lstart" not in jm.get_job(job_id)


# ── cancel_job ───────────────────────────────────────────────────────


def test_cancel_kills_on_identity_match(tmp_path, monkeypatch):
    jm = JobManager(tmp_path / "jobs")
    job_id = _job(jm, pid=4242, lstart_stub="T1", monkeypatch=monkeypatch)
    calls = _record_kills(monkeypatch)

    assert jm.cancel_job(job_id) is True
    assert calls == [("killpg", 999, signal.SIGTERM)]
    job = jm.get_job(job_id)
    assert job["status"] == "cancelled" and job["pid"] is None


def test_cancel_reused_pid_not_killed(tmp_path, monkeypatch):
    jm = JobManager(tmp_path / "jobs")
    job_id = _job(jm, pid=4242, lstart_stub="T1", monkeypatch=monkeypatch)
    # PID now belongs to a process started at a different time
    monkeypatch.setattr(jobs_mod, "_pid_lstart", lambda p: "T2")
    calls = _record_kills(monkeypatch)

    assert jm.cancel_job(job_id) is True
    assert calls == []
    job = jm.get_job(job_id)
    assert job["status"] == "cancelled" and job["pid"] is None


def test_cancel_current_ps_failure_not_killed(tmp_path, monkeypatch, capsys):
    jm = JobManager(tmp_path / "jobs")
    job_id = _job(jm, pid=4242, lstart_stub="T1", monkeypatch=monkeypatch)
    # ps flaked at cancel time: identity unprovable → err toward not killing
    monkeypatch.setattr(jobs_mod, "_pid_lstart", lambda p: "")
    calls = _record_kills(monkeypatch)

    assert jm.cancel_job(job_id) is True
    assert calls == []
    job = jm.get_job(job_id)
    assert job["status"] == "cancelled"
    # F-17: the skipped kill is never silent — the process may still be OURS
    # and running; the entry records it and stderr says so.
    assert job["kill_skipped"] == "identity_unproven"
    assert "没有发送终止信号" in capsys.readouterr().err


def test_cancel_capture_failed_entry_not_killed(tmp_path, monkeypatch, capsys):
    jm = JobManager(tmp_path / "jobs")
    # capture failed at set-pid time → recorded ""
    job_id = _job(jm, pid=4242, lstart_stub="", monkeypatch=monkeypatch)
    monkeypatch.setattr(jobs_mod, "_pid_lstart", lambda p: "T-whatever")
    calls = _record_kills(monkeypatch)

    assert jm.cancel_job(job_id) is True
    assert calls == []
    job = jm.get_job(job_id)
    assert job["status"] == "cancelled"
    assert job["kill_skipped"] == "identity_unproven"       # F-17 note
    assert "可能还在运行" in capsys.readouterr().err


def test_cancel_match_kill_leaves_no_skip_note(tmp_path, monkeypatch):
    """A proven-identity kill is a REAL cancel — no caveat anywhere."""
    jm = JobManager(tmp_path / "jobs")
    job_id = _job(jm, pid=4242, lstart_stub="T1", monkeypatch=monkeypatch)
    _record_kills(monkeypatch)

    assert jm.cancel_job(job_id) is True
    assert "kill_skipped" not in jm.get_job(job_id)


def test_cancel_reused_pid_leaves_no_skip_note(tmp_path, monkeypatch, capsys):
    """A PROVEN identity mismatch means our process is already dead — the
    skipped kill is correct, not a lie; no false alarm to the user."""
    jm = JobManager(tmp_path / "jobs")
    job_id = _job(jm, pid=4242, lstart_stub="T1", monkeypatch=monkeypatch)
    monkeypatch.setattr(jobs_mod, "_pid_lstart", lambda p: "T2")
    calls = _record_kills(monkeypatch)

    assert jm.cancel_job(job_id) is True
    assert calls == []
    assert "kill_skipped" not in jm.get_job(job_id)
    assert "终止信号" not in capsys.readouterr().err


def test_list_surfaces_skipped_kill(tmp_path, monkeypatch):
    """The job list tells Pascal the cancel could not verify the kill —
    list/sweep are the surfaces the F-17 registry note exists for."""
    jm = JobManager(tmp_path / "jobs")
    job_id = _job(jm, pid=4242, lstart_stub="", monkeypatch=monkeypatch)
    _record_kills(monkeypatch)
    jm.cancel_job(job_id)

    listing = jm.format_job_list(jm.list_jobs())
    assert job_id in listing
    assert "可能还在运行" in listing


def test_cancel_legacy_entry_keeps_kill(tmp_path, monkeypatch):
    jm = JobManager(tmp_path / "jobs")
    job_id = _job(jm, pid=4242, lstart_stub="T1", monkeypatch=monkeypatch)
    _strip_lstart(jm, job_id)  # entry written before the guard shipped
    calls = _record_kills(monkeypatch)

    assert jm.cancel_job(job_id) is True
    assert calls == [("killpg", 999, signal.SIGTERM)]
    assert jm.get_job(job_id)["status"] == "cancelled"


# ── sweep_lost ───────────────────────────────────────────────────────


def test_sweep_reused_pid_marked_lost(tmp_path, monkeypatch):
    jm = JobManager(tmp_path / "jobs")
    # our own pid is alive, but the recorded identity no longer matches
    job_id = _job(jm, pid=os.getpid(), lstart_stub="T1",
                  monkeypatch=monkeypatch)
    monkeypatch.setattr(jobs_mod, "_pid_lstart", lambda p: "T2")

    assert jm.sweep_lost(grace_seconds=300) == [job_id]
    job = jm.get_job(job_id)
    assert job["status"] == "lost" and job["pid"] is None


def test_sweep_identity_match_stays_running(tmp_path, monkeypatch):
    jm = JobManager(tmp_path / "jobs")
    job_id = _job(jm, pid=os.getpid(), lstart_stub="T1",
                  monkeypatch=monkeypatch)

    assert jm.sweep_lost(grace_seconds=300) == []
    assert jm.get_job(job_id)["status"] == "running"


def test_sweep_transient_ps_failure_stays_running(tmp_path, monkeypatch):
    jm = JobManager(tmp_path / "jobs")
    job_id = _job(jm, pid=os.getpid(), lstart_stub="T1",
                  monkeypatch=monkeypatch)
    # one flaky ps must not tell the user a healthy job died
    monkeypatch.setattr(jobs_mod, "_pid_lstart", lambda p: "")

    assert jm.sweep_lost(grace_seconds=300) == []
    assert jm.get_job(job_id)["status"] == "running"


def test_sweep_legacy_entry_bare_pid_behavior(tmp_path, monkeypatch):
    jm = JobManager(tmp_path / "jobs")
    job_id = _job(jm, pid=os.getpid(), lstart_stub="T1",
                  monkeypatch=monkeypatch)
    _strip_lstart(jm, job_id)
    monkeypatch.setattr(jobs_mod, "_pid_lstart", lambda p: "T2")

    assert jm.sweep_lost(grace_seconds=300) == []
    assert jm.get_job(job_id)["status"] == "running"


def test_sweep_capture_failed_entry_bare_pid_behavior(tmp_path, monkeypatch):
    jm = JobManager(tmp_path / "jobs")
    job_id = _job(jm, pid=os.getpid(), lstart_stub="",
                  monkeypatch=monkeypatch)
    monkeypatch.setattr(jobs_mod, "_pid_lstart", lambda p: "T2")

    assert jm.sweep_lost(grace_seconds=300) == []
    assert jm.get_job(job_id)["status"] == "running"


def test_sweep_dead_pid_still_lost_with_identity(tmp_path, monkeypatch):
    jm = JobManager(tmp_path / "jobs")
    p = subprocess.Popen(["true"])
    p.wait()
    job_id = _job(jm, pid=p.pid, lstart_stub="T1", monkeypatch=monkeypatch)

    assert jm.sweep_lost(grace_seconds=300) == [job_id]
    assert jm.get_job(job_id)["status"] == "lost"


# ── real-process roundtrip (unstubbed _pid_lstart) ───────────────────


def test_real_process_capture_and_cancel(tmp_path):
    jm = JobManager(tmp_path / "jobs")
    job_id = jm.create_job("p2p:u", "real roundtrip")
    proc = subprocess.Popen(["sleep", "300"], start_new_session=True)
    try:
        jm.update_job(job_id, pid=proc.pid)
        assert jm.get_job(job_id)["pid_lstart"]  # ps capture worked

        assert jm.cancel_job(job_id) is True  # live match → really killed
        proc.wait(timeout=5)
        assert proc.returncode != 0
        assert jm.get_job(job_id)["status"] == "cancelled"
    finally:
        if proc.poll() is None:
            proc.kill()
