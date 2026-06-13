"""Background job manager for Jarvis.

Manages long-running tasks that execute in separate Claude sessions,
allowing the user to continue chatting while jobs run in the background.

Job registry is stored in jobs/registry.json with file-lock protection
(same pattern as session.py). Each job gets its own directory for output.
"""

import fcntl
import json
import os
import signal
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

try:
    from core.timeutil import now_local_str
except ImportError:
    # When run as a script (python3 core/jobs.py), core/ isn't a package.
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from core.timeutil import now_local_str


@contextmanager
def _locked(path: Path):
    """Exclusive file lock (same pattern as session.py)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with open(lock_path, "a+", encoding="utf-8") as fh:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass


def _atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    os.replace(tmp, path)


class JobManager:
    """Manages background jobs with a file-based registry."""

    def __init__(self, jobs_dir: str | Path):
        self.jobs_dir = Path(jobs_dir)
        self.registry_path = self.jobs_dir / "registry.json"
        self.jobs_dir.mkdir(parents=True, exist_ok=True)

    def _read_registry(self) -> dict:
        if not self.registry_path.exists():
            return {}
        try:
            return json.loads(self.registry_path.read_text())
        except (json.JSONDecodeError, OSError):
            return {}

    def create_job(self, conv_key: str, description: str,
                   message_id: str = "") -> str:
        """Create a new job entry. Returns job_id."""
        job_id = f"j-{int(time.time())}-{uuid.uuid4().hex[:6]}"
        job_dir = self.jobs_dir / job_id
        job_dir.mkdir(parents=True, exist_ok=True)

        entry = {
            "conv_key": conv_key,
            "description": description[:200],
            "status": "running",
            "pid": None,
            "session_id": f"bg-{job_id}",
            "started_at": now_local_str("%Y-%m-%d %H:%M:%S"),
            "finished_at": None,
            "output_file": str(job_dir / "output.md"),
            "message_id": message_id,
        }

        with _locked(self.registry_path):
            registry = self._read_registry()
            registry[job_id] = entry
            _atomic_write_json(self.registry_path, registry)

        return job_id

    def update_job(self, job_id: str, **kwargs) -> None:
        """Update fields on a job entry."""
        with _locked(self.registry_path):
            registry = self._read_registry()
            if job_id not in registry:
                return
            registry[job_id].update(kwargs)
            _atomic_write_json(self.registry_path, registry)

    def finish_job(self, job_id: str, status: str = "completed") -> None:
        """Mark a job as finished (completed or failed).

        Only transitions jobs that are still running: the sweeper may have
        marked it lost (or the user cancelled) moments earlier, and a blind
        overwrite turned 'lost' back into 'completed' after the user was
        already told the job died.
        """
        with _locked(self.registry_path):
            registry = self._read_registry()
            job = registry.get(job_id)
            if not job:
                return
            if job.get("status") != "running":
                return
            job["status"] = status
            job["finished_at"] = now_local_str("%Y-%m-%d %H:%M:%S")
            job["pid"] = None
            _atomic_write_json(self.registry_path, registry)

    def cancel_job(self, job_id: str) -> bool:
        """Cancel a running job by killing its process."""
        with _locked(self.registry_path):
            registry = self._read_registry()
            job = registry.get(job_id)
            if not job or job["status"] != "running":
                return False

            pid = job.get("pid")
            if pid:
                try:
                    # Kill the job's process group — but ONLY if it actually
                    # has its own group (REQ-38). A job spawned without
                    # setsid shares bot.sh's group: killpg there SIGTERMs the
                    # ENTIRE bot (heartbeat, admin, handlers) — user-facing
                    # 'cancel <job>' was restarting the whole product.
                    target_pgid = os.getpgid(pid)
                    if target_pgid != os.getpgid(0):
                        os.killpg(target_pgid, signal.SIGTERM)
                    else:
                        os.kill(pid, signal.SIGTERM)
                except (ProcessLookupError, PermissionError, OSError):
                    try:
                        os.kill(pid, signal.SIGTERM)
                    except (ProcessLookupError, PermissionError):
                        pass

            job["status"] = "cancelled"
            job["finished_at"] = now_local_str("%Y-%m-%d %H:%M:%S")
            job["pid"] = None
            _atomic_write_json(self.registry_path, registry)
        return True

    def list_jobs(self, conv_key: str | None = None,
                  include_finished: bool = True,
                  max_age_hours: int = 24) -> list[dict]:
        """List jobs, optionally filtered by conv_key."""
        registry = self._read_registry()
        cutoff = time.time() - max_age_hours * 3600
        results = []

        for job_id, job in registry.items():
            # Filter by conv_key
            if conv_key and job.get("conv_key") != conv_key:
                continue
            # Filter old finished jobs
            if not include_finished and job["status"] != "running":
                continue
            # Filter by age
            try:
                started = time.mktime(time.strptime(
                    job["started_at"], "%Y-%m-%d %H:%M:%S"))
                if started < cutoff and job["status"] != "running":
                    continue
            except (ValueError, KeyError):
                pass

            results.append({"job_id": job_id, **job})

        # Running jobs first, then by start time descending
        results.sort(key=lambda j: (
            0 if j["status"] == "running" else 1,
            j.get("started_at", ""),
        ), reverse=False)
        return results

    def get_job(self, job_id: str) -> dict | None:
        registry = self._read_registry()
        job = registry.get(job_id)
        if job:
            return {"job_id": job_id, **job}
        return None

    def sweep_lost(self, grace_seconds: int = 300) -> list[str]:
        """Reconcile the registry with reality (REQ-16 MVP-3).

        A job marked running whose PID is dead did not go through the normal
        finish path (handler crashed, restart killed it). Mark it lost and
        return the ids so the caller can notify the user — otherwise the job
        silently never completes and the user waits forever. Jobs younger
        than grace_seconds are skipped (the PID may not be registered yet).
        """
        lost = []
        now = time.time()
        with _locked(self.registry_path):
            registry = self._read_registry()
            for job_id, job in registry.items():
                if job.get("status") != "running":
                    continue
                try:
                    started = time.mktime(time.strptime(
                        job.get("started_at", ""), "%Y-%m-%d %H:%M:%S"))
                except (ValueError, OverflowError):
                    started = 0
                if now - started < grace_seconds:
                    continue
                pid = job.get("pid")
                alive = False
                if pid:
                    try:
                        os.kill(pid, 0)
                        alive = True
                    except (ProcessLookupError, PermissionError):
                        alive = False
                if not alive:
                    job["status"] = "lost"
                    job["finished_at"] = now_local_str("%Y-%m-%d %H:%M:%S")
                    job["pid"] = None
                    lost.append(job_id)
            if lost:
                _atomic_write_json(self.registry_path, registry)
        return lost

    def cleanup_old_jobs(self, max_age_days: int = 7) -> int:
        """Remove jobs older than max_age_days. Returns count removed."""
        import shutil
        cutoff = time.time() - max_age_days * 86400
        removed = 0

        with _locked(self.registry_path):
            registry = self._read_registry()
            to_remove = []

            for job_id, job in registry.items():
                if job["status"] == "running":
                    continue
                try:
                    started = time.mktime(time.strptime(
                        job["started_at"], "%Y-%m-%d %H:%M:%S"))
                    if started < cutoff:
                        to_remove.append(job_id)
                except (ValueError, KeyError):
                    to_remove.append(job_id)

            for job_id in to_remove:
                del registry[job_id]
                job_dir = self.jobs_dir / job_id
                if job_dir.exists():
                    shutil.rmtree(job_dir, ignore_errors=True)
                removed += 1

            if to_remove:
                _atomic_write_json(self.registry_path, registry)

        return removed

    def format_job_list(self, jobs: list[dict]) -> str:
        """Format jobs into a readable message."""
        if not jobs:
            return "No background jobs."

        lines = ["**Background Jobs**\n"]
        for j in jobs:
            status_icon = {
                "running": "\u23f3",
                "completed": "\u2705",
                "failed": "\u274c",
                "cancelled": "\u23f9",
            }.get(j["status"], "\u2753")

            desc = j["description"][:60]
            line = f"{status_icon} `{j['job_id']}` — {desc}"
            if j["status"] == "running":
                line += f" (since {j['started_at'][11:]})"
            elif j.get("finished_at"):
                line += f" ({j['finished_at'][11:]})"
            lines.append(line)

        return "\n".join(lines)


if __name__ == "__main__":
    """CLI interface for bot.sh to call."""
    import sys

    jobs_dir = os.environ.get("JV_JOBS_DIR", "jobs")
    jm = JobManager(jobs_dir)
    cmd = sys.argv[1] if len(sys.argv) > 1 else "list"

    if cmd == "create":
        conv_key = os.environ.get("JV_CONV_KEY", "")
        desc = os.environ.get("JV_DESC", "")
        msg_id = os.environ.get("JV_MSG_ID", "")
        print(jm.create_job(conv_key, desc, msg_id))

    elif cmd == "set-pid":
        job_id = sys.argv[2]
        pid = int(sys.argv[3])
        jm.update_job(job_id, pid=pid)

    elif cmd == "finish":
        job_id = sys.argv[2]
        status = sys.argv[3] if len(sys.argv) > 3 else "completed"
        jm.finish_job(job_id, status)

    elif cmd == "cancel":
        job_id = sys.argv[2]
        ok = jm.cancel_job(job_id)
        print("cancelled" if ok else "not_found")

    elif cmd == "list":
        conv_key = os.environ.get("JV_CONV_KEY", "")
        jobs = jm.list_jobs(conv_key=conv_key or None)
        print(jm.format_job_list(jobs))

    elif cmd == "get":
        job_id = sys.argv[2]
        job = jm.get_job(job_id)
        if job:
            print(json.dumps(job, indent=2, ensure_ascii=False))
        else:
            print("not_found")

    elif cmd == "sweep":
        for jid in jm.sweep_lost():
            print(jid)

    elif cmd == "cleanup":
        n = jm.cleanup_old_jobs()
        print(f"Removed {n} old jobs")
