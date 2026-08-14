"""Unified Claude Code/Codex continuity and privacy boundaries."""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime
from pathlib import Path

from core import cross_session
from core.cross_session import (
    SESSION_NAMESPACE,
    build_prompt_context,
    collect_incremental,
    discover_interactive_sessions,
    redact_text,
)
from core.cross_session_parsing import SessionTail, Turn
from core.cross_session_projection import (
    build_prompt_context as build_projected_context,
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
           thread_source="user", originator="Codex Desktop",
           user_message="把 Codex 结论同步给主 Agent"):
    _write(path, [
        {"type": "session_meta", "payload": {
            "id": session_id,
            "cwd": "/work/beta",
            "source": source,
            "thread_source": thread_source,
            "originator": originator,
        }},
        {"type": "event_msg", "timestamp": "2026-08-11T11:00:00Z",
         "payload": {"type": "user_message", "message": user_message}},
        # Codex can emit duplicate user_message events; only one turn survives.
        {"type": "event_msg", "timestamp": "2026-08-11T11:00:00Z",
         "payload": {"type": "user_message", "message": user_message}},
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


def test_provider_failures_are_not_projected_as_assistant_memory(tmp_path):
    claude_root = tmp_path / "claude"
    codex_root = tmp_path / "codex"
    claude_path = claude_root / "project" / "human.jsonl"
    _claude(claude_path)
    with claude_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "type": "assistant",
            "sessionId": "claude-human",
            "cwd": "/work/alpha",
            "timestamp": "2026-08-11T10:02:00Z",
            "message": {"content": "You've hit your weekly limit · resets Friday"},
        }, ensure_ascii=False) + "\n")
    codex_path = codex_root / "manual.jsonl"
    _codex(codex_path)
    with codex_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "type": "event_msg",
            "timestamp": "2026-08-11T11:02:00Z",
            "payload": {
                "type": "agent_message",
                "message": "API Error: 424 no account available for model",
            },
        }, ensure_ascii=False) + "\n")

    rendered = build_prompt_context(
        claude_root=claude_root,
        codex_root=codex_root,
        tracker_path=tmp_path / "missing.json",
    )

    assert "weekly limit" not in rendered
    assert "API Error: 424" not in rendered
    assert "下一步补回归测试" in rendered
    assert "已经实现并完成验证" in rendered


def test_bounded_projection_keeps_complete_records():
    sessions = [
        SessionTail(
            provider="codex",
            session_id=f"session-{index}",
            workspace=f"/work/project-{index}",
            path=Path(f"/work/session-{index}.jsonl"),
            updated_at=f"2026-08-11T1{index}:00:00Z",
            turns=(
                Turn("user", f"USER_RECORD_{index}_" + "u" * 100),
                Turn("assistant", f"ASSISTANT_RECORD_{index}_" + "a" * 100),
            ),
        )
        for index in range(3)
    ]

    rendered = build_projected_context(
        max_chars=650,
        discover=lambda **_kwargs: list(reversed(sessions)),
    )

    assert len(rendered) <= 650
    assert "[older session context omitted]" in rendered
    for line in rendered.splitlines():
        if "_RECORD_" in line:
            assert line.startswith(("- User: ", "- Assistant: "))
            assert line.endswith(("u" * 100, "a" * 100))


def test_tiny_projection_budget_never_slices_a_framing_line():
    sessions = [
        SessionTail(
            provider="codex",
            session_id="session",
            workspace="/work/project",
            path=Path("/work/session.jsonl"),
            updated_at="2026-08-11T10:00:00Z",
            turns=(Turn("user", "objective"),),
        )
    ]

    rendered = build_projected_context(
        max_chars=45,
        discover=lambda **_kwargs: sessions,
    )

    assert len(rendered) <= 45
    assert rendered in {"", "## Recent External Work Sessions"}


