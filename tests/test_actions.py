"""Tests for core.actions — ACTION marker extraction and processing."""

import json
from pathlib import Path

import pytest

from core.actions import ActionProcessor, parse_params


def test_parse_params_basic():
    assert parse_params("title=hello|url=http://x") == {"title": "hello", "url": "http://x"}


def test_parse_params_empty():
    assert parse_params("") == {}


def test_parse_params_single():
    assert parse_params("query=test search") == {"query": "test search"}


def test_parse_params_value_with_equals():
    """Values containing '=' should not be split."""
    result = parse_params("desc=a=b|title=c")
    assert result["desc"] == "a=b"
    assert result["title"] == "c"


def _make_processor(tmp_path) -> ActionProcessor:
    memory_dir = tmp_path / "memory" / "system"
    memory_dir.mkdir(parents=True)
    return ActionProcessor(
        jarvis_dir=tmp_path,
        memory_dir=tmp_path / "memory",
        jobs_dir=tmp_path / "jobs",
    )


def test_no_actions_passthrough(tmp_path):
    ap = _make_processor(tmp_path)
    reply = "Hello, this is a normal reply."
    assert ap.process(reply) == reply


def test_marker_stripping(tmp_path):
    ap = _make_processor(tmp_path)
    reply = "I'll save that. [ACTION:heartbeat]"
    result = ap.process(reply)
    assert "[ACTION:" not in result
    assert "I'll save that." in result


def test_task_capture_creates_task(tmp_path):
    ap = _make_processor(tmp_path)
    reply = "Got it. [ACTION:task_capture|title=写周报|type=poiesis|energy=m|est=30]"
    result = ap.process(reply)

    # Verify task was created
    tasks_file = tmp_path / "memory" / "system" / "tasks.jsonl"
    assert tasks_file.exists()
    tasks = [json.loads(line) for line in tasks_file.read_text().splitlines() if line.strip()]
    assert len(tasks) == 1
    assert tasks[0]["title"] == "写周报"
    assert tasks[0]["type"] == "poiesis"


def test_task_done(tmp_path):
    ap = _make_processor(tmp_path)
    # First capture a task
    ap.process("[ACTION:task_capture|title=test task]")
    tasks = [json.loads(l) for l in (tmp_path / "memory/system/tasks.jsonl").read_text().splitlines() if l.strip()]
    tid = tasks[0]["id"]

    # Mark it done
    ap.process(f"[ACTION:task_done|id={tid}]")
    tasks = [json.loads(l) for l in (tmp_path / "memory/system/tasks.jsonl").read_text().splitlines() if l.strip()]
    assert tasks[0]["status"] == "done"


def test_multiple_actions(tmp_path):
    ap = _make_processor(tmp_path)
    reply = "Done. [ACTION:task_capture|title=task1] Also [ACTION:task_capture|title=task2]"
    result = ap.process(reply)

    tasks_file = tmp_path / "memory" / "system" / "tasks.jsonl"
    tasks = [json.loads(l) for l in tasks_file.read_text().splitlines() if l.strip()]
    assert len(tasks) == 2


def test_intent_create(tmp_path):
    """Intent create should not crash even without the DB (graceful error)."""
    ap = _make_processor(tmp_path)
    reply = "[ACTION:intent_create|name=test|when=2026-01-01T09:00:00|type=date|prompt=hello]"
    result = ap.process(reply)
    # Should produce either success or graceful error, not crash
    assert "Intent" in result or "❌" in result


def test_unknown_actions_preserved_for_bash(tmp_path):
    """Actions not handled by Python (bg, jobs, etc.) must keep their markers
    so the bash layer can process them."""
    ap = _make_processor(tmp_path)
    reply = "OK [ACTION:bg|prompt=research] and [ACTION:task_capture|title=test]"
    result = ap.process(reply)
    assert "[ACTION:bg|" in result, "bg marker must be preserved for bash"
    assert "[ACTION:task_capture|" not in result, "task_capture should be handled and stripped"


def test_praxis_lifecycle(tmp_path):
    ap = _make_processor(tmp_path)
    # Add
    ap.process("[ACTION:praxis_add|title=拉伸|freq=daily|time=08:30|dur=20]")
    praxis_file = tmp_path / "memory" / "system" / "praxis.jsonl"
    items = [json.loads(l) for l in praxis_file.read_text().splitlines() if l.strip()]
    assert len(items) == 1
    pid = items[0]["id"]

    # Done
    ap.process(f"[ACTION:praxis_done|id={pid}]")
    items = [json.loads(l) for l in praxis_file.read_text().splitlines() if l.strip()]
    assert items[0]["streak_current"] == 1

    # Remove
    ap.process(f"[ACTION:praxis_remove|id={pid}]")
    items = [json.loads(l) for l in praxis_file.read_text().splitlines() if l.strip()]
    assert len(items) == 0
