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
import subprocess
import sys
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


def _pid_lstart(pid: int) -> str:
    """Start time of a live process, used as its identity.

    A bare PID can be recycled by the OS after the job dies; comparing
    lstart before signalling stops cancel/sweep from acting on an
    unrelated process (same class of bug as the heartbeat pidfile race).
    Returns "" whenever ps fails — callers must treat "" as unknown,
    never as a match.
    """
    try:
        out = subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart="],
            capture_output=True, text=True, timeout=5)
        if out.returncode == 0:
            return out.stdout.strip()
    except Exception:
        pass
    return ""


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
                   message_id: str = "", matter_id: str = "") -> str:
        """Create a new job entry. Returns job_id."""
        job_id = f"j-{int(time.time())}-{uuid.uuid4().hex[:6]}"
        job_dir = self.jobs_dir / job_id
        job_dir.mkdir(parents=True, exist_ok=True)

        if not matter_id and os.environ.get("JARVIS_DIR"):
            try:
                from core.matter_bridge import get_binding
                binding = get_binding(conv_key)
                matter_id = str((binding or {}).get("matter_id", ""))
            except Exception:
                matter_id = ""
        entry = {
            "conv_key": conv_key,
            "description": description[:200],
            "status": "running",
            "pid": None,
            "session_id": f"bg-{job_id}",
            "session_ids": [],
            "started_at": now_local_str("%Y-%m-%d %H:%M:%S"),
            "finished_at": None,
            "output_file": str(job_dir / "output.md"),
            "message_id": message_id,
            "matter_id": matter_id,
        }

        with _locked(self.registry_path):
            registry = self._read_registry()
            registry[job_id] = entry
            _atomic_write_json(self.registry_path, registry)

        if matter_id:
            try:
                from core.matters import add_event, link_entity
                link_entity(matter_id, "job", job_id, provider="jarvis",
                            title=description[:200], metadata={"status": "running"},
                            actor="job")
                add_event(matter_id, "job_started", description[:600], actor="job",
                          payload={"job_id": job_id})
            except Exception:
                pass
        return job_id

    def update_job(self, job_id: str, **kwargs) -> bool:
        """Update fields on a job entry and report whether it existed."""
        if kwargs.get("pid"):
            # Capture identity alongside the PID (covers the set-pid CLI
            # arm) so cancel/sweep can detect reuse. Done before taking
            # the lock — ps may block up to its 5s timeout.
            kwargs["pid_lstart"] = _pid_lstart(kwargs["pid"])
        with _locked(self.registry_path):
            registry = self._read_registry()
            if job_id not in registry:
                return False
            registry[job_id].update(kwargs)
            session_id = str(kwargs.get("session_id") or "")
            if session_id:
                session_ids = registry[job_id].get("session_ids")
                if not isinstance(session_ids, list):
                    session_ids = []
                if session_id not in session_ids:
                    session_ids.append(session_id)
                registry[job_id]["session_ids"] = session_ids
            _atomic_write_json(self.registry_path, registry)
        return True

    def add_session_id(self, job_id: str, session_id: str) -> bool:
        """Register another provider session without changing the primary ID."""
        session_id = str(session_id or "").strip()
        if not session_id:
            return False
        with _locked(self.registry_path):
            registry = self._read_registry()
            job = registry.get(job_id)
            if not isinstance(job, dict):
                return False
            session_ids = job.get("session_ids")
            if not isinstance(session_ids, list):
                session_ids = []
            if session_id not in session_ids:
                session_ids.append(session_id)
            job["session_ids"] = session_ids
            _atomic_write_json(self.registry_path, registry)
        return True

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
        self._reconcile_matter(job_id, job, status)

    def _reconcile_matter(self, job_id: str, job: dict, status: str) -> None:
        """Best-effort projection of a terminal Job into its Matter index."""
        matter_id = str(job.get("matter_id") or "")
        if matter_id:
            try:
                from core.matters import add_event, link_entity
                output = ""
                try:
                    output = Path(job.get("output_file", "")).read_text(
                        encoding="utf-8", errors="ignore")[:1200]
                except OSError:
                    pass
                link_entity(matter_id, "job", job_id, provider="jarvis",
                            title=job.get("description", job_id),
                            metadata={"status": status,
                                      "output_file": job.get("output_file", "")},
                            actor="job")
                add_event(matter_id, "job_finished",
                          output or f"后台任务 {status}", actor="job",
                          payload={"job_id": job_id, "status": status})
                if output and job.get("output_file"):
                    link_entity(matter_id, "artifact", job["output_file"],
                                provider="file", title=f"{job_id} 输出",
                                metadata={"job_id": job_id}, actor="job")
            except Exception:
                pass

    def cancel_job(self, job_id: str) -> bool:
        """Cancel a running job by killing its process."""
        with _locked(self.registry_path):
            registry = self._read_registry()
            job = registry.get(job_id)
            if not job or job["status"] != "running":
                return False

            pid = job.get("pid")
            # PID-reuse guard: only signal a process we can prove is still
            # the one registered for this job. Recorded lstart must match
            # the current one; "" (capture failed at set-pid) means we can
            # never prove it, so close the entry without killing. A missing
            # key is a pre-guard entry — keep the old kill behavior rather
            # than silently leaving a legacy job running.
            kill_skipped = ""
            if pid:
                recorded = job.get("pid_lstart")
                if recorded is None:
                    allow_kill = True
                elif recorded == "":
                    # Identity was never captured — the process may well be
                    # OURS and still running; we just can't prove it.
                    allow_kill = False
                    kill_skipped = "identity_unproven"
                else:
                    current = _pid_lstart(pid)
                    if current == recorded:
                        allow_kill = True
                    elif current == "":
                        # ps flaked at cancel time — unprovable this instant.
                        allow_kill = False
                        kill_skipped = "identity_unproven"
                    else:
                        # PID provably reused: OUR process is already dead,
                        # so skipping the kill is correct, not a lie.
                        allow_kill = False
            if pid and allow_kill:
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
            elif pid and kill_skipped:
                # F-17: a skipped kill must never be silent — the entry is
                # closed as cancelled, but the real process may still be
                # running and burning tokens. Record the skip on the entry
                # so list/sweep surface it, and say so on stderr.
                job["kill_skipped"] = kill_skipped
                print(f"[jobs] 取消 {job_id}：无法确认进程 {pid} 就是这个任务"
                      f"（进程身份信息缺失），没有发送终止信号；任务已按取消"
                      f"处理，但后台进程可能还在运行", file=sys.stderr)

            job["status"] = "cancelled"
            job["finished_at"] = now_local_str("%Y-%m-%d %H:%M:%S")
            job["pid"] = None
            _atomic_write_json(self.registry_path, registry)
        self._reconcile_matter(job_id, job, "cancelled")
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
        lost_jobs = []
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
                if alive and job.get("pid_lstart"):
                    # PID exists but may be a recycled one wearing the dead
                    # job's number. Only demote to dead on a successful ps
                    # read that disagrees — a transient ps failure ("")
                    # must not mark a healthy job lost.
                    current = _pid_lstart(pid)
                    if current and current != job["pid_lstart"]:
                        alive = False
                if not alive:
                    job["status"] = "lost"
                    job["finished_at"] = now_local_str("%Y-%m-%d %H:%M:%S")
                    job["pid"] = None
                    lost.append(job_id)
                    lost_jobs.append((job_id, dict(job)))
            if lost:
                _atomic_write_json(self.registry_path, registry)
        for job_id, job in lost_jobs:
            self._reconcile_matter(job_id, job, "lost")
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
            if j.get("kill_skipped"):
                # F-17: the cancel closed the entry without a provable kill.
                line += "（取消时没能确认后台进程已停，可能还在运行）"
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
        print(jm.create_job(conv_key, desc, msg_id,
                            os.environ.get("JV_MATTER_ID", "")))

    elif cmd == "set-pid":
        job_id = sys.argv[2]
        pid = int(sys.argv[3])
        jm.update_job(job_id, pid=pid)

    elif cmd == "set-session":
        job_id = sys.argv[2]
        session_id = sys.argv[3]
        print("updated" if jm.update_job(job_id, session_id=session_id) else "not_found")

    elif cmd == "add-session":
        job_id = sys.argv[2]
        session_id = sys.argv[3]
        print("updated" if jm.add_session_id(job_id, session_id) else "not_found")

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
