"""Unified Claude Code/Codex continuity and privacy boundaries."""

from __future__ import annotations

import json
import os
import uuid

from core.cross_session import (
    SESSION_NAMESPACE,
    build_prompt_context,
    collect_incremental,
    discover_interactive_sessions,
    redact_text,
)
from core.prompt import build_system_prompt


def _write(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def _claude(path, session_id="claude-human", secret=""):
    _write(path, [
        {
            "type": "user",
            "sessionId": session_id,
            "cwd": "/work/alpha",
            "timestamp": "2026-08-11T10:00:00Z",
            "message": {"content": f"决定采用统一会话账本 {secret}"},
        },
        {
            "type": "assistant",
            "sessionId": session_id,
            "cwd": "/work/alpha",
            "timestamp": "2026-08-11T10:01:00Z",
            "message": {"content": [{"type": "tool_use", "input": "PRIVATE"},
                                      {"type": "text", "text": "下一步补回归测试"}]},
        },
    ])


def _codex(path, *, session_id="codex-human", source="vscode",
           thread_source="user"):
    _write(path, [
        {"type": "session_meta", "payload": {
            "id": session_id,
            "cwd": "/work/beta",
            "source": source,
            "thread_source": thread_source,
        }},
        {"type": "event_msg", "timestamp": "2026-08-11T11:00:00Z",
         "payload": {"type": "user_message", "message": "把 Codex 结论同步给主 Agent"}},
        # Codex can emit duplicate user_message events; only one turn survives.
        {"type": "event_msg", "timestamp": "2026-08-11T11:00:00Z",
         "payload": {"type": "user_message", "message": "把 Codex 结论同步给主 Agent"}},
        {"type": "response_item", "payload": {
            "type": "tool_output", "output": "api_key=tool-secret-must-not-leak"}},
        {"type": "event_msg", "timestamp": "2026-08-11T11:01:00Z",
         "payload": {"type": "agent_message", "message": "已经实现并完成验证"}},
    ])


def test_discovers_both_providers_and_redacts_before_projection(tmp_path):
    claude_root = tmp_path / "claude"
    codex_root = tmp_path / "codex"
    _claude(
        claude_root / "project" / "human.jsonl",
        secret="api_key=super-secret-value",
    )
    _codex(codex_root / "2026" / "08" / "human.jsonl")

    sessions = discover_interactive_sessions(
        claude_root=claude_root,
        codex_root=codex_root,
        tracker_path=tmp_path / "missing.json",
        limit=10,
    )
    assert {session.provider for session in sessions} == {"claude", "codex"}
    rendered = build_prompt_context(
        claude_root=claude_root,
        codex_root=codex_root,
        tracker_path=tmp_path / "missing.json",
    )
    assert "Claude Code - alpha" in rendered
    assert "Codex - beta" in rendered
    assert "[redacted]" in rendered
    assert "super-secret-value" not in rendered
    assert "tool-secret-must-not-leak" not in rendered
    assert rendered.count("把 Codex 结论同步给主 Agent") == 1


def test_redaction_does_not_mangle_task_notifications():
    assert redact_text("<task-notification>finished</task-notification>") == (
        "<task-notification>finished</task-notification>"
    )
    assert "super-secret" not in redact_text("api_key=super-secret")
    assert redact_text('{"api_key":"super-secret"}') == '{[redacted]}'
    assert "hunter2" not in redact_text('{"password": "hunter2"}')


def test_filters_jarvis_owned_claude_codex_exec_and_subagents(tmp_path):
    claude_root = tmp_path / "claude"
    codex_root = tmp_path / "codex"
    conv_key = "owner-conversation"
    managed_id = str(uuid.uuid5(SESSION_NAMESPACE, f"{conv_key}-1"))
    tracker = tmp_path / "active_sessions.json"
    tracker.write_text(json.dumps({
        conv_key: {"session_id": managed_id, "counter": 1},
    }))
    _claude(claude_root / "jarvis" / f"{managed_id}.jsonl", managed_id)
    background_id = "11111111-1111-4111-8111-111111111111"
    _claude(claude_root / "jarvis" / f"{background_id}.jsonl", background_id)
    jobs = tmp_path / "jobs" / "registry.json"
    jobs.parent.mkdir()
    jobs.write_text(json.dumps({
        "j-test": {"session_id": background_id, "status": "running"},
    }))
    _claude(claude_root / "manual" / "manual.jsonl", "manual-claude")
    _claude(claude_root / "automation" / "automation.jsonl", "automation")
    automation = claude_root / "automation" / "automation.jsonl"
    rows = [json.loads(line) for line in automation.read_text().splitlines()]
    rows[0]["message"]["content"] = (
        "self improve —— 这是 heartbeat 无人值守任务，不是用户聊天"
    )
    _write(automation, rows)
    _codex(codex_root / "manual.jsonl", session_id="manual-codex")
    _codex(codex_root / "fallback.jsonl", session_id="fallback", source="exec")
    _codex(codex_root / "subagent.jsonl", session_id="subagent",
           source={"subagent": "review"}, thread_source="subagent")

    sessions = discover_interactive_sessions(
        claude_root=claude_root,
        codex_root=codex_root,
        tracker_path=tracker,
        jobs_registry_path=jobs,
        limit=20,
    )
    assert {session.session_id for session in sessions} == {
        "manual-claude", "manual-codex",
    }


def test_incremental_codex_watermark_and_context_tail(tmp_path):
    claude_root = tmp_path / "claude"
    codex_root = tmp_path / "codex"
    path = codex_root / "manual.jsonl"
    _codex(path)
    state = tmp_path / "seen.json"

    first = collect_incremental(
        state_file=state,
        claude_root=claude_root,
        codex_root=codex_root,
        tracker_path=tmp_path / "missing.json",
    )
    assert "把 Codex 结论同步给主 Agent" in first
    assert "已经实现并完成验证" in first
    assert collect_incremental(
        state_file=state,
        claude_root=claude_root,
        codex_root=codex_root,
        tracker_path=tmp_path / "missing.json",
    ) == ""

    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "type": "event_msg",
            "timestamp": "2026-08-11T11:02:00Z",
            "payload": {"type": "user_message", "message": "最后决定今天发布"},
        }, ensure_ascii=False) + "\n")
    third = collect_incremental(
        state_file=state,
        claude_root=claude_root,
        codex_root=codex_root,
        tracker_path=tmp_path / "missing.json",
    )
    assert "最后决定今天发布" in third
    for line in third.splitlines():
        if "已经实现并完成验证" in line:
            assert line.startswith("[context] ")


