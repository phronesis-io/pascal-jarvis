"""Tests for HeartbeatRunner force cooldown + atomic state writes."""

import json
import time
from pathlib import Path

from core.heartbeat import HeartbeatRunner


def _make_runner(tmp_path):
    hb = tmp_path / "HEARTBEAT.md"
    hb.write_text("### memory-hourly\n- interval: 1h\n- prompt: hi\n")
    return HeartbeatRunner(
        jarvis_dir=tmp_path,
        heartbeat_file=hb,
        state_file=tmp_path / "state.json",
        memory_dir=tmp_path / "memory",
        model="sonnet",
    )


def test_force_within_cooldown_skips(tmp_path, monkeypatch):
    runner = _make_runner(tmp_path)
    called = []
    monkeypatch.setattr(runner, "claude_call", lambda p: called.append(p) or "HEARTBEAT_OK")

    # First force triggers
    runner.run_cycle(force=True)
    assert len(called) == 1

    # Second force immediately after should be blocked by cooldown
    called.clear()
    runner.run_cycle(force=True)
    assert called == []


def test_force_after_cooldown_fires(tmp_path, monkeypatch):
    runner = _make_runner(tmp_path)
    # Seed state with last_run far in the past (more than cooldown)
    runner.save_state({"memory-hourly": {"last_run": 0}})

    called = []
    monkeypatch.setattr(runner, "claude_call", lambda p: called.append(p) or "HEARTBEAT_OK")
    runner.run_cycle(force=True)
    assert len(called) == 1


def test_state_save_is_atomic(tmp_path):
    runner = _make_runner(tmp_path)
    runner.save_state({"x": {"last_run": 42}})
    # tmp file should not linger
    tmp_marker = runner.state_file.with_suffix(runner.state_file.suffix + ".tmp")
    assert not tmp_marker.exists()
    assert json.loads(runner.state_file.read_text()) == {"x": {"last_run": 42}}
