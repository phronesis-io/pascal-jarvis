from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from core.cross_session import collect_incremental


ROOT = Path(__file__).resolve().parent.parent


def _write(path: Path, rows: list[dict]):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows))


def test_claude_and_codex_reach_silent_durable_memory(tmp_path):
    claude = tmp_path / "claude" / "project" / "human.jsonl"
    codex = tmp_path / "codex" / "2026" / "08" / "human.jsonl"
    _write(claude, [
        {"type": "user", "sessionId": "claude-human", "cwd": "/work/alpha",
         "timestamp": "2026-08-13T10:00:00Z",
         "message": {"content": "Claude 决定保留资产清单"}},
        {"type": "assistant", "sessionId": "claude-human", "cwd": "/work/alpha",
         "timestamp": "2026-08-13T10:01:00Z",
         "message": {"content": "下一步验证恢复流程"}},
    ])
    _write(codex, [
        {"type": "session_meta", "payload": {
            "id": "codex-human", "cwd": "/work/beta", "source": "vscode",
            "thread_source": "user", "originator": "Codex Desktop"}},
        {"type": "event_msg", "timestamp": "2026-08-13T11:00:00Z",
         "payload": {"type": "user_message", "message": "Codex 决定补齐备份"}},
        {"type": "event_msg", "timestamp": "2026-08-13T11:01:00Z",
         "payload": {"type": "agent_message", "message": "实现完成待验证"}},
    ])
    projected = collect_incremental(
        state_file=tmp_path / "seen.json",
        claude_root=claude.parent.parent,
        codex_root=codex.parents[2],
        tracker_path=tmp_path / "missing.json",
    )
    assert "Claude 决定保留资产清单" in projected
    assert "Codex 决定补齐备份" in projected

    memory = tmp_path / "memory"
    (memory / "system").mkdir(parents=True)
    (memory / "system" / "open_threads.md").write_text("# Open\n")
    cross_post = subprocess.run(
        [sys.executable, str(ROOT / "tasks" / "cross_session_post.py")],
        input=json.dumps({"digest": projected, "user_message": ""}, ensure_ascii=False),
        text=True, capture_output=True,
        env={**os.environ, "MEMORY_DIR": str(memory), "JARVIS_DIR": str(tmp_path)},
    )
    assert cross_post.returncode == 0
    digest = memory / "system" / "cross_session_digest.md"
    assert "Claude 决定保留资产清单" in digest.read_text()
    assert "Codex 决定补齐备份" in digest.read_text()

    consolidate_pre = subprocess.run(
        ["bash", str(ROOT / "tasks" / "memory_consolidate_pre.sh")],
        text=True, capture_output=True,
        env={**os.environ, "FORCE": "1", "MEMORY_DIR": str(memory),
             "JARVIS_DIR": str(tmp_path), "WORK_DIR": str(tmp_path)},
    )
    assert "CROSS-SESSION DIGEST" in consolidate_pre.stdout
    assert "Codex 决定补齐备份" in consolidate_pre.stdout

    consolidate_post = subprocess.run(
        [sys.executable, str(ROOT / "tasks" / "memory_consolidate_post.py")],
        input="→ UPDATE: system/open_threads.md: 已吸收 Claude 与 Codex 的资产决策",
        text=True, capture_output=True,
        env={**os.environ, "MEMORY_DIR": str(memory), "JARVIS_DIR": str(tmp_path)},
    )
    assert consolidate_post.returncode == 0
    assert consolidate_post.stdout == ""
    assert "已吸收 Claude 与 Codex" in (
        memory / "system" / "open_threads.md").read_text()

