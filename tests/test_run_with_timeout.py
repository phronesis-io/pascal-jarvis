import subprocess
import sys
import time
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
RUNNER = ROOT / "scripts" / "run_with_timeout.py"


def test_portable_timeout_runner_preserves_success_output():
    result = subprocess.run(
        [
            sys.executable, str(RUNNER), "2",
            sys.executable, "-c", "print('bounded-ok')",
        ],
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert result.returncode == 0
    assert result.stdout.strip() == "bounded-ok"


def test_portable_timeout_runner_terminates_slow_command():
    started = time.monotonic()
    result = subprocess.run(
        [
            sys.executable, str(RUNNER), "0.1",
            sys.executable, "-c", "import time; time.sleep(30)",
        ],
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert result.returncode == 124
    assert time.monotonic() - started < 4


def test_configured_short_grace_hard_kills_term_ignoring_command():
    started = time.monotonic()
    result = subprocess.run(
        [
            sys.executable, str(RUNNER), "0.1",
            sys.executable, "-c",
            "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)",
        ],
        env={**os.environ, "JARVIS_TIMEOUT_GRACE": "0.1"},
        timeout=3,
    )

    assert result.returncode == 124
    assert time.monotonic() - started < 2
