"""Tests for tasks/checkin_post.py — JSONL log keeps recent check-ins and
survives markdown headers inside content."""

import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "tasks" / "checkin_post.py"
PRE_SCRIPT = Path(__file__).resolve().parent.parent / "tasks" / "checkin_pre.sh"


def test_checkin_pre_syntax():
    result = subprocess.run(
        ["bash", "-n", str(PRE_SCRIPT)], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


def _run(stdin: str, memory_dir: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=stdin,
        capture_output=True,
        text=True,
        env={**os.environ, "MEMORY_DIR": str(memory_dir)},
    )


def test_logs_and_passes_through(tmp_path):
    result = _run("今天天气不错,要不要出去走走?", tmp_path)
    assert result.returncode == 0
    assert "今天天气不错" in result.stdout

    log = tmp_path / "system" / "checkin_log.jsonl"
    assert log.exists()
    entries = [json.loads(l) for l in log.read_text().splitlines() if l.strip()]
    assert len(entries) == 1
    assert entries[0]["content"] == "今天天气不错,要不要出去走走?"


def test_preserves_markdown_headers_in_content(tmp_path):
    """Regression: previous impl split on '\\n### ' which ate this data."""
    content = "Hello!\n\n### Option A\n- Take a walk\n### Option B\n- Read"
    result = _run(content, tmp_path)
    assert result.returncode == 0

    log = tmp_path / "system" / "checkin_log.jsonl"
    entries = [json.loads(l) for l in log.read_text().splitlines() if l.strip()]
    assert len(entries) == 1
    assert entries[0]["content"] == content  # fully preserved


def test_caps_at_max_entries(tmp_path):
    for i in range(45):
        _run(f"message {i}", tmp_path)
    log = tmp_path / "system" / "checkin_log.jsonl"
    entries = [json.loads(l) for l in log.read_text().splitlines() if l.strip()]
    assert len(entries) == 40  # MAX_ENTRIES
    # Oldest dropped, newest kept
    assert entries[0]["content"] == "message 5"
    assert entries[-1]["content"] == "message 44"


def test_heartbeat_ok_noop(tmp_path):
    result = _run("HEARTBEAT_OK", tmp_path)
    assert result.returncode == 0
    assert result.stdout == ""
    assert not (tmp_path / "checkin_log.jsonl").exists()


def test_error_output_skipped(tmp_path):
    result = _run("Traceback (most recent call last):\n  File ...", tmp_path)
    assert result.returncode == 0
    assert not (tmp_path / "checkin_log.jsonl").exists()
