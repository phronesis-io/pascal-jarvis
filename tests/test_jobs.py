"""Tests for core.jobs.JobManager — background job registry."""

import json
import os
import signal
import subprocess
import threading
import time
from pathlib import Path

from core.jobs import JobManager


def test_create_job(tmp_path):
    jm = JobManager(tmp_path / "jobs")
    job_id = jm.create_job("user-1", "run experiment", "msg-123")

    assert job_id.startswith("j-")
    job = jm.get_job(job_id)
    assert job is not None
    assert job["conv_key"] == "user-1"
    assert job["description"] == "run experiment"
    assert job["status"] == "running"
    assert job["session_id"] == f"bg-{job_id}"
    assert job["message_id"] == "msg-123"
    # Job directory should exist
    assert (tmp_path / "jobs" / job_id).is_dir()


def test_list_jobs_filters_by_conv_key(tmp_path):
    jm = JobManager(tmp_path / "jobs")
    jm.create_job("user-1", "task A")
    jm.create_job("user-2", "task B")
    jm.create_job("user-1", "task C")

    jobs_1 = jm.list_jobs(conv_key="user-1")
    jobs_2 = jm.list_jobs(conv_key="user-2")
    jobs_all = jm.list_jobs()

    assert len(jobs_1) == 2
    assert len(jobs_2) == 1
    assert len(jobs_all) == 3


def test_finish_job(tmp_path):
    jm = JobManager(tmp_path / "jobs")
    job_id = jm.create_job("user-1", "experiment")

    jm.finish_job(job_id, "completed")
    job = jm.get_job(job_id)
    assert job["status"] == "completed"
    assert job["finished_at"] is not None
    assert job["pid"] is None


def test_finish_job_failed(tmp_path):
    jm = JobManager(tmp_path / "jobs")
    job_id = jm.create_job("user-1", "broken task")

    jm.finish_job(job_id, "failed")
    job = jm.get_job(job_id)
    assert job["status"] == "failed"


def test_cancel_job_with_process(tmp_path):
    jm = JobManager(tmp_path / "jobs")
    job_id = jm.create_job("user-1", "long task")

    # Start a subprocess in its own process group so cancel doesn't kill us
    proc = subprocess.Popen(
        ["sleep", "300"],
        start_new_session=True,
    )
    jm.update_job(job_id, pid=proc.pid)

    result = jm.cancel_job(job_id)
    assert result is True

    job = jm.get_job(job_id)
    assert job["status"] == "cancelled"
    assert job["finished_at"] is not None

    # Process should be dead
    proc.wait(timeout=5)
    assert proc.returncode != 0


def test_cancel_nonexistent_job(tmp_path):
    jm = JobManager(tmp_path / "jobs")
    assert jm.cancel_job("j-nonexistent") is False


def test_cancel_already_finished(tmp_path):
    jm = JobManager(tmp_path / "jobs")
    job_id = jm.create_job("user-1", "done task")
    jm.finish_job(job_id)

    assert jm.cancel_job(job_id) is False


def test_update_job(tmp_path):
    jm = JobManager(tmp_path / "jobs")
    job_id = jm.create_job("user-1", "task")

    jm.update_job(job_id, pid=12345)
    job = jm.get_job(job_id)
    assert job["pid"] == 12345


def test_get_nonexistent_job(tmp_path):
    jm = JobManager(tmp_path / "jobs")
    assert jm.get_job("j-nope") is None


def test_cleanup_old_jobs(tmp_path):
    jm = JobManager(tmp_path / "jobs")

    # Create and finish a job
    job_id = jm.create_job("user-1", "old task")
    jm.finish_job(job_id)

    # Manually backdate it
    registry = json.loads(jm.registry_path.read_text())
    registry[job_id]["started_at"] = "2020-01-01 00:00:00"
    jm.registry_path.write_text(json.dumps(registry))

    # Create job dir with content
    job_dir = tmp_path / "jobs" / job_id
    (job_dir / "output.md").write_text("old output")

    removed = jm.cleanup_old_jobs(max_age_days=7)
    assert removed == 1
    assert jm.get_job(job_id) is None
    assert not job_dir.exists()


def test_cleanup_preserves_running_jobs(tmp_path):
    jm = JobManager(tmp_path / "jobs")
    job_id = jm.create_job("user-1", "still running")

    # Backdate it
    registry = json.loads(jm.registry_path.read_text())
    registry[job_id]["started_at"] = "2020-01-01 00:00:00"
    jm.registry_path.write_text(json.dumps(registry))

    removed = jm.cleanup_old_jobs(max_age_days=7)
    assert removed == 0
    assert jm.get_job(job_id) is not None


def test_format_job_list_empty(tmp_path):
    jm = JobManager(tmp_path / "jobs")
    assert jm.format_job_list([]) == "No background jobs."


