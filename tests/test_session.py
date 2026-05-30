"""Tests for core.session — rotation, cross-session backfill, text extraction."""

import json
from pathlib import Path

import pytest

from core.session import (
    NAMESPACE,
    SessionManager,
    _extract_text,
    build_recent_turns,
    format_recent_turns,
    read_tail_turns,
)
import uuid


@pytest.fixture(autouse=True)
def _isolate_jarvis_dir(monkeypatch):
    """build_recent_turns() merges the heartbeat outbox from $JARVIS_DIR.
    Pascal's shell exports JARVIS_DIR, so without this the tests read the real
    production outbox and its 10 live check-ins evict the test fixtures from the
    window. Unset it so build_recent_turns only sees the tmp_path session files.
    """
    monkeypatch.delenv("JARVIS_DIR", raising=False)


def _write_session(session_dir: Path, sid: str, turns: list[dict]):
    path = session_dir / f"{sid}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for t in turns:
            f.write(json.dumps(t) + "\n")


def test_new_session_created_on_first_call(tmp_path):
    tracker = tmp_path / "tracker.json"
    tracker.write_text("{}")
    sm = SessionManager(tracker, tmp_path / "sessions")
    sid, rotated = sm.get_session("user-1")
    assert sid
    assert rotated is False
    assert sm.get_counter("user-1") == 1


def test_stable_session_id_across_calls(tmp_path):
    tracker = tmp_path / "tracker.json"
    tracker.write_text("{}")
    sm1 = SessionManager(tracker, tmp_path / "sessions")
    sid1, _ = sm1.get_session("user-1")

    # Fresh instance reads tracker
    sm2 = SessionManager(tracker, tmp_path / "sessions")
    sid2, rotated = sm2.get_session("user-1")
    assert sid1 == sid2
    assert rotated is False


def test_rotation_when_session_exceeds_max_size(tmp_path):
    tracker = tmp_path / "tracker.json"
    tracker.write_text("{}")
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()

    sm = SessionManager(tracker, session_dir, max_size=100)
    sid1, _ = sm.get_session("user-1")

    # Simulate a session file that exceeds max_size
    (session_dir / f"{sid1}.jsonl").write_text("x" * 200)

    sm2 = SessionManager(tracker, session_dir, max_size=100)
    sid2, rotated = sm2.get_session("user-1")
    assert sid1 != sid2
    assert rotated is True
    assert sm2.get_counter("user-1") == 2


def test_session_id_is_deterministic_uuid5(tmp_path):
    """Same conv_key + counter always maps to same session_id."""
    expected = str(uuid.uuid5(NAMESPACE, "user-1-1"))
    tracker = tmp_path / "tracker.json"
    tracker.write_text("{}")
    sm = SessionManager(tracker, tmp_path / "sessions")
    sid, _ = sm.get_session("user-1")
    assert sid == expected


def test_extract_text_string():
    assert _extract_text("hello") == "hello"


def test_extract_text_list_of_blocks():
    content = [
        {"type": "text", "text": "hello"},
        {"type": "tool_use", "name": "Read"},
        {"type": "text", "text": "world"},
    ]
    assert _extract_text(content) == "hello\nworld"


def test_extract_text_handles_weird_input():
    assert _extract_text(None) == ""
    assert _extract_text(123) == ""
    assert _extract_text([]) == ""


def test_read_tail_turns_skips_non_user_assistant(tmp_path):
    _write_session(tmp_path, "s1", [
        {"type": "user", "message": {"role": "user", "content": "hi"}, "timestamp": "2026-04-16T10:00:00Z"},
        {"type": "system", "message": {"content": "sys"}},  # skipped
        {"type": "assistant", "message": {"role": "assistant", "content": "hello"}, "timestamp": "2026-04-16T10:00:01Z"},
    ])
    turns = read_tail_turns(tmp_path, "s1")
    assert len(turns) == 2
    assert turns[0]["role"] == "user"
    assert turns[1]["role"] == "assistant"


def test_read_tail_turns_missing_file(tmp_path):
    assert read_tail_turns(tmp_path, "nonexistent") == []


def test_read_tail_turns_limit(tmp_path):
    turns_data = [
        {"type": "user", "message": {"role": "user", "content": f"msg {i}"}, "timestamp": ""}
        for i in range(10)
    ]
    _write_session(tmp_path, "s1", turns_data)
    turns = read_tail_turns(tmp_path, "s1", limit=3)
    assert len(turns) == 3
    assert turns[-1]["text"] == "msg 9"


def test_format_recent_turns_empty():
    assert format_recent_turns([]) == ""


def test_format_recent_turns_truncates_long():
    turns = [{"role": "user", "text": "x" * 1000, "ts": "2026-04-16T10:00"}]
    output = format_recent_turns(turns, max_msg_chars=50)
    assert "..." in output
    assert "x" * 51 not in output  # truncated before limit+1


def test_build_recent_turns_backfills_on_new_session(tmp_path):
    """When current session has < 5 turns and counter > 1, backfill from prev session."""
    conv_key = "user-1"
    # Previous session
    prev_sid = str(uuid.uuid5(NAMESPACE, f"{conv_key}-1"))
    _write_session(tmp_path, prev_sid, [
        {"type": "user", "message": {"role": "user", "content": f"prev {i}"}, "timestamp": ""}
        for i in range(10)
    ])
    # Current session (only 2 turns — needs backfill)
    curr_sid = str(uuid.uuid5(NAMESPACE, f"{conv_key}-2"))
    _write_session(tmp_path, curr_sid, [
        {"type": "user", "message": {"role": "user", "content": "curr 0"}, "timestamp": ""},
        {"type": "assistant", "message": {"role": "assistant", "content": "curr 1"}, "timestamp": ""},
    ])

    output = build_recent_turns(tmp_path, curr_sid, counter=2, conv_key=conv_key, limit=10)
    assert "prev" in output
    assert "curr" in output


def test_build_recent_turns_no_backfill_when_full(tmp_path):
    """No backfill if current session has 5+ turns."""
    conv_key = "user-1"
    prev_sid = str(uuid.uuid5(NAMESPACE, f"{conv_key}-1"))
    _write_session(tmp_path, prev_sid, [
        {"type": "user", "message": {"role": "user", "content": "PREV_MARKER"}, "timestamp": ""}
    ])
    curr_sid = str(uuid.uuid5(NAMESPACE, f"{conv_key}-2"))
    _write_session(tmp_path, curr_sid, [
        {"type": "user", "message": {"role": "user", "content": f"curr {i}"}, "timestamp": ""}
        for i in range(10)
    ])
    output = build_recent_turns(tmp_path, curr_sid, counter=2, conv_key=conv_key, limit=20)
    assert "PREV_MARKER" not in output
