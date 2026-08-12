"""REQ-84 — daily_plan_post logs the plan but builds no card.

daily-plan is a SILENT_TASK (6/12 hallucination incident): any card printed
by the post-hook was assembled and then discarded by the heartbeat every day.
The card build is gone; PLAN_LOG must keep working — daily_reflect_pre.sh
reads today's entry for the evening plan-vs-reality comparison.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
POST = ROOT / "tasks" / "daily_plan_post.py"


def _run_post(tmp_path: Path, stdin_text: str) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "JARVIS_DIR": str(tmp_path),
        "MEMORY_DIR": str(tmp_path / "mem"),
    }
    return subprocess.run(
        [sys.executable, str(POST)],
        input=stdin_text, capture_output=True, text=True, env=env,
    )


def test_plan_logged_but_no_card_on_stdout(tmp_path):
    plan = json.dumps({"user_message": "- 09:00 深度工作\n- 14:00 例会"},
                      ensure_ascii=False)
    result = _run_post(tmp_path, plan)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "", (
        f"card build was removed (REQ-84); stdout must stay empty, "
        f"got: {result.stdout!r}"
    )

    log = tmp_path / "mem" / "system" / "daily_plan_log.jsonl"
    entries = [json.loads(line) for line in
               log.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(entries) == 1
    assert "深度工作" in entries[0]["plan"]
    assert (tmp_path / "data" / ".daily_plan_stamp").exists()


def test_heartbeat_ok_writes_nothing(tmp_path):
    result = _run_post(tmp_path, "HEARTBEAT_OK")
    assert result.returncode == 0
    assert result.stdout.strip() == ""
    assert not (tmp_path / "mem" / "system" / "daily_plan_log.jsonl").exists()
