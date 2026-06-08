"""Tests for the harness-evolve self-evolution loop (post + apply)."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
POST = REPO / "tasks" / "harness_evolve_post.py"
APPLY = REPO / "tasks" / "harness_apply.py"


@pytest.fixture
def env(tmp_path):
    """A temp MEMORY_DIR + JARVIS_DIR with a minimal memory layout."""
    mem = tmp_path / "memory"
    (mem / "hot").mkdir(parents=True)
    (mem / "system").mkdir(parents=True)
    (mem / "hot" / "behavioral_rules.md").write_text("## 6. 尺度\n- 旧投资规则\n", encoding="utf-8")
    (mem / "system" / "open_threads.md").write_text("# 开放话题\n## 1. 活线\n", encoding="utf-8")
    jarvis = tmp_path / "jarvis"
    jarvis.mkdir()
    e = dict(os.environ, MEMORY_DIR=str(mem), JARVIS_DIR=str(jarvis))
    return e, mem, jarvis


def _run(script, env, stdin="", args=None):
    return subprocess.run(
        [sys.executable, str(script), *(args or [])],
        input=stdin, env=env, capture_output=True, text=True,
    )


def test_post_applies_a_level_hygiene(env):
    e, mem, jarvis = env
    payload = json.dumps({
        "hygiene": [
            {"op": "update", "file": "system/open_threads.md", "content": "归档死线程 X"},
            {"op": "replace", "file": "system/open_threads.md", "old": "## 1. 活线", "new": "## 1. 活线（更新）"},
        ],
        "proposals": [],
        "digest": "",
    })
    r = _run(POST, e, stdin=payload)
    assert r.returncode == 0, r.stderr
    txt = (mem / "system" / "open_threads.md").read_text(encoding="utf-8")
    assert "归档死线程 X" in txt
    assert "## 1. 活线（更新）" in txt
    # No proposals → no Feishu digest (打扰低频)
    assert r.stdout.strip() == ""
    # changelog written
    assert (jarvis / "harness_changelog.md").exists()


def test_post_blocks_a_level_edit_to_protected_contract(env):
    e, mem, jarvis = env
    payload = json.dumps({
        "hygiene": [{"op": "replace", "file": "hot/behavioral_rules.md",
                     "old": "- 旧投资规则", "new": "- 偷改的规则"}],
        "proposals": [],
    })
    r = _run(POST, e, stdin=payload)
    assert r.returncode == 0
    # behavioral_rules untouched — A-level may never edit the loaded contract
    assert "偷改的规则" not in (mem / "hot" / "behavioral_rules.md").read_text(encoding="utf-8")
    assert "BLOCKED A-level edit to protected" in r.stderr


def test_post_queues_b_level_proposals_and_emits_digest(env):
    e, mem, jarvis = env
    payload = json.dumps({
        "hygiene": [],
        "proposals": [{
            "target": "hot/behavioral_rules.md",
            "summary": "收窄投资",
            "old": "- 旧投资规则",
            "new": "- 新投资规则（纯择时不推）",
            "rationale": "Pascal 06-07 明确收窄",
            "signal": "daily_log + 2 次确认",
        }],
        "digest": "🧬 提案 #1 收窄投资",
    })
    r = _run(POST, e, stdin=payload)
    assert r.returncode == 0, r.stderr
    # Proposal queued, NOT applied
    assert "新投资规则" not in (mem / "hot" / "behavioral_rules.md").read_text(encoding="utf-8")
    pending = (jarvis / "harness_proposals_pending.jsonl").read_text(encoding="utf-8").strip()
    rec = json.loads(pending)
    assert rec["id"] == 1 and rec["status"] == "pending"
    # Digest sent to user with approval instructions
    assert "harness 通过 1" in r.stdout


def test_post_noop_on_heartbeat_ok(env):
    e, mem, jarvis = env
    r = _run(POST, e, stdin="HEARTBEAT_OK")
    assert r.returncode == 0
    assert r.stdout.strip() == ""
    assert not (jarvis / "harness_proposals_pending.jsonl").exists()
    # state still stamped
    assert (jarvis / ".harness_evolve_state").exists()


def test_apply_approve_applies_and_dequeues(env):
    e, mem, jarvis = env
    # Queue a proposal first
    _run(POST, e, stdin=json.dumps({"proposals": [{
        "target": "hot/behavioral_rules.md", "summary": "收窄投资",
        "old": "- 旧投资规则", "new": "- 新投资规则", "rationale": "x", "signal": "y",
    }]}))
    r = _run(APPLY, e, args=["--approve", "1"])
    assert r.returncode == 0, r.stderr
    assert "新投资规则" in (mem / "hot" / "behavioral_rules.md").read_text(encoding="utf-8")
    # dequeued
    assert _run(APPLY, e, args=["--list"]).stdout.strip().startswith("（无")
    assert "APPLIED #1" in (jarvis / "harness_changelog.md").read_text(encoding="utf-8")


def test_apply_anchor_drift_keeps_pending(env):
    e, mem, jarvis = env
    _run(POST, e, stdin=json.dumps({"proposals": [{
        "target": "hot/behavioral_rules.md", "summary": "x",
        "old": "- 不存在的锚点", "new": "- 新", "rationale": "", "signal": "",
    }]}))
    r = _run(APPLY, e, args=["--approve", "1"])
    assert "漂移" in r.stdout
    # still pending for regeneration
    assert "#1" in _run(APPLY, e, args=["--list"]).stdout


def test_apply_reject_dequeues_without_edit(env):
    e, mem, jarvis = env
    _run(POST, e, stdin=json.dumps({"proposals": [{
        "target": "hot/behavioral_rules.md", "summary": "x",
        "old": "- 旧投资规则", "new": "- 新", "rationale": "", "signal": "",
    }]}))
    r = _run(APPLY, e, args=["--reject", "1"])
    assert r.returncode == 0
    assert "- 新" not in (mem / "hot" / "behavioral_rules.md").read_text(encoding="utf-8")
    assert _run(APPLY, e, args=["--list"]).stdout.strip().startswith("（无")
