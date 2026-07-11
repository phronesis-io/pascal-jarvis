"""Hard timeout regression: descendants must not keep capture pipes alive."""

import os
import subprocess
import sys
import time

import pytest

from core.heartbeat import _run_isolated


def test_timeout_kills_descendants_holding_stdio(tmp_path):
    pidfile = tmp_path / "child.pid"
    child_code = (
        "import signal,time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "time.sleep(30)"
    )
    parent_code = (
        "import pathlib,subprocess,sys,time; "
        f"p=subprocess.Popen([sys.executable,'-c',{child_code!r}]); "
        f"pathlib.Path({str(pidfile)!r}).write_text(str(p.pid)); "
        "time.sleep(30)"
    )

    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        _run_isolated([sys.executable, "-c", parent_code], timeout=0.2)
    assert time.monotonic() - started < 3

    child_pid = int(pidfile.read_text())
    for _ in range(20):
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        pytest.fail("timed-out descendant survived the process-group kill")
