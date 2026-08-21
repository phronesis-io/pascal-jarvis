"""Provider-neutral discovery tests for Claude Code and Codex sessions."""

from __future__ import annotations

import json
import os
import time

import pytest

import core.db as db_module
from core.matters import create_matter, link_entity
from core.work_sessions import discover_sessions


def _write_jsonl(path, rows, mtime=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )
    if mtime is not None:
        os.utime(path, (mtime, mtime))


@pytest.fixture
def roots(tmp_path):
    return tmp_path / "claude", tmp_path / "codex"


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "sessions.db")
    if db_module._connection is not None:
        db_module._connection.close()
    db_module._connection = None
    yield
    if db_module._connection is not None:
        db_module._connection.close()
    db_module._connection = None


def test_discovers_claude_metadata_and_title(roots):
    claude_root, codex_root = roots
    path = claude_root / "project-a" / "claude-file.jsonl"
    _write_jsonl(path, [
        {
            "type": "assistant",
            "sessionId": "claude-session-1",
            "cwd": "/work/jarvis",
            "timestamp": "2026-07-20T10:00:00+08:00",
            "message": {"model": "claude-sonnet-test", "content": "hello"},
        },
        {
            "type": "user",
            "sessionId": "claude-session-1",
            "cwd": "/work/jarvis",
            "timestamp": "2026-07-20T10:01:00+08:00",
            "message": {"content": [{"type": "text", "text": "把移动端入口接起来"}]},
        },
    ])

    items = discover_sessions(
        provider="claude", claude_root=claude_root, codex_root=codex_root,
        include_matter_links=False,
    )

    assert len(items) == 1
    assert items[0]["session_id"] == "claude-session-1"
    assert items[0]["workspace"] == "/work/jarvis"
    assert items[0]["title"] == "把移动端入口接起来"
    assert items[0]["model"] == "claude-sonnet-test"


def test_claude_ignores_synthetic_tail_messages(roots):
    claude_root, codex_root = roots
    path = claude_root / "project-a" / "continued.jsonl"
    _write_jsonl(path, [
        {"type": "user", "sessionId": "claude-real",
         "message": {"content": "继续把飞书交接做完"}},
        {"type": "user", "sessionId": "claude-real",
         "message": {"content": "This session is being continued from a previous conversation"}},
        {"type": "user", "sessionId": "claude-real",
         "message": {"content": "Set model to Fable 5 and saved as your default"}},
    ])

    items = discover_sessions(
        provider="claude", claude_root=claude_root, codex_root=codex_root,
        include_matter_links=False,
    )
    assert items[0]["title"] == "继续把飞书交接做完"


def test_discovers_codex_metadata_and_title(roots):
    claude_root, codex_root = roots
    path = codex_root / "2026" / "07" / "22" / "rollout-test.jsonl"
    _write_jsonl(path, [
        {
            "type": "session_meta",
            "timestamp": "2026-07-22T09:00:00+08:00",
            "payload": {
                "id": "codex-rollout-1",
                "session_id": "codex-session-1",
                "cwd": "/work/jarvis",
                "model_provider": "openai",
            },
        },
        {
            "type": "turn_context",
            "payload": {"cwd": "/work/jarvis", "model": "gpt-5"},
        },
        {
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "认真设计 Matter PRD"},
        },
    ])

    items = discover_sessions(
        provider="codex", claude_root=claude_root, codex_root=codex_root,
        include_matter_links=False,
    )

    assert len(items) == 1
    assert items[0]["session_id"] == "codex-rollout-1"
    assert items[0]["title"] == "认真设计 Matter PRD"
    assert items[0]["model"] == "gpt-5"


def test_filters_old_files_and_applies_global_limit(roots):
    claude_root, codex_root = roots
    now = time.time()
    for index in range(3):
        _write_jsonl(
            claude_root / "p" / f"recent-{index}.jsonl",
            [{"type": "user", "sessionId": f"c-{index}",
              "message": {"content": f"Claude {index}"}}],
            mtime=now - index,
        )
    _write_jsonl(
        codex_root / "old.jsonl",
        [{"type": "session_meta", "payload": {"session_id": "old"}}],
        mtime=now - 45 * 86400,
    )

    items = discover_sessions(
        days=30, limit=2, claude_root=claude_root, codex_root=codex_root,
        include_matter_links=False,
    )
    assert [item["session_id"] for item in items] == ["c-0", "c-1"]


def test_reports_existing_matter_link(roots, isolated_db):
    claude_root, codex_root = roots
    _write_jsonl(
        codex_root / "one.jsonl",
        [
            {"type": "session_meta", "payload": {"session_id": "linked-session"}},
            {"type": "event_msg", "payload": {"type": "user_message",
                                                  "message": "Linked work"}},
        ],
    )
    matter = create_matter("已有事项")
    link_entity(matter["id"], "session", "linked-session", provider="codex")

    items = discover_sessions(
        provider="codex", claude_root=claude_root, codex_root=codex_root,
    )
    assert items[0]["matter_id"] == matter["id"]


def test_codex_filters_internal_subagents_and_uses_recent_user_title(roots):
    claude_root, codex_root = roots
    _write_jsonl(codex_root / "main.jsonl", [
        {"type": "session_meta", "payload": {"id": "main-session"}},
        {"type": "event_msg", "payload": {"type": "user_message",
                                              "message": "最早的需求"}},
        {"type": "event_msg", "payload": {"type": "user_message",
                                              "message": "最新的明确下一步"}},
    ])
    _write_jsonl(codex_root / "guardian.jsonl", [
        {"type": "session_meta", "payload": {
            "id": "guardian-session", "session_id": "main-session",
            "thread_source": "subagent", "source": {"subagent": {"other": "guardian"}},
        }},
        {"type": "event_msg", "payload": {"type": "user_message",
                                              "message": "internal review"}},
    ])

    items = discover_sessions(
        provider="codex", claude_root=claude_root, codex_root=codex_root,
        include_matter_links=False,
    )
    assert [item["session_id"] for item in items] == ["main-session"]
    assert items[0]["title"] == "最新的明确下一步"


def test_codex_finds_user_before_large_tool_tail(roots):
    claude_root, codex_root = roots
    _write_jsonl(codex_root / "long.jsonl", [
        {"type": "session_meta", "payload": {"id": "long-session"}},
        {"type": "event_msg", "payload": {"type": "user_message",
                                              "message": "不要把标题退回 hi"}},
        {"type": "response_item", "payload": {"type": "tool_output",
                                                  "output": "x" * (3 * 1024 * 1024)}},
    ])

    items = discover_sessions(
        provider="codex", claude_root=claude_root, codex_root=codex_root,
        include_matter_links=False,
    )
    assert items[0]["title"] == "不要把标题退回 hi"


def test_rejects_unknown_provider(roots):
    with pytest.raises(ValueError, match="unsupported"):
        discover_sessions(provider="gemini", claude_root=roots[0], codex_root=roots[1])
