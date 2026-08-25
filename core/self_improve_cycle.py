"""Standing self-improvement cycle — value-driven, quiet, every day.

Owner authorization (2026-08-07): 「你可以自己定时每几天根据你给我提供的价值，
进行进步」, on top of「有些自进化不用打扰我哦」; tightened to DAILY on
2026-08-09 by 「你就应该每天都想办法去自我进化一下，找时间去自我检修」. So:
every ~24h a detached Claude Code session runs one full self-improve round,
mining its topics from the real value ledgers (批阅率, noise sources, dead
ends, presence) and shipping internal reversible improvements without pinging
Pascal; only directional or irreversible choices surface as a card.

The heartbeat hosts the SCHEDULE only: the pre-hook gates on a daily stamp,
spawns the detached session, and prints nothing — so the cycle consumes zero
heartbeat model budget and cannot starve other tasks.
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

JARVIS_DIR = Path(os.environ.get(
    "JARVIS_DIR", Path(__file__).resolve().parent.parent))
# The session's cwd decides which auto-memory it loads. The repos directory
# maps to the memory that carries the self-improve workflow, the quiet rule,
# and every standing feedback contract — that context IS the guardrail.
WORK_DIR = Path(os.environ.get(
    "JV_SELF_IMPROVE_CWD", Path.home() / "Desktop" / "jarvis" / "repos"))

CYCLE_S = 86400
RETRY_S = 6 * 3600
FAILURES_BEFORE_WARNING = 2
PROMPT_FILE = "scripts/self_improve_prompt.md"
LOG_FILE = "/tmp/jarvis-self-improve.log"
RUN_TIMEOUT_S = 2 * 3600 + 300
LEASE_GRACE_S = 5 * 60


def _state_path() -> Path:
    return JARVIS_DIR / "data" / "self_improve_cycle.json"


def _read_state() -> dict:
    try:
        return json.loads(_state_path().read_text(encoding="utf-8")) or {}
    except (OSError, json.JSONDecodeError, ValueError):
        return {}


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                         encoding="utf-8")
    temporary.chmod(0o600)
    os.replace(temporary, path)


@contextmanager
def _state_lock():
    """Serialize acquire/release state without holding a replaceable inode."""
    import fcntl
    path = JARVIS_DIR / "data" / ".self_improve_cycle.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _receipt_path(run_id: str) -> Path:
    safe = "".join(ch for ch in str(run_id) if ch.isalnum() or ch in "-_")
    return JARVIS_DIR / "data" / "self_improve_receipts" / f"{safe}.json"


def _store_receipt_once(receipt: dict) -> dict:
    """Persist one immutable run receipt; a late duplicate cannot rewrite it."""
    path = _receipt_path(str(receipt["run_id"]))
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
        if (isinstance(existing, dict)
                and existing.get("run_id") == receipt.get("run_id")):
            return existing
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        pass
    _write_json(path, receipt)
    return receipt


def _record_release(receipt: dict) -> None:
    """Persist immutable evidence, then project it into the current state."""
    with _state_lock():
        receipt = _store_receipt_once(receipt)
        state = _read_state()
        if state.get("run_id") != receipt.get("run_id"):
            return
        failures = int(state.get("consecutive_failures") or 0)
        if receipt.get("status") == "succeeded":
            failures = 0
        else:
            failures += 1
        state.update(receipt)
        state["pid"] = 0
        state.pop("termination_failures", None)
        state.pop("last_termination_attempt_epoch", None)
        state.pop("identity_probe_failures", None)
        state.pop("last_identity_probe_epoch", None)
        state["consecutive_failures"] = failures
        _write_json(_state_path(), state)


def _admit_worker(run_id: str, now_epoch: float) -> dict | None:
    """Recheck run ownership after spawn and before the coding model can act."""
    with _state_lock():
        state = _read_state()
        if (state.get("run_id") == run_id
                and state.get("status") in {"acquiring", "running"}
                and not _lease_expired(state, now_epoch)):
            return state
        _store_receipt_once({
            "run_id": run_id,
            "status": "rejected",
            "acquire_epoch": 0,
            "release_epoch": now_epoch,
            "run_digest": "",
            "output_digest": "",
            "output_chars": 0,
            "exit_code": 1,
            "error_type": "stale_worker_admission",
        })
        return None


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    return True


def _lease_deadline(state: dict) -> float:
    explicit = float(state.get("lease_expires_epoch") or 0)
    if explicit > 0:
        return explicit
    acquired = float(state.get("acquire_epoch") or
                     state.get("spawned_at") or 0)
    return acquired + RUN_TIMEOUT_S + LEASE_GRACE_S if acquired > 0 else 0


def _lease_expired(state: dict, now_epoch: float) -> bool:
    deadline = _lease_deadline(state)
    return deadline > 0 and float(now_epoch) >= deadline


def _pid_matches_run(pid: int, run_id: str) -> bool | None:
    """Fence PID reuse; None means process identity could not be inspected."""
    if pid <= 0 or not run_id:
        return False
    try:
        result = subprocess.run(
            ["/bin/ps", "-p", str(pid), "-o", "command="],
            capture_output=True, text=True, timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        # `ps` failure is not evidence of reuse while kill(0) still sees the
        # process. A concurrent exit is a confirmed end, not an uncertainty.
        return False if not _pid_alive(pid) else None
    command = " ".join(str(result.stdout or "").split())
    return ("core.self_improve_cycle" in command
            and f"run {run_id}" in command)


def _terminate_worker(state: dict) -> bool:
    pid = int(state.get("pid") or 0)
    run_id = str(state.get("run_id") or "")
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    for _ in range(10):
        if not _pid_alive(pid):
            return True
        identity = _pid_matches_run(pid, run_id)
        if identity is False:
            return True
        if identity is None:
            return False
        time.sleep(0.1)
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    for _ in range(5):
        if not _pid_alive(pid):
            return True
        identity = _pid_matches_run(pid, run_id)
        if identity is False:
            return True
        if identity is None:
            return False
        time.sleep(0.1)
    if not _pid_alive(pid):
        return True
    identity = _pid_matches_run(pid, run_id)
    return identity is False


def _state_due(state: dict, now: float) -> bool:
    if _pid_alive(int(state.get("pid") or 0)):
        return False
    status = str(state.get("status") or "")
    # A missing release is discovered only on the next scheduler tick. Base
    # its retry clock on acquisition, otherwise reconciliation would add a
    # second full retry delay after the worker had already been dead for hours.
    if status == "interrupted":
        last = float(state.get("spawned_at") or
                     state.get("acquire_epoch") or 0)
    else:
        last = float(state.get("release_epoch") or
                     state.get("spawned_at") or 0)
    delay = (RETRY_S if status in {
        "empty_success", "failed", "interrupted", "spawn_failed", "timeout"
    } else CYCLE_S)
    return now - last >= delay


def due(now_epoch: float | None = None) -> bool:
    """One live round at a time; failed/released rounds retry with a bound."""
    now = time.time() if now_epoch is None else float(now_epoch)
    return _state_due(_read_state(), now)


def spawn(popen=subprocess.Popen, now_epoch: float | None = None) -> int:
    """Detach one self-improve session; stamp first so a crash can't loop.

    Returns the pid (0 when the prompt file is missing — a deploy-drift
    guard: an empty prompt would burn a full session on nothing).
    """
    prompt_path = JARVIS_DIR / PROMPT_FILE
    try:
        prompt = prompt_path.read_text(encoding="utf-8").strip()
    except OSError:
        print(f"self-improve-cycle: prompt missing: {prompt_path}",
              file=sys.stderr)
        return 0
    if not prompt:
        return 0

    now = time.time() if now_epoch is None else float(now_epoch)
    _reconcile_unreleased(now)
    run_id = f"si-{int(now)}-{uuid.uuid4().hex[:8]}"
    digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    env = os.environ.copy()
    env["JARVIS_DIR"] = str(JARVIS_DIR)
    env["PYTHONPATH"] = str(JARVIS_DIR) + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")

    with _state_lock():
        # Admission and acquire are one critical section. Two heartbeat/manual
        # ticks may both observe "due" outside this function; only the first is
        # allowed to replace the current state and start a worker.
        previous = _read_state()
        if not _state_due(previous, now):
            return 0
        state = {
            "run_id": run_id,
            "status": "acquiring",
            "spawned_at": now,
            "acquire_epoch": now,
            "lease_expires_epoch": now + RUN_TIMEOUT_S + LEASE_GRACE_S,
            "release_epoch": 0,
            "pid": 0,
            "run_digest": digest,
            "consecutive_failures": int(
                previous.get("consecutive_failures") or 0),
        }
        _write_json(_state_path(), state)
        try:
            log = open(LOG_FILE, "a", encoding="utf-8")
            os.chmod(LOG_FILE, 0o600)
            log.write(
                f"\n===== self-improve cycle {run_id} acquired at "
                f"{time.ctime(now)} =====\n")
            log.flush()
            proc = popen(
                [sys.executable, "-m", "core.self_improve_cycle", "run", run_id],
                cwd=str(WORK_DIR), stdout=log, stderr=log,
                stdin=subprocess.DEVNULL, start_new_session=True, env=env)
        except (OSError, ValueError) as exc:
            state.update({
                "status": "spawn_failed",
                "release_epoch": now,
                "error_type": type(exc).__name__,
                "consecutive_failures": state["consecutive_failures"] + 1,
            })
            _write_json(_state_path(), state)
            _store_receipt_once(state)
            print(f"self-improve-cycle: spawn failed: {type(exc).__name__}",
                  file=sys.stderr)
            return 0
        finally:
            if "log" in locals():
                log.close()
        state["pid"] = int(getattr(proc, "pid", 0) or 0)
        state["status"] = "running"
        _write_json(_state_path(), state)
    return state["pid"]


def run_worker(run_id: str, *, run=subprocess.run,
               now_epoch: float | None = None) -> int:
    """Run the coding session and always leave a release receipt."""
    started = time.time()
    state = _admit_worker(
        run_id,
        time.time() if now_epoch is None else float(now_epoch),
    )
    if state is None:
        return 1
    prompt_path = JARVIS_DIR / PROMPT_FILE
    try:
        prompt = prompt_path.read_text(encoding="utf-8").strip()
    except OSError:
        prompt = ""
    stdout = ""
    stderr = ""
    error_type = ""
    try:
        if not prompt:
            raise ValueError("self-improve prompt missing or empty")
        from core.claude_bin import resolve_claude_bin
        result = run(
            [resolve_claude_bin(), "--dangerously-skip-permissions", "-p", prompt],
            cwd=str(WORK_DIR), capture_output=True, text=True,
            stdin=subprocess.DEVNULL, timeout=RUN_TIMEOUT_S)
        exit_code = int(result.returncode)
        stdout = str(result.stdout or "")
        stderr = str(result.stderr or "")
        status = ("succeeded" if exit_code == 0 and stdout.strip()
                  else "empty_success" if exit_code == 0 else "failed")
    except subprocess.TimeoutExpired as exc:
        exit_code = 124
        status = "timeout"
        stdout = str(exc.stdout or "")
        stderr = str(exc.stderr or "")
        error_type = type(exc).__name__
    except Exception as exc:
        exit_code = 1
        status = "failed"
        error_type = type(exc).__name__
        stderr = str(exc)

    try:
        with open(LOG_FILE, "a", encoding="utf-8") as log:
            os.chmod(LOG_FILE, 0o600)
            if stdout:
                log.write(stdout)
                if not stdout.endswith("\n"):
                    log.write("\n")
            if stderr:
                log.write(stderr)
                if not stderr.endswith("\n"):
                    log.write("\n")
            log.write(
                f"===== self-improve cycle {run_id} released: {status} =====\n")
    except OSError:
        pass

    released = (time.time() if now_epoch is None else float(now_epoch))
    receipt = {
        "run_id": run_id,
        "status": status,
        "acquire_epoch": float(state.get("acquire_epoch") or started),
        "release_epoch": released,
        "run_digest": str(state.get("run_digest") or hashlib.sha256(
            prompt.encode("utf-8")).hexdigest()),
        "output_digest": hashlib.sha256(stdout.encode("utf-8")).hexdigest(),
        "output_chars": len(stdout),
        "exit_code": exit_code,
        "error_type": error_type,
    }
    _record_release(receipt)
    try:
        from core.sched_events import emit
        emit(
            JARVIS_DIR,
            "llm_usage",
            task="self-improve-cycle",
            run_id=run_id,
            provider="claude",
            model="opus",
            duration_s=max(0.0, released - started),
            output_chars=len(stdout),
            usage_available=False,
            status=status,
        )
    except Exception:
        pass
    return 0 if status == "succeeded" else 1


def _reconcile_unreleased(now_epoch: float) -> None:
    state = _read_state()
    if str(state.get("status") or "") not in {"acquiring", "running"}:
        return
    alive = _pid_alive(int(state.get("pid") or 0))
    lease_expired = _lease_expired(state, now_epoch)
    if alive and not lease_expired:
        return
    run_id = str(state.get("run_id") or "")
    if not run_id:
        return
    if alive:
        identity = _pid_matches_run(int(state.get("pid") or 0), run_id)
        if identity is None:
            # `ps` failure is uncertainty, not evidence of PID reuse. Preserve
            # the fencing token and retry inspection on the next scheduler tick.
            state["identity_probe_failures"] = int(
                state.get("identity_probe_failures") or 0) + 1
            state["last_identity_probe_epoch"] = now_epoch
            _write_json(_state_path(), state)
            return
        if identity is True:
            terminated = _terminate_worker(state)
            if not terminated:
                # Keep ownership visible and block a second worker. The next
                # tick retries termination; health_line surfaces the impasse.
                state["termination_failures"] = int(
                    state.get("termination_failures") or 0) + 1
                state["last_termination_attempt_epoch"] = now_epoch
                _write_json(_state_path(), state)
                return
            error_type = "worker_lease_expired"
        else:
            error_type = "worker_lease_expired_pid_reused"
        status = "timeout"
    else:
        error_type = "missing_release_receipt"
        status = "interrupted"
    receipt = {
        "run_id": run_id,
        "status": status,
        "acquire_epoch": float(state.get("acquire_epoch") or
                               state.get("spawned_at") or 0),
        "release_epoch": now_epoch,
        "run_digest": str(state.get("run_digest") or ""),
        "output_digest": "",
        "output_chars": 0,
        "exit_code": -1,
        "error_type": error_type,
    }
    _record_release(receipt)


def tick(now_epoch: float | None = None) -> int:
    now = time.time() if now_epoch is None else float(now_epoch)
    _reconcile_unreleased(now)
    return spawn(now_epoch=now) if due(now) else 0


def health_line() -> str:
    state = _read_state()
    if int(state.get("identity_probe_failures") or 0) > 0:
        return ("⚠️ 自我改进后台任务已经超时，但系统暂时无法确认旧进程身份——"
                "已保留独占租约并继续复查")
    if int(state.get("termination_failures") or 0) > 0:
        return ("⚠️ 自我改进后台任务已经超时，但系统未能结束旧进程——"
                "已阻止新任务重叠，正在继续回收")
    failures = int(state.get("consecutive_failures") or 0)
    if failures < FAILURES_BEFORE_WARNING:
        return ""
    return (f"⚠️ 自我改进后台任务连续 {failures} 次没有正常收尾——"
            "系统会继续有界重试，先查本地自进化日志")


def main(argv: list[str]) -> int:
    if argv[:1] == ["tick"]:
        pid = tick()
        if pid:
            print(f"self-improve-cycle: spawned pid {pid}", file=sys.stderr)
        return 0
    if argv[:1] == ["run"] and len(argv) == 2:
        return run_worker(argv[1])
    if argv[:1] == ["health"]:
        line = health_line()
        if line:
            print(line)
        return 0
    print("usage: python3 -m core.self_improve_cycle [tick|run <id>|health]",
          file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
