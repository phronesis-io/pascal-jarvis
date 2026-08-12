"""Session-level provider failover and continuity scenario."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import dashboard.db as db_module
from core import codex_fallback, model_fallback
from core.matter_bridge import record_turn
from core.prompt import build_system_prompt


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "jarvis.db")
    if db_module._connection is not None:
        db_module._connection.close()
    db_module._connection = None
    yield
    if db_module._connection is not None:
        db_module._connection.close()
    db_module._connection = None


def _write_codex_session(path: Path) -> None:
    rows = [
        {"type": "session_meta", "payload": {
            "id": "owner-codex-session",
            "cwd": "/work/eigenflux",
            "source": "vscode",
            "thread_source": "user",
            "originator": "Codex Desktop",
        }},
        {"type": "event_msg", "timestamp": "2026-08-12T09:00:00Z",
         "payload": {"type": "user_message",
                     "message": "继续白皮书节奏安排"}},
        {"type": "event_msg", "timestamp": "2026-08-12T09:01:00Z",
         "payload": {"type": "agent_message",
                     "message": "先完成第四章审阅，再冻结第五章范围"}},
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def _write_fake_codex(path: Path) -> None:
    path.write_text(
        """#!/usr/bin/env python3
import json
import os
import pathlib
import sys

if sys.argv[1:3] == ["login", "status"]:
    print("Logged in")
    raise SystemExit(0)

prompt = sys.stdin.read()
pathlib.Path(os.environ["FAKE_CODEX_PROMPT"]).write_text(prompt, encoding="utf-8")
output = pathlib.Path(sys.argv[sys.argv.index("-o") + 1])
output.write_text("已由 Codex 接管并保留白皮书上下文", encoding="utf-8")
print(json.dumps({"type": "thread.started", "thread_id": "thread-e2e"}))
""",
        encoding="utf-8",
    )
    path.chmod(0o755)


def test_weekly_limit_codex_takeover_returns_context_to_next_provider(
    tmp_path, monkeypatch,
):
    jarvis_dir = tmp_path / "jarvis"
    memory_dir = tmp_path / "memory"
    session_dir = tmp_path / "sessions"
    claude_root = tmp_path / "claude"
    codex_root = tmp_path / "codex"
    tracker = jarvis_dir / "active_sessions.json"
    for path in (jarvis_dir, memory_dir, session_dir, claude_root):
        path.mkdir(parents=True, exist_ok=True)
    tracker.write_text("{}", encoding="utf-8")
    _write_codex_session(codex_root / "2026" / "08" / "owner.jsonl")

    monkeypatch.setenv("JARVIS_DIR", str(jarvis_dir))
    monkeypatch.setenv("CROSS_SESSION_CLAUDE_ROOT", str(claude_root))
    monkeypatch.setenv("CROSS_SESSION_CODEX_ROOT", str(codex_root))
    monkeypatch.setattr(model_fallback, "_notify_pascal", lambda *args: None)

    claude_error = (
        "You've hit your weekly limit · resets Aug 15 at 3am (Asia/Shanghai)"
    )
    assert model_fallback.limit_reason(claude_error) == "spend_limit"
    model_fallback.trip("spend_limit", jarvis_dir)
    assert model_fallback.gate(jarvis_dir, probe=False) == "backup"

    system_before = build_system_prompt(
        str(jarvis_dir), str(memory_dir), str(session_dir), "lark-session",
        "ou_owner", "2026-08-12 18:00", str(tracker),
    )
    assert "继续白皮书节奏安排" in system_before
    assert "先完成第四章审阅，再冻结第五章范围" in system_before

    fake_codex = tmp_path / "codex-bin"
    prompt_log = tmp_path / "codex-prompt.txt"
    _write_fake_codex(fake_codex)
    monkeypatch.setenv("FAKE_CODEX_PROMPT", str(prompt_log))
    answer = codex_fallback.run_fallback(
        content="上一下备用吧",
        conv_key="ou_owner",
        system_prompt=system_before,
        model="gpt-integration",
        timeout=10,
        work_dir=jarvis_dir,
        binary=str(fake_codex),
    )
    assert answer == "已由 Codex 接管并保留白皮书上下文"
    assert "继续白皮书节奏安排" in prompt_log.read_text(encoding="utf-8")
    assert codex_fallback.load_session("ou_owner")["thread_id"] == "thread-e2e"

    assert record_turn(
        "ou_owner", "user", "上一下备用吧", message_id="om-user",
    )
    assert record_turn(
        "ou_owner", "assistant", answer, message_id="om-answer",
        provider="Codex", model="gpt-integration", session_id="thread-e2e",
    )
    system_after = build_system_prompt(
        str(jarvis_dir), str(memory_dir), str(session_dir), "lark-session",
        "ou_owner", "2026-08-12 18:01", str(tracker),
    )
    assert "Codex / gpt-integration" in system_after
    assert answer in system_after
    assert system_after.count("继续白皮书节奏安排") == 1
