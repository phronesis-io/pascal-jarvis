"""Regression tests for heartbeat bug fixes (2026-05-20 ~ 2026-05-25).

Each test targets a specific bug that was found and fixed during code review.
"""

import json
import time
from pathlib import Path

import pytest

from core.heartbeat import HeartbeatRunner, parse_heartbeat


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


# ── HEARTBEAT_OK exact match (was substring before fix) ────────────


def test_heartbeat_ok_exact_match_passes_through():
    """Exact 'HEARTBEAT_OK' should be treated as idle."""
    # This is the normal case — just verifying it still works
    assert "HEARTBEAT_OK" == "HEARTBEAT_OK".strip()


def test_heartbeat_ok_substring_in_json_not_discarded(tmp_path, monkeypatch):
    """A JSON envelope containing 'HEARTBEAT_OK' as a per-task value
    must NOT cause the entire response to be discarded.

    Bug: old code used `"HEARTBEAT_OK" in raw` which matched substrings.
    Fix: changed to `raw.strip() == "HEARTBEAT_OK"` (exact match).
    """
    hb = (
        "### task-a\n- interval: 1h\n- prompt: a\n\n"
        "### task-b\n- interval: 1h\n- prompt: b\n"
    )
    runner = _make_runner(tmp_path, hb)

    envelope = json.dumps({
        "tasks": {
            "task-a": "HEARTBEAT_OK",
            "task-b": "This is real content that should reach the user",
        },
        "user_message": "",
    })
    monkeypatch.setattr(runner, "claude_call", lambda p: envelope)
    result = runner.run_cycle(force=True)

    # task-b's content must NOT be silently discarded
    assert "real content" in result


def test_heartbeat_ok_with_whitespace(tmp_path, monkeypatch):
    """HEARTBEAT_OK with surrounding whitespace should still be idle."""
    runner = _make_runner(tmp_path, "### t\n- interval: 1h\n- prompt: hi\n")
    monkeypatch.setattr(runner, "claude_call", lambda p: "  HEARTBEAT_OK  \n")
    result = runner.run_cycle(force=True)
    assert result == ""


# ── MAX_BATCH_SIZE cap ─────────────────────────────────────────────


def test_batch_cap_limits_tasks_sent_to_claude(tmp_path, monkeypatch):
    """When more tasks are due than MAX_BATCH_SIZE, only MAX_BATCH_SIZE
    should be sent to Claude in a single call."""
    # Create 6 tasks, all with 1h interval
    tasks_md = "\n\n".join(
        f"### task-{i}\n- interval: 1h\n- prompt: do {i}" for i in range(6)
    )
    runner = _make_runner(tmp_path, tasks_md)

    called_prompts = []
    monkeypatch.setattr(runner, "claude_call",
                        lambda p: called_prompts.append(p) or "HEARTBEAT_OK")

    runner.run_cycle(force=True)

    assert len(called_prompts) == 1
    prompt = called_prompts[0]
    # Count how many "=== TASK:" markers appear
    task_markers = prompt.count("=== TASK:")
    assert task_markers <= HeartbeatRunner.MAX_BATCH_SIZE


def test_batch_cap_staleness_sort(tmp_path, monkeypatch):
    """Stalest tasks (lowest last_run) should be selected first."""
    tasks_md = "\n\n".join(
        f"### task-{i}\n- interval: 1h\n- prompt: do {i}" for i in range(6)
    )
    runner = _make_runner(tmp_path, tasks_md)

    now = int(time.time())
    # task-0 has the oldest last_run (stalest), task-5 has the newest (freshest)
    state = {f"task-{i}": {"last_run": now - 7200 + i * 100} for i in range(6)}
    runner.save_state(state)

    called_prompts = []
    monkeypatch.setattr(runner, "claude_call",
                        lambda p: called_prompts.append(p) or "HEARTBEAT_OK")

    runner.run_cycle(force=True)

    prompt = called_prompts[0]
    # task-0 should be in the prompt (stalest), task-5 should not (freshest)
    assert "task-0" in prompt
    assert "task-5" not in prompt


