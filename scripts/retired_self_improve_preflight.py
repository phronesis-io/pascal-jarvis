#!/usr/bin/env python3
"""Fail closed when a retired unattended coding worker may still be alive."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable


ACTIVE_STATUSES = {"acquiring", "running", "termination_failed"}
TERMINAL_STATUSES = {
    "failed",
    "interrupted",
    "spawn_failed",
    "succeeded",
    "timeout",
    "workspace_cleanup_failed",
}
KNOWN_STATUSES = ACTIVE_STATUSES | TERMINAL_STATUSES
PROCESS_MARKERS = ("core.self_improve_cycle", "jarvis-harness-worker")
LAUNCHD_PREFIX = "com.pascal.jarvis.harness."
Runner = Callable[..., subprocess.CompletedProcess[str]]


def _run(runner: Runner, command: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return runner(
            command,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            env={"PATH": "/usr/bin:/bin"},
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return subprocess.CompletedProcess(command, 127, "", type(exc).__name__)


def inspect_retired_worker(
    root: str | Path,
    *,
    runner: Runner = subprocess.run,
    platform: str = sys.platform,
) -> dict[str, Any]:
    """Return read-only evidence; uncertainty blocks a governed restart."""
    project = Path(root).expanduser().resolve()
    state_path = project / "data" / "self_improve_cycle.json"
    issues: list[str] = []
    state: dict[str, Any] = {}
    if state_path.exists():
        try:
            loaded = json.loads(state_path.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict):
                raise ValueError("state is not an object")
            state = loaded
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            issues.append(f"legacy state unreadable:{type(exc).__name__}")

    status = str(state.get("status") or "")
    if state_path.exists() and status not in KNOWN_STATUSES:
        issues.append(f"legacy state status is unknown:{status or '<empty>'}")
    if status in ACTIVE_STATUSES:
        issues.append(f"legacy run is unresolved:{status}")

    recorded_pids: set[int] = set()
    for key in ("pid", "harness_pid"):
        raw_pid = state.get(key)
        try:
            pid = int(raw_pid or 0)
        except (TypeError, ValueError):
            pid = 0
            issues.append(f"legacy state {key} is invalid")
        if raw_pid not in (None, "", 0, "0") and pid <= 0:
            issues.append(f"legacy state {key} is invalid")
        if pid > 0:
            recorded_pids.add(pid)
    raw_processes = state.get("harness_processes")
    if raw_processes not in (None, []) and not isinstance(raw_processes, list):
        issues.append("legacy state harness_processes is invalid")
        raw_processes = []
    for item in raw_processes or []:
        if not isinstance(item, dict):
            issues.append("legacy harness process entry is invalid")
            continue
        raw_pid = item.get("pid")
        try:
            pid = int(raw_pid or 0)
        except (TypeError, ValueError):
            pid = 0
        if raw_pid in (None, "", 0, "0") or pid <= 0:
            issues.append("legacy harness process pid is invalid")
        if pid > 0:
            recorded_pids.add(pid)

    process_result = _run(runner, ["/bin/ps", "ax", "-o", "pid=,command="])
    if process_result.returncode != 0:
        issues.append("cannot inspect process table")
    else:
        parsed_processes = 0
        for raw_line in process_result.stdout.splitlines():
            match = re.match(r"\s*([1-9][0-9]*)\s+(.*)", raw_line)
            if not match:
                continue
            parsed_processes += 1
            pid, command = int(match.group(1)), match.group(2)
            if pid in recorded_pids:
                issues.append(f"recorded legacy pid is alive:{pid}")
            if any(marker in command for marker in PROCESS_MARKERS):
                issues.append(f"legacy mutation process is alive:{pid}")
        if parsed_processes == 0:
            issues.append("process table inspection returned no parseable rows")

    if platform == "darwin":
        launchd = _run(runner, ["/bin/launchctl", "print", f"gui/{os.getuid()}"])
        if launchd.returncode != 0:
            issues.append("cannot inspect launchd domain")
        else:
            expected_header = f"gui/{os.getuid()} = {{"
            if expected_header not in launchd.stdout:
                issues.append("launchd inspection returned unparseable output")
            elif LAUNCHD_PREFIX in launchd.stdout:
                issues.append("legacy harness launchd job is loaded")

    return {
        "ok": not issues,
        "state_path": str(state_path),
        "state_status": status,
        "issues": list(dict.fromkeys(issues)),
        "remediation": (
            "Return to the pre-retirement revision and run its reconciler, or "
            "independently prove the recorded worker/job is absent before "
            "archiving the legacy state. Do not delete the state as proof."
            if issues else ""
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check that the retired self-improve worker is quiescent"
    )
    parser.add_argument("--root", default=os.environ.get("JARVIS_DIR") or ".")
    args = parser.parse_args(argv)
    result = inspect_retired_worker(args.root)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