def test_redaction_does_not_mangle_task_notifications():
    assert redact_text("<task-notification>finished</task-notification>") == (
        "<task-notification>finished</task-notification>"
    )
    assert "super-secret" not in redact_text("api_key=super-secret")
    assert redact_text('{"api_key":"super-secret"}') == '{[redacted]}'
    assert "hunter2" not in redact_text('{"password": "hunter2"}')
    assert "AWSVALUE" not in redact_text("AWS_SECRET_ACCESS_KEY=AWSVALUE")
    assert "AKIAIOSFODNN7EXAMPLE" not in redact_text("AKIAIOSFODNN7EXAMPLE")
    pem = "-----BEGIN PRIVATE KEY----- private-material -----END PRIVATE KEY-----"
    assert "private-material" not in redact_text(pem)
    assert "session-value" not in redact_text("Cookie: session=session-value\nnext")
    assert redact_text("Cookie: session=session-value\nnext").endswith("next")
    uri = redact_text("connect https://alice:uri-password@example.test/path")
    assert "alice" not in uri and "uri-password" not in uri
    assert "example.test/path" in uri


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
    retry_id = "22222222-2222-4222-8222-222222222222"
    _claude(claude_root / "jarvis" / f"{background_id}.jsonl", background_id)
    _claude(claude_root / "jarvis" / f"{retry_id}.jsonl", retry_id)
    jobs = tmp_path / "jobs" / "registry.json"
    jobs.parent.mkdir()
    jobs.write_text(json.dumps({
        "j-test": {
            "session_id": background_id,
            "session_ids": [background_id, retry_id],
            "status": "running",
        },
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
    _codex(codex_root / "desktop-exec.jsonl", session_id="desktop-exec", source="exec")
    _codex(
        codex_root / "fallback.jsonl",
        session_id="fallback",
        source="exec",
        originator="Codex CLI",
        user_message="You are the Codex execution provider inside Jarvis. do work",
    )
    _codex(
        codex_root / "review.jsonl",
        session_id="review",
        source="exec",
        user_message="Review the code changes against the base branch main",
    )
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
        "manual-claude", "manual-codex", "desktop-exec",
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


def test_incremental_prunes_watermarks_outside_scan_window_without_replay(
    tmp_path, monkeypatch,
):
    now = 10_000
    codex_root = tmp_path / "codex"
    active = codex_root / "active.jsonl"
    stale = codex_root / "stale.jsonl"
    _codex(active, session_id="active")
    _codex(stale, session_id="stale")
    os.utime(active, (now - 10, now - 10))
    os.utime(stale, (now - 7200, now - 7200))
    state = tmp_path / "seen.json"
    state.write_text(json.dumps({
        "version": 2,
        "files": {
            str(stale): {
                "provider": "codex",
                "session_id": "stale",
                "size": stale.stat().st_size,
                "fingerprints": ["old"],
            },
        },
    }), encoding="utf-8")
    monkeypatch.setattr("core.cross_session.time.time", lambda: now)

    first = collect_incremental(
        state_file=state,
        claude_root=tmp_path / "claude",
        codex_root=codex_root,
        tracker_path=tmp_path / "missing.json",
        window_hours=1,
    )
    saved = json.loads(state.read_text(encoding="utf-8"))

    assert "把 Codex 结论同步给主 Agent" in first
    assert set(saved["files"]) == {str(active)}
    assert collect_incremental(
        state_file=state,
        claude_root=tmp_path / "claude",
        codex_root=codex_root,
        tracker_path=tmp_path / "missing.json",
        window_hours=1,
    ) == ""


def test_incremental_reactivation_after_pruning_emits_only_new_turn(
    tmp_path, monkeypatch,
):
    base = datetime.fromisoformat("2026-08-11T12:00:00+00:00").timestamp()
    codex_root = tmp_path / "codex"
    path = codex_root / "reactivated.jsonl"
    _codex(path, session_id="reactivated")
    state = tmp_path / "seen.json"
    clock = {"now": base}
    monkeypatch.setattr("core.cross_session.time.time", lambda: clock["now"])

    first = collect_incremental(
        state_file=state,
        claude_root=tmp_path / "claude",
        codex_root=codex_root,
        tracker_path=tmp_path / "missing.json",
        window_hours=1,
    )
    assert "把 Codex 结论同步给主 Agent" in first

    clock["now"] = base + 7200
    os.utime(path, (base, base))
    assert collect_incremental(
        state_file=state,
        claude_root=tmp_path / "claude",
        codex_root=codex_root,
        tracker_path=tmp_path / "missing.json",
        window_hours=1,
    ) == ""
    assert json.loads(state.read_text(encoding="utf-8"))["files"] == {}

    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "type": "event_msg",
            "timestamp": "2026-08-11T15:00:00Z",
            "payload": {"type": "user_message", "message": "重新激活后的新决定"},
        }, ensure_ascii=False) + "\n")
    os.utime(path, (base + 10800, base + 10800))
    clock["now"] = base + 10801
    reactivated = collect_incremental(
        state_file=state,
        claude_root=tmp_path / "claude",
        codex_root=codex_root,
        tracker_path=tmp_path / "missing.json",
        window_hours=1,
    )

    assert "重新激活后的新决定" in reactivated
    lines = reactivated.splitlines()
    assert len([line for line in lines if not line.startswith("[context] ")]) == 1
    for line in lines:
        if "把 Codex 结论同步给主 Agent" in line or "已经实现并完成验证" in line:
            assert line.startswith("[context] ")


