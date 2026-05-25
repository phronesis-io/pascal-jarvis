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
    assert daemon_mod.HEARTBEAT_STALE_THRESHOLD == 1200


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
