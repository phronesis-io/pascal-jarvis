#!/usr/bin/env python3
"""Run a command with a portable wall-clock timeout.

macOS does not ship GNU ``timeout``. This helper gives shell hooks one shared
TERM-then-KILL implementation and forwards shutdown signals so a supervised
task cannot leave the bounded command behind.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys


def _terminate(process: subprocess.Popen, grace: float | None = None) -> None:
    if grace is None:
        try:
            grace = max(0.0, float(os.environ.get("JARVIS_TIMEOUT_GRACE", "2")))
        except ValueError:
            grace = 2
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=grace)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        pass


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) < 2:
        print("usage: run_with_timeout.py SECONDS COMMAND [ARG ...]",
              file=sys.stderr)
        return 2
    try:
        timeout = float(args.pop(0))
    except ValueError:
        print("timeout must be a number", file=sys.stderr)
        return 2
    if timeout <= 0:
        print("timeout must be positive", file=sys.stderr)
        return 2

    process = subprocess.Popen(args, start_new_session=True)

    def stop(signum: int, _frame) -> None:
        _terminate(process)
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    try:
        return process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        _terminate(process)
        return 124


if __name__ == "__main__":
    raise SystemExit(main())