def test_prompt_and_first_digest_keep_latest_user_after_many_agent_updates(tmp_path):
    codex_root = tmp_path / "codex"
    path = codex_root / "busy.jsonl"
    _codex(path, user_message="用户最初目标不能被进度消息挤掉")
    with path.open("a", encoding="utf-8") as handle:
        for index in range(25):
            handle.write(json.dumps({
                "type": "event_msg",
                "timestamp": f"2026-08-11T11:{index + 2:02d}:00Z",
                "payload": {
                    "type": "agent_message",
                    "message": f"进度更新 {index}",
                },
            }, ensure_ascii=False) + "\n")

    kwargs = {
        "claude_root": tmp_path / "claude",
        "codex_root": codex_root,
        "tracker_path": tmp_path / "missing.json",
    }
    assert "用户最初目标不能被进度消息挤掉" in build_prompt_context(**kwargs)
    assert "用户最初目标不能被进度消息挤掉" in collect_incremental(
        state_file=tmp_path / "seen.json", **kwargs)


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


def test_automated_candidates_do_not_consume_valid_scan_budget(
    tmp_path, monkeypatch,
):
    codex_root = tmp_path / "codex"
    human = codex_root / "human.jsonl"
    _codex(human, session_id="human", user_message="保留较早的人类会话")
    os.utime(human, (1, 1))
    for index in range(4):
        automated = codex_root / f"automation-{index}.jsonl"
        _codex(
            automated,
            session_id=f"automation-{index}",
            source="exec",
            user_message="Review the code changes against the base branch main",
        )
        os.utime(automated, (10 + index, 10 + index))
    monkeypatch.setattr("core.cross_session.MAX_SCAN_FILES", 2)
    monkeypatch.setattr("core.cross_session.time.time", lambda: 100)

    sessions = discover_interactive_sessions(
        window_hours=1,
        claude_root=tmp_path / "claude",
        codex_root=codex_root,
        tracker_path=tmp_path / "missing.json",
        limit=1,
    )

    assert [session.session_id for session in sessions] == ["human"]


def test_large_codex_tail_cannot_hide_automated_first_request(
    tmp_path, monkeypatch,
):
    codex_root = tmp_path / "codex"
    automated = codex_root / "large-automation.jsonl"
    _codex(
        automated,
        session_id="large-automation",
        source="exec",
        user_message="Review the code changes against the base branch main",
    )
    with automated.open("a", encoding="utf-8") as handle:
        for index in range(20):
            handle.write(json.dumps({
                "type": "response_item",
                "payload": {"type": "reasoning", "text": "x" * 100},
            }) + "\n")
        handle.write(json.dumps({
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "看起来像正常请求"},
        }, ensure_ascii=False) + "\n")

    original_tail = cross_session._tail_records
    monkeypatch.setattr(
        "core.cross_session._tail_records",
        lambda path: original_tail(path, max_bytes=512),
    )

    sessions = discover_interactive_sessions(
        claude_root=tmp_path / "claude",
        codex_root=codex_root,
        tracker_path=tmp_path / "missing.json",
        limit=10,
    )

    assert sessions == []


def test_desktop_exec_without_head_user_evidence_fails_closed(tmp_path):
    path = tmp_path / "codex" / "drifted.jsonl"
    _write(path, [{
        "type": "session_meta",
        "payload": {
            "id": "drifted",
            "cwd": "/work/beta",
            "source": "exec",
            "thread_source": "user",
            "originator": "Codex Desktop",
        },
    }, {
        "type": "event_msg",
        "payload": {"type": "agent_message", "message": "private automation"},
    }])

    sessions = discover_interactive_sessions(
        claude_root=tmp_path / "claude",
        codex_root=tmp_path / "codex",
        tracker_path=tmp_path / "missing.json",
        limit=10,
    )

    assert sessions == []


def test_facade_keeps_public_types_constants_and_function_signatures():
    """The split must not require callers to migrate their imports."""
    import inspect

    from core import cross_session_discovery
    from core import cross_session_parsing
    from core import cross_session_projection

    assert cross_session.Turn is cross_session_parsing.Turn
    assert cross_session.SessionTail is cross_session_parsing.SessionTail
    assert cross_session.SESSION_NAMESPACE == cross_session_discovery.SESSION_NAMESPACE
    assert cross_session.DEFAULT_STATE_FILE == cross_session_projection.DEFAULT_STATE_FILE
    assert list(inspect.signature(cross_session.discover_interactive_sessions).parameters) == [
        "claude_root", "codex_root", "tracker_path", "jobs_registry_path",
        "window_hours", "limit",
    ]
    assert list(inspect.signature(cross_session.collect_incremental).parameters) == [
        "state_file", "claude_root", "codex_root", "tracker_path",
        "jobs_registry_path", "window_hours",
    ]
    assert list(inspect.signature(cross_session.build_prompt_context).parameters) == [
        "claude_root", "codex_root", "tracker_path", "jobs_registry_path",
        "window_hours", "max_chars",
    ]


