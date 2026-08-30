"""Memory Compiler pre-hook batch/retry behavior."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
PRE = ROOT / "tasks" / "cross_session_pre.sh"
POST = ROOT / "tasks" / "cross_session_post.py"


def _session(tmp_path: Path) -> Path:
    path = tmp_path / "home" / ".claude" / "projects" / "project" / "s1.jsonl"
    path.parent.mkdir(parents=True)
    return path


def _turn(role: str, text: str, ts: str) -> str:
    return json.dumps({
        "type": role,
        "sessionId": "human",
        "cwd": "/work/project",
        "message": {"content": text},
        "timestamp": ts,
    }, ensure_ascii=False) + "\n"


def _env(tmp_path: Path) -> dict[str, str]:
    return {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "JARVIS_DIR": str(tmp_path),
        "JARVIS_DB_PATH": str(tmp_path / "jarvis.db"),
        "CROSS_SESSION_MEMORY_DB": str(tmp_path / "cross-session.db"),
        "JARVIS_PYTHON": sys.executable,
        "PYTHONPATH": str(ROOT),
    }


def _run_pre(tmp_path: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(PRE)], capture_output=True, text=True, env=_env(tmp_path),
    )


def _apply_ignored(tmp_path: Path, batch: dict) -> subprocess.CompletedProcess:
    envelope = {
        "schema": "jarvis.memory-candidates.v1",
        "batch_id": batch["batch_id"],
        "claims": [],
        "ignored_source_refs": [item["source_ref"] for item in batch["sources"]],
    }
    return subprocess.run(
        [sys.executable, str(POST)], input=json.dumps(envelope, ensure_ascii=False),
        capture_output=True, text=True, env=_env(tmp_path),
    )


def _run_post(tmp_path: Path, raw: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(POST)], input=raw, capture_output=True,
        text=True, env=_env(tmp_path),
    )


def test_pending_batch_replays_until_valid_post_then_goes_quiet(tmp_path):
    session = _session(tmp_path)
    session.write_text(
        _turn("user", "帮我保留这个决定", "2026-08-27T10:00:00Z")
        + _turn("assistant", "已经记录", "2026-08-27T10:01:00Z"),
        encoding="utf-8",
    )
    first = _run_pre(tmp_path)
    assert first.returncode == 0, first.stderr
    batch = json.loads(first.stdout)
    assert any("保留这个决定" in item["text"] for item in batch["sources"])

    retry = _run_pre(tmp_path)
    assert json.loads(retry.stdout)["batch_id"] == batch["batch_id"]

    applied = _apply_ignored(tmp_path, batch)
    assert applied.returncode == 0, applied.stderr
    quiet = _run_pre(tmp_path)
    assert quiet.returncode == 0, quiet.stderr
    assert quiet.stdout == ""


def test_append_after_applied_batch_creates_only_new_source_batch(tmp_path):
    session = _session(tmp_path)
    session.write_text(
        _turn("user", "第一条决定", "2026-08-27T10:00:00Z"), encoding="utf-8"
    )
    first = json.loads(_run_pre(tmp_path).stdout)
    assert _apply_ignored(tmp_path, first).returncode == 0

    with session.open("a", encoding="utf-8") as handle:
        handle.write(_turn("user", "第二条新决定", "2026-08-27T11:00:00Z"))
    second = json.loads(_run_pre(tmp_path).stdout)
    assert second["batch_id"] != first["batch_id"]
    assert [item["text"] for item in second["sources"]] == ["第二条新决定"]


def test_invalid_post_keeps_batch_retryable(tmp_path):
    session = _session(tmp_path)
    session.write_text(
        _turn("user", "不能丢的决定", "2026-08-27T10:00:00Z"), encoding="utf-8"
    )
    batch = json.loads(_run_pre(tmp_path).stdout)
    invalid = subprocess.run(
        [sys.executable, str(POST)], input="not json", capture_output=True,
        text=True, env=_env(tmp_path),
    )
    assert invalid.returncode == 1
    assert json.loads(_run_pre(tmp_path).stdout)["batch_id"] == batch["batch_id"]


def test_three_invalid_posts_fail_poison_batch_and_release_new_source(tmp_path):
    session = _session(tmp_path)
    session.write_text(
        _turn("user", "毒批次", "2026-08-27T10:00:00Z"), encoding="utf-8"
    )
    first = json.loads(_run_pre(tmp_path).stdout)
    with session.open("a", encoding="utf-8") as handle:
        handle.write(_turn("user", "后续新决定", "2026-08-27T11:00:00Z"))

    for attempt in range(1, 4):
        failed = subprocess.run(
            [sys.executable, str(POST)], input="not json", capture_output=True,
            text=True, env=_env(tmp_path),
        )
        assert failed.returncode == 1
        assert f'"attempts": {attempt}' in failed.stderr
        if attempt < 3:
            assert json.loads(_run_pre(tmp_path).stdout)["batch_id"] == first["batch_id"]

    next_batch = json.loads(_run_pre(tmp_path).stdout)
    assert next_batch["batch_id"] != first["batch_id"]
    assert [item["text"] for item in next_batch["sources"]] == ["后续新决定"]
    with sqlite3.connect(tmp_path / "jarvis.db") as db:
        status, attempts = db.execute(
            "SELECT status,attempts FROM memory_compile_batches WHERE id=?",
            (first["batch_id"],),
        ).fetchone()
    assert (status, attempts) == ("failed", 3)


def test_heartbeat_ok_is_a_failure_when_a_compile_batch_is_pending(tmp_path):
    session = _session(tmp_path)
    session.write_text(
        _turn("user", "不能假绿", "2026-08-27T10:00:00Z"), encoding="utf-8"
    )
    batch = json.loads(_run_pre(tmp_path).stdout)
    result = subprocess.run(
        [sys.executable, str(POST)], input="HEARTBEAT_OK", capture_output=True,
        text=True, env=_env(tmp_path),
    )
    assert result.returncode == 1
    assert "received no compile envelope" in result.stderr
    with sqlite3.connect(tmp_path / "jarvis.db") as db:
        status, attempts = db.execute(
            "SELECT status,attempts FROM memory_compile_batches WHERE id=?",
            (batch["batch_id"],),
        ).fetchone()
    assert (status, attempts) == ("pending", 1)


def test_envelope_quoting_idle_token_is_applied_not_dropped(tmp_path):
    session = _session(tmp_path)
    session.write_text(
        _turn(
            "user",
            "决定：post 钩子不能把 HEARTBEAT_OK 或 rate_limit 当引文协议词",
            "2026-08-29T10:00:00Z",
        ),
        encoding="utf-8",
    )
    batch = json.loads(_run_pre(tmp_path).stdout)
    source = next(
        item for item in batch["sources"]
        if "HEARTBEAT_OK" in item["text"] and "rate_limit" in item["text"]
    )
    envelope = {
        "schema": "jarvis.memory-candidates.v1",
        "batch_id": batch["batch_id"],
        "claims": [{
            "source_ref": source["source_ref"],
            "quote": "post 钩子不能把 HEARTBEAT_OK 或 rate_limit 当引文协议词",
            "kind": "decision",
            "claim_key": "post_hook_idle_check",
            "content": "结构化信封中的引文不能触发原始文本协议过滤",
            "matter_id": "",
        }],
        "ignored_source_refs": [
            item["source_ref"] for item in batch["sources"]
            if item["source_ref"] != source["source_ref"]
        ],
    }

    applied = _run_post(tmp_path, json.dumps(envelope, ensure_ascii=False))

    assert applied.returncode == 0, applied.stderr
    assert "jarvis.memory-compile-receipt.v1" in applied.stderr
    assert _run_pre(tmp_path).stdout == ""
