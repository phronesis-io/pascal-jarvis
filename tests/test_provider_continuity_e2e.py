"""Session-level provider failover and continuity scenario."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

import core.db as db_module
from core import codex_fallback, model_fallback, provider_health
from core.cross_session_index import index_sessions
from core.matter_bridge import record_turn
from core.memory_compiler import apply_compile_result, prepare_batch
from core.prompt import build_system_prompt


ROOT = Path(__file__).resolve().parent.parent
BOT_SOURCE = (ROOT / "bot.sh").read_text(encoding="utf-8")
LIFECYCLE_HELPERS = ROOT / "scripts" / "process_lifecycle.sh"


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


def _compile_whitepaper_claim(codex_root: Path, state_root: Path) -> None:
    index_db = state_root / "cross-session.db"
    index_sessions(
        db_path=index_db,
        claude_root=state_root / "empty-claude",
        codex_root=codex_root,
        tracker_path=state_root / "missing-tracker.json",
        batch_size=20,
    )
    batch = prepare_batch(index_db=index_db, batch_size=64)
    assert batch is not None
    source = next(
        item for item in batch["sources"]
        if item["text"] == "继续白皮书节奏安排"
    )
    apply_compile_result({
        "schema": "jarvis.memory-candidates.v1",
        "batch_id": batch["batch_id"],
        "claims": [{
            "source_ref": source["source_ref"],
            "quote": "继续白皮书节奏安排",
            "kind": "todo",
            "claim_key": "whitepaper.next_work",
            "content": "继续白皮书节奏安排",
            "matter_id": source.get("matter_id", ""),
        }],
        "ignored_source_refs": [
            item["source_ref"] for item in batch["sources"]
            if item["source_ref"] != source["source_ref"]
        ],
    })


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


def _write_usage_limited_codex(path: Path) -> None:
    path.write_text(
        """#!/usr/bin/env python3
import json
import sys

if sys.argv[1:3] == ["login", "status"]:
    print("Logged in")
    raise SystemExit(0)

print(json.dumps({"type": "thread.started", "thread_id": "thread-limit"}))
print(json.dumps({"type": "turn.started"}))
message = "You've hit your usage limit. Try again Aug 18."
print(json.dumps({"type": "error", "message": message}))
print(json.dumps({"type": "turn.failed", "error": {"message": message}}))
raise SystemExit(1)
""",
        encoding="utf-8",
    )
    path.chmod(0o755)


def _write_openai_python_wrapper(path: Path) -> None:
    path.write_text(
        """#!/bin/bash
if [ "${1:-}" = "-m" ] && [ "${2:-}" = "core.openai_fallback" ]; then
  cat >/dev/null
  printf '%s\n' '已由最终 GPT 备用通道接管'
  exit 0
fi
exec "$REAL_PYTHON" "$@"
""",
        encoding="utf-8",
    )
    path.chmod(0o755)


def _write_crashing_command_python(path: Path) -> None:
    path.write_text(
        """#!/bin/bash
if [ "${1:-}" = "-c" ]; then
  exec "$REAL_PYTHON" "$@"
fi
if [ "${1:-}" = "-m" ] && [ "${2:-}" = "core.matter_bridge" ]; then
  printf committed > "$COMMAND_SIDE_EFFECT"
  exit 70
fi
exec "$REAL_PYTHON" "$@"
""",
        encoding="utf-8",
    )
    path.chmod(0o755)


def _bot_function(name: str) -> str:
    """Extract one exact top-level production function from bot.sh."""
    start_match = re.search(rf"(?m)^{re.escape(name)}\(\) \{{\n", BOT_SOURCE)
    assert start_match is not None, f"bot function not found: {name}"
    end_match = re.search(r"(?m)^}\n", BOT_SOURCE[start_match.end():])
    assert end_match is not None, f"bot function is not closed: {name}"
    end = start_match.end() + end_match.end()
    return BOT_SOURCE[start_match.start():end]


def _write_fake_claude(path: Path) -> None:
    path.write_text(
        """#!/bin/bash
printf '%s\n' called >> "$FAKE_CLAUDE_LOG"
printf '%s' '{"subtype":"success","result":"You have hit your weekly limit - resets Aug 15 at 3am (Asia/Shanghai)"}'
""",
        encoding="utf-8",
    )
    path.chmod(0o755)


def _write_fake_lark(path: Path) -> None:
    path.write_text(
        """#!/bin/bash
