"""Descriptor-safe rotation for logs owned by launchd-supervised services."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


Runner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class ManagedLog:
    label: str
    paths: tuple[Path, ...]
    optional: bool = False

    @property
    def plist(self) -> Path:
        return Path.home() / "Library" / "LaunchAgents" / f"{self.label}.plist"


def managed_logs() -> tuple[ManagedLog, ...]:
    return (
        ManagedLog(
            "com.pascal.jarvis.taskline",
            (Path("/tmp/jarvis-taskline.log"),),
            optional=True,
        ),
        ManagedLog(
            "com.pascal.jarvis.daemon",
            (
                Path("/tmp/jarvis-daemon-stdout.log"),
                Path("/tmp/jarvis-daemon-stderr.log"),
            ),
        ),
        ManagedLog(
            "com.jarvis.conversation-audit",
            (Path("/tmp/jarvis-conversation-audit.log"),),
        ),
        ManagedLog(
            "com.jarvis.session-backup",
            (Path("/tmp/jarvis-session-backup.log"),),
        ),
    )


def _run(
    runner: Runner,
    command: list[str],
    *,
    timeout: float = 15,
) -> subprocess.CompletedProcess[str]:
    return runner(
        command,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _shift_generations(path: Path, keep: int) -> None:
    if keep < 1:
        path.unlink(missing_ok=True)
        path.touch()
        return
    Path(f"{path}.{keep}").unlink(missing_ok=True)
    for generation in range(keep - 1, 0, -1):
        old = Path(f"{path}.{generation}")
        if old.exists():
            old.replace(Path(f"{path}.{generation + 1}"))
    if path.exists():
        path.replace(Path(f"{path}.1"))
    path.touch()


def _service_is_explicitly_absent(
    result: subprocess.CompletedProcess[str],
) -> bool:
    if result.returncode == 0:
        return False
    detail = f"{result.stderr or ''}\n{result.stdout or ''}".casefold()
    return any(
        marker in detail
        for marker in (
            "could not find service",
            "service cannot be found",
        )
    )


def _restore_loaded(runner: Runner, domain: str, target: str, plist: Path) -> str:
    """Best-effort bootstrap after a bootout attempt; return a recovery error."""
    bootstrap_error = ""
    try:
        started = _run(runner, ["launchctl", "bootstrap", domain, str(plist)])
        if started.returncode != 0:
            bootstrap_error = (
                started.stderr or started.stdout or "bootstrap failed"
            ).strip()[:240]
    except (OSError, subprocess.SubprocessError) as exc:
        bootstrap_error = str(exc)[:240]
    try:
        loaded = _run(runner, ["launchctl", "print", target])
        if loaded.returncode == 0:
            return ""
    except (OSError, subprocess.SubprocessError) as exc:
        if not bootstrap_error:
            bootstrap_error = str(exc)[:240]
    return bootstrap_error or "service is not loaded after recovery"


def rotate_managed_log(
    spec: ManagedLog,
    *,
    max_bytes: int = 5_000_000,
    keep: int = 3,
    uid: int | None = None,
    runner: Runner = subprocess.run,
) -> dict:
    """Rotate one service only after launchd has closed its append descriptors."""
    sizes = {
        str(path): path.stat().st_size if path.exists() else 0
        for path in spec.paths
    }
    if max(sizes.values(), default=0) <= max_bytes:
        return {
            "label": spec.label,
            "status": "below_threshold",
            "sizes": sizes,
        }
    domain = f"gui/{uid if uid is not None else os.getuid()}"
    target = f"{domain}/{spec.label}"
    if not spec.plist.exists() and not spec.optional:
        return {
            "label": spec.label,
            "status": "missing_plist",
            "ok": False,
            "sizes": sizes,
        }
    try:
        loaded = _run(runner, ["launchctl", "print", target])
    except (OSError, subprocess.SubprocessError) as exc:
        return {
            "label": spec.label,
            "status": "probe_failed",
            "ok": False,
            "detail": str(exc)[:240],
            "sizes": sizes,
        }
    if not spec.plist.exists():
        if loaded.returncode == 0:
            return {
                "label": spec.label,
                "status": "optional_missing_plist_loaded",
                "ok": False,
                "sizes": sizes,
            }
        if not _service_is_explicitly_absent(loaded):
            detail = (
                loaded.stderr or loaded.stdout or "launchctl print failed"
            ).strip()
            return {
                "label": spec.label,
                "status": "probe_failed",
                "ok": False,
                "detail": detail[:240],
                "sizes": sizes,
            }
        try:
            for path in spec.paths:
                _shift_generations(path, keep)
        except OSError as exc:
            return {
                "label": spec.label,
                "status": "optional_absent_rotation_failed",
                "ok": False,
                "detail": str(exc)[:240],
                "sizes": sizes,
            }
        return {
            "label": spec.label,
            "status": "optional_absent_rotated",
            "ok": True,
            "sizes": sizes,
        }
    if loaded.returncode != 0:
        return {
            "label": spec.label,
            "status": "not_loaded",
            "ok": False,
            "sizes": sizes,
        }

    try:
        stopped = _run(runner, ["launchctl", "bootout", target])
    except (OSError, subprocess.SubprocessError) as exc:
        recovery_error = _restore_loaded(runner, domain, target, spec.plist)
        return {
            "label": spec.label,
            "status": (
                "stop_failed_recovered"
                if not recovery_error
                else "stop_failed_recovery_failed"
            ),
            "ok": False,
            "detail": str(exc)[:240],
            "recovery_error": recovery_error,
            "sizes": sizes,
        }
    if stopped.returncode != 0:
        detail = (stopped.stderr or stopped.stdout or "bootout failed").strip()
        recovery_error = _restore_loaded(runner, domain, target, spec.plist)
        return {
            "label": spec.label,
            "status": (
                "stop_failed_recovered"
                if not recovery_error
                else "stop_failed_recovery_failed"
            ),
            "ok": False,
            "detail": detail[:240],
            "recovery_error": recovery_error,
            "sizes": sizes,
        }

    rotation_error = ""
    try:
        for path in spec.paths:
            _shift_generations(path, keep)
    except OSError as exc:
        rotation_error = str(exc)

    recovery_error = _restore_loaded(runner, domain, target, spec.plist)
    if recovery_error:
        return {
            "label": spec.label,
            "status": "restart_failed",
            "ok": False,
            "detail": recovery_error,
            "rotation_error": rotation_error,
            "sizes": sizes,
        }
    if rotation_error:
        return {
            "label": spec.label,
            "status": "rotation_failed_restarted",
            "ok": False,
            "detail": rotation_error,
            "sizes": sizes,
        }
    return {
        "label": spec.label,
        "status": "rotated",
        "ok": True,
        "sizes": sizes,
    }


def maintain_logs(
    *,
    max_bytes: int = 5_000_000,
    keep: int = 3,
    specs: tuple[ManagedLog, ...] | None = None,
    runner: Runner = subprocess.run,
    lock_path: Path = Path("/tmp/jarvis-log-maintenance.lock"),
) -> dict:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"ok": True, "status": "already_running", "results": []}
        results = [
            rotate_managed_log(
                spec,
                max_bytes=max_bytes,
                keep=keep,
                runner=runner,
            )
            for spec in (specs or managed_logs())
        ]
    return {
        "ok": all(item.get("ok", True) for item in results),
        "status": "complete",
        "results": results,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Safely rotate Jarvis service logs")
    parser.add_argument("--max-bytes", type=int, default=5_000_000)
    parser.add_argument("--keep", type=int, default=3)
    args = parser.parse_args(argv)
    result = maintain_logs(max_bytes=max(1, args.max_bytes), keep=max(1, args.keep))
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
