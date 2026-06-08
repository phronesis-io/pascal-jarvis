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
    kwargs.setdefault("idle_judge", False)  # never hit the network in unit tests
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
    """Only one of the PIPELINE_TASKS may run per cycle to prevent races.
    memory-hourly is a PRIORITY_TASK (Tier 0) so it bypasses Claude.
    memory-daily would go to Claude but is blocked by pipeline protection."""
    hb = """
### memory-hourly
- interval: 1h
- post: tasks/memory_hourly_post.py
- prompt: h

### memory-daily
- interval: 12h
- prompt: d
"""
    runner = _make_runner(tmp_path, hb)
    called_with = []
    monkeypatch.setattr(runner, "claude_call", lambda p: called_with.append(p) or "HEARTBEAT_OK")

    runner.run_cycle(force=True)
    # memory-hourly is PRIORITY (exempt from batch cap) but NOT Tier 0
    # → goes through Claude. memory-daily blocked by pipeline_picked.
    assert len(called_with) == 1
    assert "memory-hourly" in called_with[0]
    assert "memory-daily" not in called_with[0]


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


def test_idle_sentinel_detection():
    """Standalone HEARTBEAT_OK line = idle; prose mention = not idle."""
    from core.heartbeat import _has_idle_sentinel
    leaked = "**🎯 Intent**\n\nNothing worth notifying Pascal.\n\nHEARTBEAT_OK"
    assert _has_idle_sentinel(leaked)
    assert _has_idle_sentinel("HEARTBEAT_OK")
    assert not _has_idle_sentinel("明天下午多了个会，要不要挪一下？")
    # Token mentioned inside prose (this very topic) must NOT be killed.
    assert not _has_idle_sentinel("当模型输出 HEARTBEAT_OK 时就不该打扰你")


def test_single_task_idle_sentinel_suppressed(tmp_path, monkeypatch):
    """Single-task path: reasoning text ending in HEARTBEAT_OK is dropped, not sent."""
    hb = "### intent-check\n- interval: 1h\n- prompt: check\n"
    runner = _make_runner(tmp_path, hb)
    leaked = "The only due intent is a test. Not worth notifying.\n\nHEARTBEAT_OK"
    monkeypatch.setattr(runner, "claude_call", lambda p: leaked)
    result = runner.run_cycle(force=True)
    assert "test" not in result and "HEARTBEAT_OK" not in result


def test_idle_judge_drops_noise(tmp_path, monkeypatch):
    """Judge ON + NOISE verdict → message dropped."""
    hb = "### t\n- interval: 1h\n- prompt: hi\n"
    runner = _make_runner(tmp_path, hb, idle_judge=True)
    monkeypatch.setattr(runner, "claude_call", lambda p: "现在没什么要紧的")
    monkeypatch.setattr(runner, "_judge_is_idle_noise", lambda m: True)
    assert "现在没什么要紧的" not in runner.run_cycle(force=True)


def test_idle_judge_delivers_on_conservative_verdict(tmp_path, monkeypatch):
    """Judge ON + DELIVER verdict (fail-open default) → message kept."""
    hb = "### t\n- interval: 1h\n- prompt: hi\n"
    runner = _make_runner(tmp_path, hb, idle_judge=True)
    monkeypatch.setattr(runner, "claude_call", lambda p: "明天的会改到三点了")
    monkeypatch.setattr(runner, "_judge_is_idle_noise", lambda m: False)
    assert "明天的会改到三点了" in runner.run_cycle(force=True)