def test_facade_private_codex_meta_hook_still_drives_parser(tmp_path, monkeypatch):
    session = tmp_path / "codex.jsonl"
    session.write_text(
        json.dumps({"type": "event_msg", "payload": {
            "type": "user_message", "message": "hello",
        }}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(cross_session, "_codex_meta", lambda _path: {
        "id": "patched-meta", "cwd": "/patched", "source": "vscode",
    })

    parsed = cross_session._codex_tail(session)

    assert parsed is not None
    assert parsed.session_id == "patched-meta"
    assert parsed.workspace == "/patched"


def test_facade_private_codex_policy_hooks_still_drive_parser(tmp_path, monkeypatch):
    session = tmp_path / "codex.jsonl"
    session.write_text(
        json.dumps({"type": "event_msg", "payload": {
            "type": "user_message", "message": "hello",
        }}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(cross_session, "_codex_meta", lambda _path: {
        "id": "patched-policy", "cwd": "/patched", "source": "vscode",
    })
    monkeypatch.setattr(cross_session, "redact_text", lambda _value: "patched")
    monkeypatch.setattr(
        cross_session, "_turn_identity",
        lambda *_args: "patched-identity",
    )

    parsed = cross_session._codex_tail(session)

    assert parsed is not None
    assert parsed.turns[0].text == "patched"
    assert parsed.turns[0].identity == "patched-identity"

    monkeypatch.setattr(cross_session, "_codex_is_interactive", lambda _meta: False)
    assert cross_session._codex_tail(session) is None


def test_facade_private_claude_policy_hooks_still_drive_parser(
    tmp_path, monkeypatch,
):
    session = tmp_path / "claude.jsonl"
    session.write_text(json.dumps({
        "type": "user", "sessionId": "claude-hook", "cwd": "/patched",
        "message": {"content": "hello"},
    }) + "\n", encoding="utf-8")
    monkeypatch.setattr(cross_session, "redact_text", lambda _value: "patched")
    monkeypatch.setattr(
        cross_session, "_turn_identity", lambda *_args: "patched-identity",
    )

    parsed = cross_session._claude_tail(session)

    assert parsed is not None
    assert parsed.turns[0].text == "patched"
    assert parsed.turns[0].identity == "patched-identity"

    monkeypatch.setattr(cross_session, "_is_synthetic", lambda _text: True)
    assert cross_session._claude_tail(session) is None


def test_facade_one_argument_codex_initial_user_override_is_compatible(
    tmp_path, monkeypatch,
):
    session = tmp_path / "codex.jsonl"
    session.write_text(json.dumps({"type": "event_msg", "payload": {
        "type": "user_message", "message": "hello",
    }}) + "\n", encoding="utf-8")
    monkeypatch.setattr(cross_session, "_codex_meta", lambda _path: {
        "id": "legacy-hook", "source": "vscode",
    })
    monkeypatch.setattr(cross_session, "_codex_initial_user", lambda _path: "hello")

    assert cross_session._codex_tail(session) is not None


def test_facade_synthetic_hook_drives_initial_user_filter(tmp_path, monkeypatch):
    session = tmp_path / "codex.jsonl"
    session.write_text(
        json.dumps({"type": "event_msg", "payload": {
            "type": "user_message", "message": "synthetic-by-policy",
        }}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        cross_session, "_is_synthetic",
        lambda text: text == "synthetic-by-policy",
    )

    assert cross_session._codex_initial_user(session) == ""


def test_facade_cli_keeps_incremental_and_context_commands(tmp_path, monkeypatch, capsys):
    state = tmp_path / "seen.json"
    calls = []

    def fake_incremental(**kwargs):
        calls.append(("incremental", kwargs))
        return "new turns"

    def fake_context(**kwargs):
        calls.append(("context", kwargs))
        return "recent context"

    monkeypatch.setattr(cross_session, "collect_incremental", fake_incremental)
    monkeypatch.setattr(cross_session, "build_prompt_context", fake_context)

    assert cross_session.main([
        "incremental", "--state-file", str(state), "--window-hours", "12",
    ]) == 0
    assert capsys.readouterr().out == "new turns\n"
    assert calls.pop() == ("incremental", {
        "state_file": str(state), "window_hours": 12,
    })

    assert cross_session.main(["context", "--window-hours", "6", "--max-chars", "321"]) == 0
    assert capsys.readouterr().out == "recent context\n"
    assert calls.pop() == ("context", {"window_hours": 6, "max_chars": 321})
