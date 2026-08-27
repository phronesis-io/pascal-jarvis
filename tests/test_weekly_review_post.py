"""Regression tests for the deterministic weekly Matter review."""

import json
import os
import subprocess
import sys
from pathlib import Path

from core.heartbeat import HeartbeatRunner


ROOT = Path(__file__).resolve().parent.parent
POST = ROOT / "tasks" / "weekly_review_post.py"


def _run_post(tmp_path: Path, payload: str) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "JARVIS_DIR": str(tmp_path),
        "MEMORY_DIR": str(tmp_path / "memory"),
    }
    return subprocess.run(
        [sys.executable, str(POST)],
        input=payload,
        capture_output=True,
        text=True,
        env=env,
    )


def _report(**sections) -> dict:
    return {
        "schema": "jarvis.matter-review.v1",
        "generated_at": "2026-08-27T12:00:00+08:00",
        "period_days": 7,
        "summary": {},
        "outcomes": sections.get("outcomes", []),
        "closure_candidates": sections.get("closure_candidates", []),
        "attention": sections.get("attention", []),
        "next_actions": sections.get("next_actions", []),
        "integrity": {},
        "material": bool(sections),
        "authority": {"read_only": True},
    }


def _assert_weekly_card(result: subprocess.CompletedProcess) -> dict:
    assert result.returncode == 0, result.stderr
    card = json.loads(result.stdout)
    encoded = json.dumps(card, ensure_ascii=False)
    assert "周省" in encoded
    return card


def test_confirmed_results_render_as_one_bounded_card(tmp_path):
    result = _run_post(
        tmp_path,
        json.dumps(_report(outcomes=[{
            "id": "mat_1",
            "title": "白皮书路线图",
            "outcome": "已经发布并读回",
        }]), ensure_ascii=False),
    )

    card = _assert_weekly_card(result)
    encoded = json.dumps(card, ensure_ascii=False)
    assert "本周形成的结果" in encoded
    assert "已经发布并读回" in encoded
    assert (tmp_path / "data" / ".weekly_review_stamp").exists()
    assert len(list((tmp_path / "views").glob("*.json"))) == 1


def test_empty_review_is_silent_but_records_success(tmp_path):
    result = _run_post(tmp_path, json.dumps(_report()))

    assert result.returncode == 0
    assert result.stdout == ""
    assert (tmp_path / "data" / ".weekly_review_stamp").exists()


def test_malformed_review_fails_without_spending_the_weekly_occurrence(tmp_path):
    result = _run_post(tmp_path, "not-json")

    assert result.returncode == 1
    assert result.stdout == ""
    assert not (tmp_path / "data" / ".weekly_review_stamp").exists()


def test_suppressed_card_does_not_spend_the_weekly_occurrence(tmp_path):
    result = _run_post(
        tmp_path,
        json.dumps(_report(next_actions=[{
            "id": "mat_1",
            "title": "修复 HEARTBEAT_OK 路由",
            "next_action": "保留安全哨兵",
        }]), ensure_ascii=False),
    )

    assert result.returncode == 1
    assert result.stdout == ""
    assert not (tmp_path / "data" / ".weekly_review_stamp").exists()


def test_weekly_review_is_model_free_tier_zero():
    assert "weekly-review" in HeartbeatRunner.TIER0_TASKS