def test_live_root_gate_injects_owner_prompt_but_not_group(tmp_path, monkeypatch):
    jarvis_dir = tmp_path / "jarvis"
    memory = tmp_path / "memory"
    claude_root = tmp_path / "claude"
    codex_root = tmp_path / "codex"
    jarvis_dir.mkdir()
    memory.mkdir()
    _codex(codex_root / "manual.jsonl")
    monkeypatch.setenv("JARVIS_DIR", str(jarvis_dir))
    monkeypatch.setenv("CROSS_SESSION_CLAUDE_ROOT", str(claude_root))
    monkeypatch.setenv("CROSS_SESSION_CODEX_ROOT", str(codex_root))

    owner = build_system_prompt(
        jarvis_dir=str(jarvis_dir),
        memory_dir=str(memory),
        session_dir=str(tmp_path / "provider-sessions"),
        session_id="owner-session",
        conv_key="owner-key",
        now_ts="2026-08-11 12:00",
        tracker_path=str(jarvis_dir / "active_sessions.json"),
    )
    group = build_system_prompt(
        jarvis_dir=str(jarvis_dir),
        memory_dir=str(memory),
        session_dir=str(tmp_path / "provider-sessions"),
        session_id="group-session",
        conv_key="group-key",
        now_ts="2026-08-11 12:00",
        tracker_path=str(jarvis_dir / "active_sessions.json"),
        chat_type="group",
    )
    assert "Recent External Work Sessions" in owner
    assert "把 Codex 结论同步给主 Agent" in owner
    assert "Recent External Work Sessions" not in group
    assert "把 Codex 结论同步给主 Agent" not in group


def test_corrupt_provider_file_degrades_without_losing_good_session(tmp_path):
    claude_root = tmp_path / "claude"
    codex_root = tmp_path / "codex"
    _codex(codex_root / "good.jsonl")
    bad = codex_root / "bad.jsonl"
    bad.write_text("{not-json\n", encoding="utf-8")
    os.utime(bad, None)

    rendered = build_prompt_context(
        claude_root=claude_root,
        codex_root=codex_root,
        tracker_path=tmp_path / "missing.json",
    )
    assert "把 Codex 结论同步给主 Agent" in rendered
