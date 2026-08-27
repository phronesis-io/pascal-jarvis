"""End-to-end Claude/Codex compilation into source-traceable memory."""

from __future__ import annotations

import json
from pathlib import Path

import core.db as db_module
from core.cross_session_index import index_sessions
from core.memory_compiler import apply_compile_result, compiled_context, prepare_batch


def _write(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def test_claude_and_codex_become_silent_compiled_memory(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "jarvis.db")
    db_module._connection = None
    claude = tmp_path / "claude" / "project" / "human.jsonl"
    codex = tmp_path / "codex" / "2026" / "08" / "human.jsonl"
    _write(claude, [{
        "type": "user", "sessionId": "claude-human", "cwd": "/work/alpha",
        "timestamp": "2026-08-13T10:00:00Z",
        "message": {"content": "Claude 里决定保留资产清单"},
    }])
    _write(codex, [
        {"type": "session_meta", "payload": {
            "id": "codex-human", "cwd": "/work/beta", "source": "vscode",
            "thread_source": "user", "originator": "Codex Desktop",
        }},
        {"type": "event_msg", "timestamp": "2026-08-13T11:00:00Z",
         "payload": {"type": "user_message", "message": "Codex 里决定补齐备份"}},
    ])
    index_db = tmp_path / "cross-session.db"
    index_sessions(
        db_path=index_db, claude_root=claude.parent.parent,
        codex_root=codex.parents[2], tracker_path=tmp_path / "missing.json",
        batch_size=20,
    )
    batch = prepare_batch(index_db=index_db, batch_size=20)
    claims = [{
        "source_ref": item["source_ref"],
        "quote": item["text"],
        "kind": "decision",
        "claim_key": "asset.backup" if "备份" in item["text"] else "asset.inventory",
        "content": item["text"],
        "matter_id": "",
    } for item in batch["sources"]]
    receipt = apply_compile_result({
        "schema": "jarvis.memory-candidates.v1",
        "batch_id": batch["batch_id"],
        "claims": claims,
        "ignored_source_refs": [],
    })

    assert receipt["claim_count"] == 2
    context = compiled_context("资产备份")
    assert "Claude 里决定保留资产清单" in context
    assert "Codex 里决定补齐备份" in context
    assert "Recent External Work Sessions" not in context
    assert "Relevant Historical Work Sessions" not in context
    assert "source `session_turn:" in context
