"""Tests for tasks/write_claim_audit.py (REQ-88 shadow write-claim audit).

Covers: claim-detection regex (positives/negatives), confirmed vs unverified
verdicts against real tmp write-surfaces, no-claim → no output, and the
never-raises contract on malformed input/env.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from tasks.write_claim_audit import detect_claims

SCRIPT = Path(__file__).resolve().parent.parent / "tasks" / "write_claim_audit.py"


# ── Claim detection: positives ────────────────────────────────────────

def test_detects_first_person_perfective_persistence_claims():
    positives = [
        "记进memory了",
        "已写入记忆",
        "我已经把这条写进 tasks 了",
        "好的，已存档",
        "已记录到 open_threads",
        "帮你记进memory了",
        "这条反馈已经记下来了",
        "已保存到记忆",
    ]
    for text in positives:
        assert detect_claims(text), f"should detect claim: {text!r}"


# ── Claim detection: negatives (评审红线: 宁漏勿误纠) ─────────────────

def test_ignores_non_claims():
    negatives = [
        "之前已记录",              # past reference, not a fresh claim
        "你可以记录一下",           # suggestion to the user
        "你记下来了吗",             # question
        "我会记下来",               # future intent, not perfective
        "还没写入记忆",             # negation
        "如果已记录就跳过",          # conditional
        "已同步到服务器",            # persistence verb but not a memory target
        "请记进memory",             # imperative
        "今天天气不错",              # unrelated
    ]
    for text in negatives:
        assert not detect_claims(text), f"should NOT detect claim: {text!r}"


def test_claims_inside_code_blocks_are_ignored():
    text = "看这段代码：\n```\nlog('已写入记忆')\n```\n就这样"
    assert not detect_claims(text)
    assert detect_claims("代码跑完了。已写入记忆")


def test_detect_claims_never_raises_on_junk():
    for junk in (None, "", 123, "已" * 10000):
        detect_claims(junk)  # must not raise


# ── End-to-end: verdicts against tmp write surfaces ──────────────────

def _run(env_extra, tmp_path):
    jarvis_dir = tmp_path / "jarvis"
    jarvis_dir.mkdir(exist_ok=True)
    env = {
        **os.environ,
        "JARVIS_DIR": str(jarvis_dir),
        "MEMORY_DIR": str(tmp_path / "memory"),
        # Point auto-memory discovery at an empty tmp dir so the test never
        # scans the real ~/.claude/projects.
        "JV_CLAUDE_PROJECTS": str(tmp_path / "claude-projects"),
        **env_extra,
    }
    proc = subprocess.run(
        [sys.executable, str(SCRIPT)], env=env, capture_output=True, text=True
    )
    return proc, jarvis_dir / "data" / "write_claim_audit.jsonl"


def _rows(audit_file):
    if not audit_file.exists():
        return []
    return [json.loads(l) for l in audit_file.read_text().splitlines() if l.strip()]


def test_recent_write_yields_confirmed(tmp_path):
    mem = tmp_path / "memory"
    mem.mkdir()
    (mem / "note.md").write_text("fresh")  # mtime = now, inside window
    proc, audit = _run({"JV_REPLY": "已写入记忆，放心。"}, tmp_path)
    assert proc.returncode == 0, proc.stderr
    rows = _rows(audit)
    assert len(rows) == 1
    assert rows[0]["verdict"] == "confirmed"
    assert "jarvis_memory" in rows[0]["surfaces_hit"]
    assert "已写入记忆" in rows[0]["claim"]
    assert rows[0]["unchecked"] == ["journal"]


def test_stale_surfaces_yield_unverified(tmp_path):
    mem = tmp_path / "memory"
    mem.mkdir()
    f = mem / "note.md"
    f.write_text("old")
    old = time.time() - 3600  # far outside the 10-min window
    os.utime(f, (old, old))
    os.utime(mem, (old, old))
    proc, audit = _run({"JV_REPLY": "这条我已经记进memory了"}, tmp_path)
    assert proc.returncode == 0, proc.stderr
    rows = _rows(audit)
    assert len(rows) == 1
    assert rows[0]["verdict"] == "unverified"
    assert rows[0]["surfaces_hit"] == []


def test_no_claim_writes_nothing(tmp_path):
    (tmp_path / "memory").mkdir()
    proc, audit = _run({"JV_REPLY": "好的，明天提醒你开会。"}, tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert not audit.exists()


def test_never_raises_with_broken_env(tmp_path):
    # Bogus window value + nonexistent dirs: script must exit 0, silently.
    proc, audit = _run(
        {"JV_REPLY": "已写入记忆", "JV_WRITE_CLAIM_WINDOW_MIN": "not-a-number",
         "MEMORY_DIR": str(tmp_path / "does-not-exist")},
        tmp_path,
    )
    assert proc.returncode == 0, proc.stderr
    rows = _rows(audit)
    assert len(rows) == 1  # falls back to the default window and still logs
    assert rows[0]["verdict"] in ("confirmed", "unverified")


def test_missing_reply_or_dir_is_a_silent_noop(tmp_path):
    proc = subprocess.run(
        [sys.executable, str(SCRIPT)],
        env={**os.environ, "JARVIS_DIR": "", "JV_REPLY": "已写入记忆"},
        capture_output=True, text=True,
    )
    assert proc.returncode == 0
    proc, audit = _run({"JV_REPLY": ""}, tmp_path)
    assert proc.returncode == 0
    assert not audit.exists()
