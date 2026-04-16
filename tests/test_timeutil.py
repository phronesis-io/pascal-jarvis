"""Tests for core.timeutil — system-local time detection must be robust
to subprocess TZ env corruption.

The production bug this guards against: a memory-hourly entry wrote
'05:49' to hourly_log.md when the user's wall clock said '13:49'. Root
cause: some subprocess had TZ=UTC (or no TZ), so datetime.now() returned
UTC-equivalent time instead of local.
"""

import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from core.timeutil import now_local, now_local_str, system_tz_name


def test_detects_system_tz():
    """On a typical dev machine, /etc/localtime should resolve to an IANA name."""
    tz = system_tz_name()
    # This may be None on weird systems (Docker containers, CI, etc) —
    # we don't fail the test, we just record what was found.
    if tz is not None:
        assert "/" in tz or tz in ("UTC", "GMT"), f"unexpected tz name: {tz!r}"


def test_now_local_returns_datetime():
    t = now_local()
    assert isinstance(t, datetime)


def test_now_local_str_format():
    s = now_local_str("%Y-%m-%d %H:%M")
    assert re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$", s), f"bad format: {s!r}"


def test_now_local_custom_format():
    s = now_local_str("%H:%M")
    assert re.match(r"^\d{2}:\d{2}$", s)


def test_ignores_TZ_env_when_possible():
    """If /etc/localtime resolution succeeded, TZ=UTC in the current process
    should NOT force the helper to return UTC.

    Uses a subprocess with TZ=UTC to simulate a misconfigured launchd/cron
    environment — the helper should still produce system-local time.
    """
    if system_tz_name() is None:
        pytest.skip("no /etc/localtime — cannot guarantee TZ-env independence")

    # Get expected hour in local tz vs UTC
    t_local = now_local()
    utc_hour = datetime.now(timezone.utc).hour

    # If local tz happens to equal UTC (e.g. running in London winter), skip:
    # we can't distinguish correctness in that case
    if t_local.hour == utc_hour:
        pytest.skip("local tz equals UTC — ambiguous test case")

    repo_root = Path(__file__).resolve().parent.parent
    env = {**os.environ, "TZ": "UTC", "PYTHONPATH": str(repo_root)}
    result = subprocess.run(
        [sys.executable, "-c",
         "from core.timeutil import now_local_str; print(now_local_str('%H'))"],
        capture_output=True, text=True, env=env,
    )
    assert result.returncode == 0, result.stderr
    hour_from_subprocess = int(result.stdout.strip())
    # The subprocess, even with TZ=UTC, should return local hour (not UTC)
    assert hour_from_subprocess == t_local.hour, (
        f"subprocess saw hour={hour_from_subprocess}, "
        f"expected local hour={t_local.hour} (UTC was {utc_hour})"
    )


def test_memory_hourly_post_writes_local_tz_under_TZ_UTC(tmp_path):
    """End-to-end: run memory_hourly_post.py with TZ=UTC and verify the
    written timestamp matches system-local time, not UTC."""
    if system_tz_name() is None:
        pytest.skip("no /etc/localtime — test cannot verify TZ-independence")

    t_local = now_local()
    utc_hour = datetime.now(timezone.utc).hour
    if t_local.hour == utc_hour:
        pytest.skip("local tz equals UTC — test cannot distinguish")

    repo_root = Path(__file__).resolve().parent.parent
    script = repo_root / "tasks" / "memory_hourly_post.py"
    env = {
        **os.environ,
        "TZ": "UTC",
        "MEMORY_DIR": str(tmp_path),
    }
    result = subprocess.run(
        [sys.executable, str(script)],
        input="- 今天有意义的一件小事\n- 另一条索引信息就够了",
        capture_output=True, text=True, env=env,
    )
    assert result.returncode == 0, result.stderr

    log = (tmp_path / "hourly_log.md").read_text(encoding="utf-8")
    # Extract the "### YYYY-MM-DD HH:MM" header
    m = re.search(r"### (\d{4}-\d{2}-\d{2} \d{2}:\d{2})", log)
    assert m, f"no header found in log: {log!r}"
    written_hour = int(m.group(1).split(" ")[1].split(":")[0])
    assert written_hour == t_local.hour, (
        f"wrote hour={written_hour} but system-local expected {t_local.hour} "
        f"(UTC was {utc_hour}) — timeutil did not override TZ env correctly"
    )