printf '%s\n' "$*" >> "$FAKE_LARK_LOG"
printf '%s\n' '{"data":{"message_id":"om-delivered-e2e"}}'
""",
        encoding="utf-8",
    )
    path.chmod(0o755)


def _wait_for_turn(db_path: Path, conv_key: str) -> dict:
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            with sqlite3.connect(db_path) as connection:
                connection.row_factory = sqlite3.Row
                row = connection.execute(
                    "SELECT * FROM conversation_turns WHERE conv_key=? "
                    "AND role='assistant' ORDER BY id DESC LIMIT 1",
                    (conv_key,),
                ).fetchone()
            if row:
                return dict(row)
        except sqlite3.Error:
            pass
        time.sleep(0.05)
    raise AssertionError("bot did not persist the delivered assistant turn")


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
    _compile_whitepaper_claim(codex_root, tmp_path / "compile-state")

    system_before = build_system_prompt(
        str(jarvis_dir), str(memory_dir), str(session_dir), "lark-session",
        "ou_owner", "2026-08-12 18:00", str(tracker),
    )
    assert "继续白皮书节奏安排" in system_before
    assert "先完成第四章审阅，再冻结第五章范围" not in system_before

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


def test_production_handler_weekly_limit_routes_codex_and_records_continuity(
    tmp_path, monkeypatch,
):
    """Extracted production handlers: limit -> Codex -> Lark receipt -> ledger."""
    jarvis_dir = tmp_path / "jarvis"
    memory_dir = jarvis_dir / "memory"
    work_dir = jarvis_dir / "work"
    claude_sessions = jarvis_dir / "claude-sessions"
    codex_sessions = jarvis_dir / "codex-sessions"
    jobs_dir = jarvis_dir / "jobs"
    bin_dir = tmp_path / "bin"
    for directory in (
        memory_dir, work_dir, claude_sessions, codex_sessions, jobs_dir, bin_dir,
    ):
        directory.mkdir(parents=True, exist_ok=True)
    db_path = jarvis_dir / "data" / "jarvis.db"
    tracker = jarvis_dir / "active_sessions.json"
    tracker.write_text(json.dumps({
        "ou_owner": {"session_id": "session-bot-e2e", "counter": 1},
    }), encoding="utf-8")
    (jarvis_dir / "jarvis.yaml").write_text(
        "data_dir: " + str(jarvis_dir / "data") + "\n"
        "work_dir: " + str(work_dir) + "\n"
        "lark:\n  user_id: ou_owner\n",
        encoding="utf-8",
    )
    _write_codex_session(codex_sessions / "2026" / "08" / "owner.jsonl")

    fake_claude = bin_dir / "claude"
    fake_codex = bin_dir / "codex-bin"
    fake_lark = bin_dir / "lark-cli"
    _write_fake_claude(fake_claude)
    _write_fake_codex(fake_codex)
    _write_fake_lark(fake_lark)
    with (jarvis_dir / "jarvis.yaml").open("a", encoding="utf-8") as handle:
        handle.write(
            "codex:\n  fallback_enabled: true\n"
            f"  binary: {fake_codex}\n  fallback_model: gpt-bot-e2e\n"
        )

    # Exact production handler functions, with only external adapters mocked. The
    # provider branch, lock ownership, reliable delivery CLI, and successful
    # matter-record boundary all remain the bot's real source.
    harness = tmp_path / "bot-provider-e2e.sh"
    harness.write_text(
        "set -uo pipefail\n"
        f'source "{LIFECYCLE_HELPERS}"\n'
        "process_start_token() { printf 'test-start\\n'; }\n"
        "log(){ printf '[%s] %s\\n' \"$1\" \"${*:2}\" >> \"$LOG_FILE\"; }\n"
        "log_warn(){ log WARN \"$@\"; }\n"
        "log_info(){ log INFO \"$@\"; }\n"
        "log_err(){ log ERROR \"$@\"; }\n"
        "lark_remove_reaction(){ :; }\n"
        "lark_reply_text(){ printf 'direct:%s\\n' \"$*\" >> \"$FAKE_LARK_LOG\"; }\n"
        "load_memory(){ printf 'isolated memory'; }\n"
        "process_actions(){ printf '%s' \"$1\"; }\n"
        "resolve_memorial_thread_after_reply(){ :; }\n"
        + _bot_function("looks_like_error")
        + _bot_function("delivery_reply_reliable")
        + _bot_function("run_codex_locked")
        + _bot_function("with_primary_model_credential")
        + _bot_function("with_openai_credential")
        + _bot_function("handle_message")
        + "handle_message \"$@\"\nwait\n",
        encoding="utf-8",
    )
    syntax = subprocess.run(
        ["bash", "-n", str(harness)], capture_output=True, text=True,
    )
    assert syntax.returncode == 0, syntax.stderr

    claude_log = tmp_path / "claude.log"
    lark_log = tmp_path / "lark.log"
    prompt_log = tmp_path / "codex-prompt.txt"
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
        "PYTHONPATH": str(ROOT),
        "JARVIS_DIR": str(jarvis_dir),
        "JARVIS_DB_PATH": str(db_path),
        "MEMORY_DIR": str(memory_dir),
        "WORK_DIR": str(work_dir),
        "CLAUDE_PROJECT_DIR": str(claude_sessions),
        "SESSION_TRACKER": str(tracker),
        "JOBS_DIR": str(jobs_dir),
        "LOG_FILE": str(tmp_path / "bot.log"),
        "USER_ID": "ou_owner",
        "OWNER_NAME": "Pascal",
        "MAIN_MODEL": "opus",
        "MAX_SESSION_SIZE": "512000",
        "CLAUDE_BACKUP_ENABLED": "false",
        "CLAUDE_BACKUP2_ENABLED": "false",
        "CODEX_FALLBACK_ENABLED": "true",
        "CODEX_FALLBACK_MODEL": "gpt-bot-e2e",
        "CODEX_FALLBACK_BINARY": str(fake_codex),
        "CODEX_FALLBACK_TIMEOUT": "10",
        "OPENAI_FALLBACK_ENABLED": "false",
        "FAKE_CLAUDE_LOG": str(claude_log),
        "FAKE_LARK_LOG": str(lark_log),
        "FAKE_CODEX_PROMPT": str(prompt_log),
        "CROSS_SESSION_CLAUDE_ROOT": str(claude_sessions),
        "CROSS_SESSION_CODEX_ROOT": str(codex_sessions),
    }
    result = subprocess.run(
        [
            "bash", str(harness), "ou_owner", "安排白皮书节奏", "om-user-e2e",
            "session-bot-e2e", "", "p2p", "ou_owner",
            "conversation:ou_owner", "", "",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "command not found" not in result.stderr
    assert claude_log.read_text(encoding="utf-8").splitlines() == ["called"]
    assert "安排白皮书节奏" in prompt_log.read_text(encoding="utf-8")
    delivery_log = lark_log.read_text(encoding="utf-8")
    assert "+messages-reply" in delivery_log
    assert "om-user-e2e" in delivery_log
    assert "已由 Codex 接管并保留白皮书上下文" in delivery_log
    assert "Codex 接手" in delivery_log

    turn = _wait_for_turn(db_path, "ou_owner")
    assert turn["provider"] == "Codex"
    assert turn["model"] == "gpt-bot-e2e"
    assert "已由 Codex 接管" in turn["text"]
    with sqlite3.connect(db_path) as connection:
        session = connection.execute(
            "SELECT thread_id, model FROM codex_conversation_sessions "
            "WHERE conv_key=?",
            ("conversation:ou_owner",),
        ).fetchone()
    assert session == ("thread-e2e", "gpt-bot-e2e")
    assert not (jarvis_dir / ".session_lock_session-bot-e2e").exists()

    # The first real failure/success observations now steer the next turn.
    # Primary remains account-limited and Codex is healthy, so no second
    # doomed Claude call is allowed.
    second = subprocess.run(
        [
            "bash", str(harness), "ou_owner", "继续下一步", "om-user-e2e-2",
            "session-bot-e2e", "", "p2p", "ou_owner",
            "conversation:ou_owner", "", "",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert second.returncode == 0, second.stderr
    assert claude_log.read_text(encoding="utf-8").splitlines() == ["called"]
    assert "继续下一步" in prompt_log.read_text(encoding="utf-8")

    # Once the final configured fallback is also cooling, the handler must
    # fail closed for this turn. It may report the bounded product error, but
    # must not probe the account-limited primary or replay Codex.
    provider_health.observe(
        "codex", "unhealthy", "request_failed", root=jarvis_dir,
    )
    codex_prompt_before = prompt_log.read_text(encoding="utf-8")
    third = subprocess.run(
        [
            "bash", str(harness), "ou_owner", "再试一次", "om-user-e2e-3",
            "session-bot-e2e", "", "p2p", "ou_owner",
            "conversation:ou_owner", "", "",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert third.returncode == 0, third.stderr
    assert claude_log.read_text(encoding="utf-8").splitlines() == ["called"]
    assert prompt_log.read_text(encoding="utf-8") == codex_prompt_before
    final_log = lark_log.read_text(encoding="utf-8")
    assert "当前已配置的模型通道都在恢复中" in final_log
    assert "这次请求没有执行" in final_log

    monkeypatch.setattr(db_module, "DB_PATH", db_path)
    if db_module._connection is not None:
        db_module._connection.close()
    db_module._connection = None
    monkeypatch.setenv("JARVIS_DIR", str(jarvis_dir))
    monkeypatch.setenv("CROSS_SESSION_CLAUDE_ROOT", str(claude_sessions))
    monkeypatch.setenv("CROSS_SESSION_CODEX_ROOT", str(codex_sessions))
    _compile_whitepaper_claim(codex_sessions, jarvis_dir / "compile-state")
    next_prompt = build_system_prompt(
        str(jarvis_dir), str(memory_dir), str(claude_sessions),
        "session-bot-e2e", "ou_owner", "2026-08-12 19:00", str(tracker),
    )
    assert "Codex / gpt-bot-e2e" in next_prompt
    assert "继续白皮书节奏安排" in next_prompt


def test_production_handler_codex_usage_limit_reaches_final_gpt(
    tmp_path, monkeypatch,
):
    """Real handler: Claude limit -> Codex preturn limit -> GPT -> Lark."""
    jarvis_dir = tmp_path / "jarvis"
    memory_dir = jarvis_dir / "memory"
    work_dir = jarvis_dir / "work"
    claude_sessions = jarvis_dir / "claude-sessions"
    jobs_dir = jarvis_dir / "jobs"
    bin_dir = tmp_path / "bin"
    for directory in (
        memory_dir, work_dir, claude_sessions, jobs_dir, bin_dir,
    ):
        directory.mkdir(parents=True, exist_ok=True)
    db_path = jarvis_dir / "data" / "jarvis.db"
    tracker = jarvis_dir / "active_sessions.json"
    tracker.write_text(json.dumps({
        "ou_owner": {"session_id": "session-gpt-e2e", "counter": 1},
    }), encoding="utf-8")
    (jarvis_dir / "jarvis.yaml").write_text(
        "data_dir: " + str(jarvis_dir / "data") + "\n"
        "work_dir: " + str(work_dir) + "\n"
        "lark:\n  user_id: ou_owner\n",
        encoding="utf-8",
    )

    fake_claude = bin_dir / "claude"
    fake_codex = bin_dir / "codex-bin"
    fake_lark = bin_dir / "lark-cli"
    fake_python = bin_dir / "python3"
    _write_fake_claude(fake_claude)
    _write_usage_limited_codex(fake_codex)
    _write_fake_lark(fake_lark)
    _write_openai_python_wrapper(fake_python)

    harness = tmp_path / "bot-terminal-fallback-e2e.sh"
    harness.write_text(
        "set -uo pipefail\n"
        f'source "{LIFECYCLE_HELPERS}"\n'
        "process_start_token() { printf 'test-start\\n'; }\n"
        "log(){ printf '[%s] %s\\n' \"$1\" \"${*:2}\" >> \"$LOG_FILE\"; }\n"
        "log_warn(){ log WARN \"$@\"; }\n"
        "log_info(){ log INFO \"$@\"; }\n"
        "log_err(){ log ERROR \"$@\"; }\n"
        "lark_remove_reaction(){ :; }\n"
        "lark_reply_text(){ printf 'direct:%s\\n' \"$*\" >> \"$FAKE_LARK_LOG\"; }\n"
        "load_memory(){ printf 'isolated memory'; }\n"
        "process_actions(){ printf '%s' \"$1\"; }\n"
        "resolve_memorial_thread_after_reply(){ :; }\n"
        + _bot_function("looks_like_error")
        + _bot_function("delivery_reply_reliable")
        + _bot_function("run_codex_locked")
        + _bot_function("with_primary_model_credential")
        + _bot_function("with_openai_credential")
        + _bot_function("handle_message")
        + "handle_message \"$@\"\nwait\n",
        encoding="utf-8",
    )
    syntax = subprocess.run(
        ["bash", "-n", str(harness)], capture_output=True, text=True,
    )
    assert syntax.returncode == 0, syntax.stderr

    lark_log = tmp_path / "lark.log"
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
        "REAL_PYTHON": sys.executable,
        "PYTHONPATH": str(ROOT),
        "JARVIS_DIR": str(jarvis_dir),
        "JARVIS_DB_PATH": str(db_path),
        "MEMORY_DIR": str(memory_dir),
        "WORK_DIR": str(work_dir),
        "CLAUDE_PROJECT_DIR": str(claude_sessions),
        "SESSION_TRACKER": str(tracker),
        "JOBS_DIR": str(jobs_dir),
        "LOG_FILE": str(tmp_path / "bot.log"),
        "USER_ID": "ou_owner",
        "OWNER_NAME": "Pascal",
        "MAIN_MODEL": "opus",
        "MAX_SESSION_SIZE": "512000",
        "CLAUDE_BACKUP_ENABLED": "false",
        "CLAUDE_BACKUP2_ENABLED": "false",
        "CODEX_FALLBACK_ENABLED": "true",
        "CODEX_FALLBACK_MODEL": "gpt-codex-limited",
        "CODEX_FALLBACK_BINARY": str(fake_codex),
        "CODEX_FALLBACK_TIMEOUT": "10",
        "OPENAI_FALLBACK_ENABLED": "true",
        "OPENAI_FALLBACK_MODEL": "gpt-api-final",
        "OPENAI_API_KEY": "sk-test",
        "FAKE_CLAUDE_LOG": str(tmp_path / "claude.log"),
        "FAKE_LARK_LOG": str(lark_log),
    }
    result = subprocess.run(
        [
            "bash", str(harness), "ou_owner", "继续完成白皮书", "om-gpt-e2e",
            "session-gpt-e2e", "", "p2p", "ou_owner",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert "command not found" not in result.stderr
    logs = (tmp_path / "bot.log").read_text(encoding="utf-8")
    assert "Codex fallback failed (exit=75" in logs
    assert "OpenAI fallback succeeded" in logs
    delivery_log = lark_log.read_text(encoding="utf-8")
    assert "已由最终 GPT 备用通道接管" in delivery_log
    assert "GPT 兜底" in delivery_log
    turn = _wait_for_turn(db_path, "ou_owner")
    assert turn["provider"] == "GPT fallback"
    assert turn["model"] == "gpt-api-final"


def test_queued_handler_refuses_execution_after_logical_session_switch(tmp_path):
    """A queued A turn must not follow a physical rotation into Matter B."""
    jarvis_dir = tmp_path / "jarvis"
    memory_dir = jarvis_dir / "memory"
    work_dir = jarvis_dir / "work"
    sessions = jarvis_dir / "sessions"
    jobs_dir = jarvis_dir / "jobs"
    for directory in (memory_dir, work_dir, sessions, jobs_dir):
        directory.mkdir(parents=True, exist_ok=True)
    tracker = jarvis_dir / "active_sessions.json"
    tracker.write_text(json.dumps({
        "ou_owner": {
            "session_id": "session-a", "counter": 1,
            "context_key": "matter:alpha",
        },
    }), encoding="utf-8")
    (jarvis_dir / "jarvis.yaml").write_text(
        "data_dir: " + str(jarvis_dir / "data") + "\n"
        "work_dir: " + str(work_dir) + "\n"
        "lark:\n  user_id: ou_owner\n",
        encoding="utf-8",
    )
    lock = jarvis_dir / ".session_lock_session-a"
    lock.write_text(f"{os.getpid()} occupied", encoding="utf-8")
    delivery_log = tmp_path / "delivery.log"
    claude_log = tmp_path / "claude.log"
    fake_claude = tmp_path / "claude"
    fake_claude.write_text(
        "#!/bin/bash\nprintf called >> \"$FAKE_CLAUDE_LOG\"\n",
        encoding="utf-8",
    )
    fake_claude.chmod(0o755)
    harness = tmp_path / "queued-context.sh"
    harness.write_text(
        "set -uo pipefail\n"
        f'source "{LIFECYCLE_HELPERS}"\n'
        "log_warn(){ :; }\nlog_info(){ :; }\nlog_err(){ :; }\n"
        "lark_remove_reaction(){ :; }\n"
        "lark_reply_text(){ :; }\n"
        "load_memory(){ printf isolated; }\n"
        "delivery_reply_reliable(){ printf '%s' \"$2\" > \"$DELIVERY_LOG\"; }\n"
        + _bot_function("with_primary_model_credential")
        + _bot_function("with_openai_credential")
        + _bot_function("handle_message")
        + "handle_message \"$@\"\n",
        encoding="utf-8",
    )
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ.get('PATH', '')}",
        "PYTHONPATH": str(ROOT),
        "JARVIS_DIR": str(jarvis_dir),
        "JARVIS_DB_PATH": str(jarvis_dir / "data" / "jarvis.db"),
        "MEMORY_DIR": str(memory_dir),
        "WORK_DIR": str(work_dir),
        "CLAUDE_PROJECT_DIR": str(sessions),
        "SESSION_TRACKER": str(tracker),
        "JOBS_DIR": str(jobs_dir),
        "LOG_FILE": str(tmp_path / "bot.log"),
        "USER_ID": "ou_owner",
        "MAIN_MODEL": "opus",
        "CLAUDE_BACKUP_ENABLED": "false",
        "CODEX_FALLBACK_ENABLED": "false",
        "OPENAI_FALLBACK_ENABLED": "false",
        "DELIVERY_LOG": str(delivery_log),
        "FAKE_CLAUDE_LOG": str(claude_log),
    }
    process = subprocess.Popen(
        [
            "bash", str(harness), "ou_owner", "仍属于 A 的排队消息", "om-queued",
            "session-a", "", "p2p", "ou_owner", "matter:alpha", "alpha", "",
        ],
        cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    tracker.write_text(json.dumps({
        "ou_owner": {
            "session_id": "session-b", "counter": 2,
            "context_key": "matter:beta",
        },
    }), encoding="utf-8")
    lock.unlink(missing_ok=True)
    stdout, stderr = process.communicate(timeout=15)

    assert process.returncode == 0, stderr or stdout
    assert "command not found" not in stderr
    assert "没有跨会话执行" in delivery_log.read_text(encoding="utf-8")
    assert not claude_log.exists()
    assert not list(jarvis_dir.glob(".session_lock_*"))


def test_deterministic_command_process_hard_exit_fails_closed(tmp_path):
    """A commit followed by os._exit-equivalent must never reach the model."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_python = bin_dir / "python3"
    _write_crashing_command_python(fake_python)
    harness = tmp_path / "matter-command-hard-exit.sh"
    harness.write_text(
        "set -uo pipefail\n"
        "log_err(){ printf '%s\\n' \"$*\" >> \"$LOG_FILE\"; }\n"
        + _bot_function("run_matter_command")
        + "run_matter_command '新开会话 原子命令' owner owner p2p\n",
        encoding="utf-8",
    )
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
        "PYTHONPATH": str(ROOT),
        "REAL_PYTHON": sys.executable,
        "COMMAND_SIDE_EFFECT": str(tmp_path / "committed"),
        "LOG_FILE": str(tmp_path / "bot.log"),
        "SESSION_TRACKER": str(tmp_path / "tracker.json"),
        "CLAUDE_PROJECT_DIR": str(tmp_path / "sessions"),
        "JARVIS_DIR": str(tmp_path),
    }

    result = subprocess.run(
        ["bash", str(harness)], cwd=ROOT, env=env,
        capture_output=True, text=True, timeout=10,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["handled"] is True
    assert payload["command_process_error"] is True
    assert "不会交给模型" in payload["reply"]
    assert (tmp_path / "committed").read_text(encoding="utf-8") == "committed"
    assert "status=70" in (tmp_path / "bot.log").read_text(encoding="utf-8")
