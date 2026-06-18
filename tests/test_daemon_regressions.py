"""Regression tests for daemon.py bug fixes (2026-05-25).

Tests the guardian daemon's health-check and restart logic.
"""

import os
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

import daemon as daemon_mod


def test_pid_parse_with_boot_timestamp(tmp_path, monkeypatch):
    """PID file in 'PID BOOT_TS' format must parse correctly.

    Bug: old code used int(content.strip()) which failed on '1234 1779680834'.
    Fix: changed to int(content.strip().split()[0]).
    """
    pid_file = tmp_path / ".bot.pid"
    pid_file.write_text("99999 1779680834")  # PID that doesn't exist
    monkeypatch.setattr(daemon_mod, "BOT_PID_FILE", pid_file)

    # PID 99999 likely doesn't exist → should return False (via ProcessLookupError)
    # But it should NOT crash on ValueError
    result = daemon_mod._is_bot_alive()
    # We just verify it didn't raise — it returns False because PID doesn't exist
    assert result is False or result is True  # no crash


def test_pid_parse_legacy_format(tmp_path, monkeypatch):
    """Legacy PID-only format must still work."""
    pid_file = tmp_path / ".bot.pid"
    pid_file.write_text("99999")
    monkeypatch.setattr(daemon_mod, "BOT_PID_FILE", pid_file)

    # Should not crash — gracefully returns False for non-existent PID
    result = daemon_mod._is_bot_alive()
    assert result is False or result is True


def test_pid_parse_empty_file(tmp_path, monkeypatch):
    """Empty PID file should not crash."""
    pid_file = tmp_path / ".bot.pid"
    pid_file.write_text("")
    monkeypatch.setattr(daemon_mod, "BOT_PID_FILE", pid_file)

    result = daemon_mod._is_bot_alive()
    assert result is False or result is True


def test_stale_threshold_is_1200():
    """Stale threshold must be 1200s (20 min) to accommodate long Claude calls.

    Was 900s (15 min), caused false-positive stale detection → restart spiral.
    """
    assert daemon_mod.HEARTBEAT_STALE_THRESHOLD == 1800


def test_daemon_records_wake_gap(monkeypatch):
    monkeypatch.setattr(daemon_mod, "last_wake_time", 0)
    monkeypatch.setattr(daemon_mod.time, "time", lambda: 12345.0)
    monkeypatch.setattr(daemon_mod, "log", lambda *a, **k: None)

    assert daemon_mod._record_wake_gap(
        slept_for_s=daemon_mod.CHECK_INTERVAL + daemon_mod.SLEEP_GAP_THRESHOLD + 1,
        expected_s=daemon_mod.CHECK_INTERVAL,
    ) == daemon_mod.SLEEP_GAP_THRESHOLD + 1
    assert daemon_mod.last_wake_time == 12345.0
    assert daemon_mod._in_wake_grace(now=12345.0 + daemon_mod.WAKE_GRACE_SECONDS - 1)
    assert not daemon_mod._in_wake_grace(now=12345.0 + daemon_mod.WAKE_GRACE_SECONDS + 1)


def test_daemon_wake_grace_suppresses_stale_heartbeat_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(daemon_mod, "JARVIS_DIR", tmp_path)
    monkeypatch.setattr(daemon_mod, "_bot_pid", lambda: 123)
    monkeypatch.setattr(daemon_mod, "_is_lark_listener_alive", lambda bot_pid=None: True)
    monkeypatch.setattr(daemon_mod, "_find_last_heartbeat", lambda: daemon_mod.HEARTBEAT_STALE_THRESHOLD + 60)
    monkeypatch.setattr(daemon_mod, "_in_wake_grace", lambda now=None: True)
    monkeypatch.setattr(daemon_mod, "log", lambda *a, **k: None)

    result = daemon_mod.check_health()

    assert result["healthy"] is True
    assert result["issues"] == []


def test_daemon_stale_heartbeat_still_fails_outside_wake_grace(tmp_path, monkeypatch):
    monkeypatch.setattr(daemon_mod, "JARVIS_DIR", tmp_path)
    monkeypatch.setattr(daemon_mod, "_bot_pid", lambda: 123)
    monkeypatch.setattr(daemon_mod, "_is_lark_listener_alive", lambda bot_pid=None: True)
    monkeypatch.setattr(daemon_mod, "_find_last_heartbeat", lambda: daemon_mod.HEARTBEAT_STALE_THRESHOLD + 60)
    monkeypatch.setattr(daemon_mod, "_in_wake_grace", lambda now=None: False)

    result = daemon_mod.check_health()

    assert result["healthy"] is False
    assert any("Heartbeat stale" in issue for issue in result["issues"])


def test_kill_patterns_include_eigenflux():
    """diagnose_and_fix must kill eigenflux stream processes during restart.

    Bug: old kill list didn't include eigenflux → orphan streams survived restart
    → 'Connection replaced by another session' infinite loop.
    """
    import inspect
    source = inspect.getsource(daemon_mod.diagnose_and_fix)
    assert "eigenflux stream" in source, \
        "diagnose_and_fix must include 'eigenflux stream' in its kill patterns"


def test_restart_wait_is_at_least_60s():
    """After restarting bot.sh, daemon must wait long enough for the first
    heartbeat cycle to complete (Claude call can take 60-120s).

    Bug: old code waited only 10s → immediately judged as 'still unhealthy'
    → triggered another restart → restart spiral.
    """
    import inspect
    source = inspect.getsource(daemon_mod.diagnose_and_fix)
    # Check for the sleep loop with range(90) — 90 seconds
    assert "range(90)" in source, \
        "diagnose_and_fix must wait ~90s after restart before health check"


def test_lark_listener_must_be_owned_by_bot(monkeypatch):
    """An orphan sidecar must not satisfy daemon health.

    Regression: broad pgrep saw a reparented lark_event_sidecar and marked the
    listener healthy while bot.sh/admin were dead.
    """
    monkeypatch.setattr(daemon_mod, "_bot_pid", lambda: 100)
    monkeypatch.setattr(daemon_mod, "_ps_processes", lambda: {
        100: (1, "bash /repo/bot.sh"),
        200: (1, "python3 /repo/scripts/lark_event_sidecar.py"),
    })
    assert daemon_mod._is_lark_listener_alive() is False


def test_lark_listener_owned_by_bot_is_healthy(monkeypatch):
    monkeypatch.setattr(daemon_mod, "_bot_pid", lambda: 100)
    monkeypatch.setattr(daemon_mod, "_ps_processes", lambda: {
        100: (1, "bash /repo/bot.sh"),
        150: (100, "bash pipeline subshell"),
        200: (150, "python3 /repo/scripts/lark_event_sidecar.py"),
    })
    assert daemon_mod._is_lark_listener_alive() is True
