"""Regression tests for the weekly-review heartbeat post-hook."""

import json
import os
import subprocess
import sys
from pathlib import Path


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


def _assert_weekly_card(result: subprocess.CompletedProcess) -> dict:
    assert result.returncode == 0, result.stderr
    card = json.loads(result.stdout)
    encoded = json.dumps(card, ensure_ascii=False)
    assert "周省" in encoded
    return card


def test_structured_review_renders_without_time_source_argument(tmp_path):
    result = _run_post(
        tmp_path,
        json.dumps({
            "user_message": "本周完成了两件重要事项。",
            "auto_actions": [],
        }, ensure_ascii=False),
    )

    card = _assert_weekly_card(result)
    assert "本周完成了两件重要事项" in json.dumps(card, ensure_ascii=False)
    assert (tmp_path / "data" / ".weekly_review_stamp").exists()
    assert len(list((tmp_path / "views").glob("*.json"))) == 1


def test_plain_text_review_renders_without_time_source_argument(tmp_path):
    result = _run_post(
        tmp_path,
        "本周保持了稳定节奏，也发现了下一步需要收紧的边界。",
    )

    card = _assert_weekly_card(result)
    assert "稳定节奏" in json.dumps(card, ensure_ascii=False)
    assert (tmp_path / "data" / ".weekly_review_stamp").exists()
    assert len(list((tmp_path / "views").glob("*.json"))) == 1