def test_format_job_list_mixed(tmp_path):
    jm = JobManager(tmp_path / "jobs")
    j1 = jm.create_job("user-1", "running task")
    j2 = jm.create_job("user-1", "another task")
    jm.finish_job(j2, "completed")

    jobs = jm.list_jobs(conv_key="user-1")
    output = jm.format_job_list(jobs)

    assert "Background Jobs" in output
    assert j1 in output
    assert j2 in output
    assert "\u23f3" in output  # hourglass for running
    assert "\u2705" in output  # checkmark for completed


def test_list_jobs_running_first(tmp_path):
    jm = JobManager(tmp_path / "jobs")
    j1 = jm.create_job("user-1", "finished first")
    time.sleep(0.1)
    j2 = jm.create_job("user-1", "still running")
    jm.finish_job(j1, "completed")

    jobs = jm.list_jobs(conv_key="user-1")
    assert jobs[0]["status"] == "running"
    assert jobs[1]["status"] == "completed"


def test_concurrent_job_creates(tmp_path):
    """Multiple threads creating jobs should not corrupt the registry."""
    jm = JobManager(tmp_path / "jobs")
    results = []
    errors = []

    def worker(i):
        try:
            job_id = jm.create_job(f"user-{i}", f"task-{i}")
            results.append(job_id)
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"Errors: {errors}"
    assert len(results) == 20
    assert len(set(results)) == 20  # all unique IDs

    # Registry should be valid JSON with all 20 entries
    registry = json.loads(jm.registry_path.read_text())
    assert len(registry) == 20


def test_cli_interface(tmp_path):
    """Test the CLI interface used by bot.sh."""
    jobs_py = str(Path(__file__).parent.parent / "core" / "jobs.py")
    env = {**os.environ, "JV_JOBS_DIR": str(tmp_path / "jobs"),
           "JV_CONV_KEY": "user-1", "JV_DESC": "test task",
           "JV_MSG_ID": "msg-1"}

    # Create
    result = subprocess.run(
        ["python3", jobs_py, "create"],
        env=env, capture_output=True, text=True)
    assert result.returncode == 0
    job_id = result.stdout.strip()
    assert job_id.startswith("j-")

    # List
    result = subprocess.run(
        ["python3", jobs_py, "list"],
        env=env, capture_output=True, text=True)
    assert job_id in result.stdout

    # Get
    result = subprocess.run(
        ["python3", jobs_py, "get", job_id],
        env=env, capture_output=True, text=True)
    data = json.loads(result.stdout)
    assert data["status"] == "running"

    # Finish
    result = subprocess.run(
        ["python3", jobs_py, "finish", job_id, "completed"],
        env=env, capture_output=True, text=True)
    assert result.returncode == 0

    # Verify finished
    result = subprocess.run(
        ["python3", jobs_py, "get", job_id],
        env=env, capture_output=True, text=True)
    data = json.loads(result.stdout)
    assert data["status"] == "completed"

    # Cleanup
    result = subprocess.run(
        ["python3", jobs_py, "cleanup"],
        env=env, capture_output=True, text=True)
    assert "Removed" in result.stdout


def test_description_truncation(tmp_path):
    jm = JobManager(tmp_path / "jobs")
    long_desc = "x" * 500
    job_id = jm.create_job("user-1", long_desc)
    job = jm.get_job(job_id)
    assert len(job["description"]) == 200


# ── promoted-job closure contract (bot.sh, static) ──────────────────────
# j-1786098762 (2026-08-07): an auto-promoted job did its work, but nothing
# was ever written to the output_file the registry advertises, and the empty-
# answer paths ended in deliberate silence — after the promotion message had
# promised 「做完我会把结果发回来」. Silence is the right policy for an
# interactive turn (the user can resend); for a promoted job it is a broken
# promise. These contracts pin the receipt + persisted-output behavior.

def _bot_source():
    from pathlib import Path
    return (Path(__file__).resolve().parent.parent / "bot.sh").read_text(
        encoding="utf-8")


def test_promoted_job_persists_its_result_where_the_registry_points():
    source = _bot_source()
    assert 'printf \'%s\\n\' "$reply" > "$JOBS_DIR/$_promoted_job/output.md"' \
        in source
    assert "任务失败：模型未产出结果" in source


def test_promoted_job_never_ends_in_silence():
    source = _bot_source()
    assert "被外部中断，没有产出结果" in source
    assert "没能产出可交付的结果" in source
    # Both receipts reply to the ORIGINAL message the promotion notice
    # answered, so the user finds them in the thread they were waiting in.
    assert source.count('lark_reply_text "$message_id" \\\n            "后台任务 \\`$_promoted_job\\`') == 2
