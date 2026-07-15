"""Tests for phronesis-monitor post: sentinel gate + cross-cycle flagged ledger."""

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
POST = REPO / "tasks" / "phronesis_monitor_post.py"


def _run_post(tmp_path, payload):
    import os
    return subprocess.run(
        [sys.executable, str(POST)], input=payload,
        env={**os.environ, "JARVIS_DIR": str(tmp_path)},
        capture_output=True, text=True, timeout=30)


def _ledger(tmp_path):
    path = tmp_path / "data" / "phronesis_flagged.jsonl"
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


def test_trailing_sentinel_is_suppressed_and_not_ledgered(tmp_path):
    # The exact 2026-07-15 leak: analysis prose + trailing HEARTBEAT_OK.
    leaked = ("Just team members chatting about seating and air "
              "conditioning—nothing noteworthy.\n\nHEARTBEAT_OK")
    r = _run_post(tmp_path, leaked)
    assert r.returncode == 0
    assert r.stdout.strip() == ""
    assert _ledger(tmp_path) == []


def test_bare_sentinel_is_silent(tmp_path):
    r = _run_post(tmp_path, "HEARTBEAT_OK")
    assert r.stdout.strip() == "" and _ledger(tmp_path) == []


def test_surfaced_card_is_recorded_in_flagged_ledger(tmp_path):
    content = "⚠️ 办公室有工业/塑料味，Vic、鱼刺、揽星几个人头晕，源头未定位。"
    r = _run_post(tmp_path, content)
    assert r.returncode == 0
    assert r.stdout.strip().startswith('{"config":')
    entries = _ledger(tmp_path)
    assert len(entries) == 1
    assert "工业" in entries[0]["summary"]
    assert entries[0]["ts"]  # ISO timestamp present


def test_ledger_stays_bounded(tmp_path):
    ledger = tmp_path / "data" / "phronesis_flagged.jsonl"
    ledger.parent.mkdir(parents=True)
    ledger.write_text("".join(
        json.dumps({"ts": "2026-07-15T10:00:00+08:00", "summary": f"old {i}"}) + "\n"
        for i in range(250)))
    _run_post(tmp_path, "新的值得上报的内容：客户 X 提了合同问题")
    lines = ledger.read_text().splitlines()
    assert len(lines) <= 200
    assert "客户 X" in lines[-1]
