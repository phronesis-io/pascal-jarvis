"""Tests for core.heartbeat — parsing, scheduling, pipeline protection."""

import json
import time
from pathlib import Path

import pytest

from core.heartbeat import HeartbeatRunner, parse_heartbeat, parse_interval


def test_parse_interval():
    assert parse_interval("10s") == 10
    assert parse_interval("5m") == 300
    assert parse_interval("2h") == 7200
    assert parse_interval("1d") == 86400
    assert parse_interval("invalid") == 600  # default


def test_parse_heartbeat_basic(tmp_path):
    hb = tmp_path / "HEARTBEAT.md"
    hb.write_text("""
### task-one
- interval: 10m
- pre: tasks/pre.sh
- post: tasks/post.py
- prompt: Do something.

### task-two
- interval: 1h
- prompt: |
    Multiline prompt
    with two lines.
""")
    tasks = parse_heartbeat(hb)
    assert len(tasks) == 2
    assert tasks[0]["name"] == "task-one"
    assert tasks[0]["interval"] == 600
    assert tasks[0]["pre"] == "tasks/pre.sh"
    assert tasks[0]["post"] == "tasks/post.py"
    assert tasks[0]["prompt"] == "Do something."
    assert tasks[1]["interval"] == 3600
    assert "Multiline prompt" in tasks[1]["prompt"]
    assert "with two lines." in tasks[1]["prompt"]


def _make_runner(tmp_path, heartbeat_content: str, **kwargs) -> HeartbeatRunner:
    hb = tmp_path / "HEARTBEAT.md"
    hb.write_text(heartbeat_content)
    state_file = tmp_path / "state.json"
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    jarvis_dir = tmp_path / "jarvis"
    jarvis_dir.mkdir()
    return HeartbeatRunner(
        jarvis_dir=jarvis_dir,
        heartbeat_file=hb,
        state_file=state_file,
        memory_dir=memory_dir,
        model="sonnet",
        **kwargs,
    )


def test_state_persistence(tmp_path):
    runner = _make_runner(tmp_path, "### t\n- interval: 1h\n- prompt: hi\n")
    runner.save_state({"t": {"last_run": 12345}})
    assert runner.load_state() == {"t": {"last_run": 12345}}


def test_no_task_due_returns_empty(tmp_path, monkeypatch):
    runner = _make_runner(tmp_path, "### t\n- interval: 1h\n- prompt: hi\n")
    now = int(time.time())
    runner.save_state({"t": {"last_run": now}})  # just ran
    result = runner.run_cycle(force=False)
    assert result == ""


def test_only_task_filter(tmp_path, monkeypatch):
    """only_task parameter should limit cycle to one named task."""
    runner = _make_runner(tmp_path,
        "### task-a\n- interval: 1h\n- prompt: a\n\n"
        "### task-b\n- interval: 1h\n- prompt: b\n"
    )
    called_with = []
    monkeypatch.setattr(runner, "claude_call", lambda p: called_with.append(p) or "HEARTBEAT_OK")
    runner.run_cycle(force=True, only_task="task-a")
    assert len(called_with) == 1
    assert "task-a" in called_with[0]
    assert "task-b" not in called_with[0]


def test_pipeline_protection_one_memory_task_per_cycle(tmp_path, monkeypatch):
    """Only one of the PIPELINE_TASKS may run per cycle to prevent races."""
    hb = """
### memory-hourly
- interval: 1h
- prompt: h

### memory-daily
- interval: 12h
- prompt: d
"""
    runner = _make_runner(tmp_path, hb)
    called_with = []
    monkeypatch.setattr(runner, "claude_call", lambda p: called_with.append(p) or "HEARTBEAT_OK")

    runner.run_cycle(force=True)
    assert len(called_with) == 1
    prompt = called_with[0]
    # First pipeline task encountered should be the only one picked
    assert "memory-hourly" in prompt
    assert "memory-daily" not in prompt


def test_empty_pre_script_output_skips_task(tmp_path, monkeypatch):
    """If pre-script returns empty, task is skipped with shortened retry."""
    hb = """
### checkin
- interval: 1h
- pre: nonexistent.sh
- prompt: hi
"""
    runner = _make_runner(tmp_path, hb)
    called = []
    monkeypatch.setattr(runner, "claude_call", lambda p: called.append(p) or "x")

    runner.run_cycle(force=True)
    assert called == []  # Claude not called
    state = runner.load_state()
    # Retry delay should be applied (last_run < now but > 0)
    assert "checkin" in state


def test_heartbeat_ok_updates_state(tmp_path, monkeypatch):
    runner = _make_runner(tmp_path, "### t\n- interval: 1h\n- prompt: hi\n")
    monkeypatch.setattr(runner, "claude_call", lambda p: "HEARTBEAT_OK")
    runner.run_cycle(force=True)
    state = runner.load_state()
    assert "t" in state
    assert state["t"]["last_run"] > 0


def test_work_dir_defaults_to_jarvis_dir(tmp_path):
    runner = _make_runner(tmp_path, "### t\n- interval: 1h\n- prompt: hi\n")
    assert runner.work_dir == runner.jarvis_dir


def test_work_dir_explicit(tmp_path):
    work = tmp_path / "custom_work"
    work.mkdir()
    runner = _make_runner(tmp_path, "### t\n- interval: 1h\n- prompt: hi\n",
                          work_dir=work)
    assert runner.work_dir == work


def test_multi_task_envelope_parsing(tmp_path, monkeypatch):
    """When >1 tasks run, Claude returns a JSON envelope — route responses correctly."""
    hb = """
### task-a
- interval: 1h
- prompt: a

### task-b
- interval: 1h
- prompt: b
"""
    runner = _make_runner(tmp_path, hb)
    envelope = json.dumps({
        "tasks": {
            "task-a": "response for a",
            "task-b": "response for b",
        },
        "user_message": "",
    })
    monkeypatch.setattr(runner, "claude_call", lambda p: envelope)

    result = runner.run_cycle(force=True)
    assert "response for a" in result
    assert "response for b" in result
