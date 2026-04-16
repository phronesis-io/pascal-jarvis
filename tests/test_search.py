"""Tests for core.search — session loading, meta, and substring search."""

import json
from pathlib import Path

from core import search as core_search


def _write_session(path: Path, messages: list[dict]):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for m in messages:
            f.write(json.dumps(m) + "\n")


def test_load_session_messages_empty(tmp_path):
    assert core_search.load_session_messages(tmp_path / "missing.jsonl") == []


def test_load_session_messages_filters_system(tmp_path):
    p = tmp_path / "s.jsonl"
    _write_session(p, [
        {"type": "user", "timestamp": "2026-04-16T10:00:00", "message": {"role": "user", "content": "hi"}},
        {"type": "system", "message": {"content": "sys"}},
        {"type": "assistant", "timestamp": "2026-04-16T10:00:01", "message": {"role": "assistant", "content": "hello"}},
    ])
    msgs = core_search.load_session_messages(p)
    assert len(msgs) == 2
    assert msgs[0]["role"] == "user"


def test_load_session_messages_extracts_tool_blocks(tmp_path):
    p = tmp_path / "s.jsonl"
    _write_session(p, [
        {"type": "assistant", "timestamp": "2026-04-16T10:00:00",
         "message": {"role": "assistant", "content": [
             {"type": "text", "text": "Let me search..."},
             {"type": "tool_use", "name": "Grep", "input": {"pattern": "foo"}},
         ]}},
    ])
    msgs = core_search.load_session_messages(p)
    assert len(msgs) == 1
    assert "Let me search" in msgs[0]["text"]
    assert "[tool: Grep" in msgs[0]["text"]


def test_sessions_meta_newest_first(tmp_path):
    _write_session(tmp_path / "old.jsonl", [
        {"type": "user", "timestamp": "2026-04-01T10:00:00", "message": {"role": "user", "content": "old prompt"}}
    ])
    # Touch "new" after "old" so mtime ordering is deterministic
    import time; time.sleep(0.01)
    _write_session(tmp_path / "new.jsonl", [
        {"type": "user", "timestamp": "2026-04-10T10:00:00", "message": {"role": "user", "content": "new prompt"}}
    ])
    metas = core_search.sessions_meta(tmp_path)
    assert [m["id"] for m in metas][0] == "new"


def test_search_finds_matches_with_context(tmp_path):
    _write_session(tmp_path / "s.jsonl", [
        {"type": "user", "timestamp": "2026-04-16T10:00:00", "message": {"role": "user", "content": "question about PORTFOLIO"}},
        {"type": "assistant", "timestamp": "2026-04-16T10:00:01", "message": {"role": "assistant", "content": "here's my answer"}},
    ])
    results = core_search.search(tmp_path, "portfolio")  # case-insensitive
    assert len(results) == 1
    assert results[0]["session_id"] == "s"
    assert results[0]["role"] == "user"


def test_search_empty_query_returns_nothing(tmp_path):
    assert core_search.search(tmp_path, "") == []


def test_search_limited_to_specific_session(tmp_path):
    _write_session(tmp_path / "a.jsonl", [
        {"type": "user", "timestamp": "2026-04-16T10:00:00", "message": {"role": "user", "content": "apple"}}
    ])
    _write_session(tmp_path / "b.jsonl", [
        {"type": "user", "timestamp": "2026-04-16T10:00:00", "message": {"role": "user", "content": "apple"}}
    ])
    results = core_search.search(tmp_path, "apple", session_id="a")
    assert len(results) == 1
    assert results[0]["session_id"] == "a"
