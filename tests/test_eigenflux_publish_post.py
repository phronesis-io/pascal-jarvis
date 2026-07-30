"""Tests for tasks/eigenflux_publish_post.py — type gate + confirmation card.

Only supply/demand/insight broadcasts are allowed; "info" relays (papers/news)
are still banned. All broadcasts go through a confirmation card before publishing.
"""

import subprocess
import sys
import json
import os
import time
from pathlib import Path

from core.jsonl import read_jsonl

SCRIPT = Path(__file__).resolve().parent.parent / "tasks" / "eigenflux_publish_post.py"


def _run(payload: str, tmp_path) -> str:
    env = {"JARVIS_DIR": str(tmp_path), "PATH": "/usr/bin:/bin"}
    r = subprocess.run([sys.executable, str(SCRIPT)], input=payload,
                       capture_output=True, text=True, env=env)
    return r.stdout


def _mk(btype: str, url: str = "") -> str:
    return (
        '{"should_publish":true,"content":"some broadcast body",'
        f'"source_url":"{url}",'
        f'"notes":{{"type":"{btype}","domains":["agents"],"summary":"s",'
        '"expire_time":"2026-07-01T00:00:00Z","source_type":"original"}}'
    )


def test_info_broadcast_dropped(tmp_path):
    out = _run(_mk("info", "https://arxiv.org/abs/x"), tmp_path)
    assert out.strip() == ""


def test_supply_broadcast_carded(tmp_path):
    out = _run(_mk("supply"), tmp_path)
    assert "广播待确认" in out
    assert "supply" in out
    card = json.loads(out)
    labels = [a["text"]["content"] for element in card["elements"]
              if element.get("tag") == "action" for a in element["actions"]]
    assert labels == ["发（确认广播）", "不发（取消）", "💬 聊聊这个"]


def test_demand_broadcast_carded(tmp_path):
    out = _run(_mk("demand"), tmp_path)
    assert "广播待确认" in out


def test_source_url_rendered_as_clickable_link(tmp_path):
    out = _run(_mk("supply", "https://www.eigenflux.ai"), tmp_path)
    assert "[https://www.eigenflux.ai](https://www.eigenflux.ai)" in out


def test_insight_broadcast_carded(tmp_path):
    out = _run(_mk("insight"), tmp_path)
    assert "广播待确认" in out
    assert "insight" in out


def test_should_publish_false_no_card(tmp_path):
    out = _run('{"should_publish":false}', tmp_path)
    assert out.strip() == ""


# ── Backlog gate (7/22): unanswered pending broadcast blocks new drafts ──

def _seed_pending(tmp_path, name: str, age_seconds: int = 0):
    pending = tmp_path / "eigenflux" / "pending_publish"
    pending.mkdir(parents=True, exist_ok=True)
    f = pending / name
    f.write_text('{"id":"x","content":"old","notes":{}}')
    if age_seconds:
        old = time.time() - age_seconds
        os.utime(f, (old, old))
    return f


def test_active_pending_blocks_new_draft(tmp_path):
    _seed_pending(tmp_path, "1000_1.json", age_seconds=3600)
    out = _run(_mk("insight"), tmp_path)
    assert out.strip() == ""
    # and no second pending file was written
    files = list((tmp_path / "eigenflux" / "pending_publish").glob("*.json"))
    assert len(files) == 1


def test_stale_pending_does_not_block(tmp_path):
    _seed_pending(tmp_path, "1000_1.json", age_seconds=49 * 3600)
    out = _run(_mk("insight"), tmp_path)
    assert "广播待确认" in out


PRE_SCRIPT = Path(__file__).resolve().parent.parent / "tasks" / "eigenflux_publish_pre.sh"


def _run_pre(tmp_path) -> subprocess.CompletedProcess:
    import shutil, os
    # a fake `eigenflux` binary so the pre-hook doesn't exit at the CLI check
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    fake = bindir / "eigenflux"
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(0o755)
    env = {"JARVIS_DIR": str(tmp_path),
           "PATH": f"{bindir}:/usr/bin:/bin",
           "HOME": str(tmp_path),
           "JARVIS_PYTHON": sys.executable}
    return subprocess.run(["bash", str(PRE_SCRIPT)],
                          capture_output=True, text=True, env=env)


def test_pre_hook_blocks_on_active_pending(tmp_path):
    _seed_pending(tmp_path, "1000_1.json", age_seconds=3600)
    r = _run_pre(tmp_path)
    assert r.returncode == 0
    assert r.stdout.strip() == ""  # empty pre → task skipped as healthy idle
    assert "awaiting user approval" in r.stderr


def test_pre_hook_expires_stale_pending_and_proceeds(tmp_path):
    f = _seed_pending(tmp_path, "1000_1.json", age_seconds=49 * 3600)
    r = _run_pre(tmp_path)
    assert r.returncode == 0
    assert not f.exists()
    expired = tmp_path / "eigenflux" / "pending_publish" / "expired" / "1000_1.json"
    assert expired.exists()
    assert "Ready to publish" in r.stdout


def test_expired_draft_lapses_its_approval_card(tmp_path):
    _run(_mk("insight"), tmp_path)
    draft = next((tmp_path / "eigenflux" / "pending_publish").glob("*.json"))
    payload = json.loads(draft.read_text())
    assert payload["memorial_id"]
    old = time.time() - 49 * 3600
    os.utime(draft, (old, old))

    result = _run_pre(tmp_path)

    assert result.returncode == 0
    from core.memorial import _fold
    states = _fold(read_jsonl(tmp_path / "memorials.jsonl"))
    card = states[payload["memorial_id"]]
    assert card["status"] == "lapsed"
    assert "48 小时" in card["lapse_reason"]
