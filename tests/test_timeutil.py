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
from zoneinfo import ZoneInfo

import pytest

import core.timeutil as timeutil
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

    log = (tmp_path / "timeline" / "hourly_log.md").read_text(encoding="utf-8")
    # Extract the "### YYYY-MM-DD HH:MM" header
    m = re.search(r"### (\d{4}-\d{2}-\d{2} \d{2}:\d{2})", log)
    assert m, f"no header found in log: {log!r}"
    written_hour = int(m.group(1).split(" ")[1].split(":")[0])
    assert written_hour == t_local.hour, (
        f"wrote hour={written_hour} but system-local expected {t_local.hour} "
        f"(UTC was {utc_hour}) — timeutil did not override TZ env correctly"
    )


# ---------------------------------------------------------------------------
# Mid-process system timezone changes (2026-07-10 incident).
#
# The production bug these guard against: the timezone was detected once at
# import, so a heartbeat started under Atlantic/Reykjavik kept writing
# Reykjavik timestamps (8h behind) for hours after the Mac had switched back
# to Asia/Shanghai — quiet hours, batching windows, injected 'Current time'
# and every log ts were all skewed until restart. now_local() must follow
# /etc/localtime within the cache TTL.
# ---------------------------------------------------------------------------


@pytest.fixture
def _restore_tz_cache():
    """Save/restore timeutil's module-level tz cache around a test."""
    saved = (timeutil._TZ_NAME, timeutil._LOCAL_TZ, timeutil._tz_checked_at)
    yield
    timeutil._TZ_NAME, timeutil._LOCAL_TZ, timeutil._tz_checked_at = saved


def _expire_tz_cache():
    """Age the cache stamp so the next call re-detects (simulates TTL expiry)."""
    timeutil._tz_checked_at = (
        time.monotonic() - timeutil._TZ_CACHE_TTL_SECONDS - 1
    )


def test_follows_system_tz_change_after_ttl(monkeypatch, _restore_tz_cache):
    """Simulate travel: /etc/localtime re-pointed while the process runs.

    After the TTL expires, now_local() and system_tz_name() must reflect the
    new zone without a process restart.
    """
    # Start in Reykjavik (UTC+0 year-round)
    monkeypatch.setattr(timeutil, "_detect_system_tz_name",
                        lambda: "Atlantic/Reykjavik")
    timeutil._refresh_local_tz(force=True)
    assert system_tz_name() == "Atlantic/Reykjavik"
    ref = datetime.now(timezone.utc)
    assert now_local().utcoffset() == ref.astimezone(
        ZoneInfo("Atlantic/Reykjavik")).utcoffset()

    # OS switches back to Shanghai (UTC+8) — 2026-07-10 scenario
    monkeypatch.setattr(timeutil, "_detect_system_tz_name",
                        lambda: "Asia/Shanghai")
    _expire_tz_cache()
    t = now_local()
    assert timeutil._TZ_NAME == "Asia/Shanghai"
    assert t.utcoffset() == ref.astimezone(ZoneInfo("Asia/Shanghai")).utcoffset()
    assert system_tz_name() == "Asia/Shanghai"


def test_ttl_avoids_redetect_on_every_call(monkeypatch, _restore_tz_cache):
    """Within the TTL the symlink is NOT re-resolved (hot-loop cheapness)."""
    monkeypatch.setattr(timeutil, "_detect_system_tz_name",
                        lambda: "Asia/Shanghai")
    timeutil._refresh_local_tz(force=True)

    calls = []

    def _counting_detect():
        calls.append(1)
        return "America/New_York"

    monkeypatch.setattr(timeutil, "_detect_system_tz_name", _counting_detect)
    # Cache is fresh (just refreshed) → no re-detection, old zone kept
    now_local()
    now_local()
    assert calls == []
    assert timeutil._TZ_NAME == "Asia/Shanghai"

    # Once the TTL expires the new zone is picked up
    _expire_tz_cache()
    now_local()
    assert calls == [1]
    assert timeutil._TZ_NAME == "America/New_York"


def test_transient_detection_failure_keeps_last_known_good(
        monkeypatch, _restore_tz_cache):
    """If /etc/localtime is momentarily unreadable, keep the cached zone
    instead of degrading to naive datetimes (which follow a possibly
    polluted TZ env)."""
    monkeypatch.setattr(timeutil, "_detect_system_tz_name",
                        lambda: "Asia/Shanghai")
    timeutil._refresh_local_tz(force=True)

    monkeypatch.setattr(timeutil, "_detect_system_tz_name", lambda: None)
    timeutil._refresh_local_tz(force=True)
    assert timeutil._TZ_NAME == "Asia/Shanghai"
    assert timeutil._LOCAL_TZ is not None
    assert now_local().tzinfo is not None


def test_msg_timestamp_prefix_follows_tz_change(monkeypatch, _restore_tz_cache):
    """The dual-display (abroad) vs single-line (home) decision must be based
    on the CURRENT zone, not the one cached at import."""
    monkeypatch.setattr(timeutil, "_detect_system_tz_name",
                        lambda: "Atlantic/Reykjavik")
    timeutil._refresh_local_tz(force=True)
    abroad = timeutil.msg_timestamp_prefix()
    assert "当地" in abroad and "上海" in abroad

    monkeypatch.setattr(timeutil, "_detect_system_tz_name",
                        lambda: "Asia/Shanghai")
    _expire_tz_cache()
    home = timeutil.msg_timestamp_prefix()
    assert "当地" not in home
    assert re.match(r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2} 周.\]$", home)