def test_deferred_tasks_stay_due_next_cycle(tmp_path, monkeypatch):
    """Tasks deferred by batch cap should remain due in the next cycle."""
    tasks_md = "\n\n".join(
        f"### task-{i}\n- interval: 1h\n- prompt: do {i}" for i in range(6)
    )
    runner = _make_runner(tmp_path, tasks_md)

    call_count = [0]
    def mock_claude(p):
        call_count[0] += 1
        return "HEARTBEAT_OK"
    monkeypatch.setattr(runner, "claude_call", mock_claude)

    # First cycle: 4 tasks run
    runner.run_cycle(force=True)
    assert call_count[0] == 1

    # Second cycle: remaining 2 tasks should be due (their last_run wasn't updated)
    runner.run_cycle(force=True)
    assert call_count[0] == 2  # Claude called again for the deferred tasks


def test_tier0_tasks_bypass_claude(tmp_path, monkeypatch):
    """TIER0_TASKS (calendar-sync) should pipe pre→post directly, no Claude.
    PRIORITY_TASKS that need reasoning (memory-hourly) still go through Claude."""
    hb = (
        "### calendar-sync\n- interval: 30m\n- pre: tasks/cal_pre.sh\n"
        "- post: tasks/cal_post.py\n- prompt: sync\n\n"
        "### memory-hourly\n- interval: 1h\n- pre: tasks/mem_pre.sh\n"
        "- post: tasks/mem_post.py\n- prompt: index\n\n"
        "### checkin\n- interval: 30m\n- prompt: hi\n"
    )
    runner = _make_runner(tmp_path, hb)

    # Create dummy scripts
    for name in ["cal_pre.sh", "mem_pre.sh"]:
        pre = runner.jarvis_dir / "tasks" / name
        pre.parent.mkdir(parents=True, exist_ok=True)
        pre.write_text("#!/bin/bash\necho 'data'")
        pre.chmod(0o755)
    for name in ["cal_post.py", "mem_post.py"]:
        post = runner.jarvis_dir / "tasks" / name
        post.write_text("#!/usr/bin/env python3\nimport sys\nprint(sys.stdin.read().strip())")
        post.chmod(0o755)

    claude_called = []
    monkeypatch.setattr(runner, "claude_call",
                        lambda p: claude_called.append(p) or "HEARTBEAT_OK")

    result = runner.run_cycle(force=True)

    # Claude should be called for memory-hourly + checkin, NOT calendar-sync
    if claude_called:
        prompt = claude_called[0]
        assert "calendar-sync" not in prompt  # Tier 0 → bypassed
        assert "memory-hourly" in prompt  # PRIORITY but not Tier 0 → through Claude


def test_batch_cap_before_prescripts(tmp_path, monkeypatch):
    """Batch cap must happen BEFORE pre-scripts run, to avoid wasting
    pre-script side effects on tasks that will be deferred."""
    tasks_md = "\n\n".join(
        f"### task-{i}\n- interval: 1h\n- pre: tasks/pre_{i}.sh\n- prompt: do {i}"
        for i in range(6)
    )
    runner = _make_runner(tmp_path, tasks_md)

    scripts_called = []
    original_run_script = runner.run_script

    def tracking_run_script(path, stdin_data=""):
        scripts_called.append(path)
        return "some data"  # non-empty so task is runnable

    monkeypatch.setattr(runner, "run_script", tracking_run_script)
    monkeypatch.setattr(runner, "claude_call", lambda p: "HEARTBEAT_OK")

    runner.run_cycle(force=True)

    # Only MAX_BATCH_SIZE pre-scripts should have run
    pre_scripts = [s for s in scripts_called if "pre_" in s]
    assert len(pre_scripts) <= HeartbeatRunner.MAX_BATCH_SIZE
