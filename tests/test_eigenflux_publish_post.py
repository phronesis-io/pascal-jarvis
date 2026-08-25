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
    card = json.loads(out)
    labels = [a["text"]["content"] for element in card["elements"]
              if element.get("tag") == "action" for a in element["actions"]]
    assert labels == ["发（确认广播）", "不发（取消）", "💬 聊聊这个", "🤔 看不懂"]
    body = card["elements"][0]["text"]["content"]
    # 2026-08-24 card-style audit: no English metadata opener; the card opens
    # with a Chinese line, and the banner matches the 发/不发 buttons
    # (decision, not「知道就行」).
    assert "**类型**" not in body and "**领域**" not in body
    assert "想对外" in body
    assert "🎯 等你拍一个" in body
    assert "知道就行" not in body


def test_chinese_summary_leads_the_card(tmp_path):
    payload = (
        '{"should_publish":true,"content":"english broadcast body",'
        '"source_url":"",'
        '"notes":{"type":"insight","domains":["agents"],'
        '"summary":"给其他智能体的一个观察","expire_time":"2026-07-01T00:00:00Z",'
        '"source_type":"original"}}'
    )
    out = _run(payload, tmp_path)
    card = json.loads(out)
    body = card["elements"][0]["text"]["content"]
    assert "想对外广播这条：给其他智能体的一个观察" in body


def test_summary_cn_leads_the_card_and_never_ships_in_the_broadcast(tmp_path):
    """notes.summary_cn (the drafting prompt's owner-facing line) wins over
    the generic summary, and is POPPED from the persisted draft so the
    outbound broadcast payload stays unchanged."""
    payload = (
        '{"should_publish":true,"content":"english broadcast body",'
        '"source_url":"",'
        '"notes":{"type":"insight","domains":["agents"],'
        '"summary":"english summary","summary_cn":"一句给主人看的中文总结",'
        '"expire_time":"2026-07-01T00:00:00Z","source_type":"original"}}'
    )
    out = _run(payload, tmp_path)
    body = json.loads(out)["elements"][0]["text"]["content"]
    assert "想对外广播这条：一句给主人看的中文总结" in body
    draft = next((tmp_path / "eigenflux" / "pending_publish").glob("*.json"))
    assert "summary_cn" not in json.loads(draft.read_text())["notes"]


def test_publish_source_is_protected_from_engagement_demotion():
    """The 8/24 audit found a 知道就行 banner over 发/不发 buttons: the
    governor had demoted the source. Belt: explicit attention='decision';
    suspenders: the source is protected so evaluate() never measures it into
    a zombie override."""
    from core.attention_roi import PROTECTED_SOURCES
    assert "eigenflux-publish" in PROTECTED_SOURCES


def test_demand_broadcast_carded(tmp_path):
    out = _run(_mk("demand"), tmp_path)
    assert "广播待确认" in out


def test_source_url_rendered_as_clickable_link(tmp_path):
    out = _run(_mk("supply", "https://www.eigenflux.ai"), tmp_path)
    assert "[https://www.eigenflux.ai](https://www.eigenflux.ai)" in out


def test_insight_broadcast_carded(tmp_path):
    out = _run(_mk("insight"), tmp_path)
    assert "广播待确认" in out


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


def test_pre_hook_expires_stale_pending_but_skips_without_new_material(tmp_path):
    f = _seed_pending(tmp_path, "1000_1.json", age_seconds=49 * 3600)
    r = _run_pre(tmp_path)
    assert r.returncode == 0
    assert not f.exists()
    expired = tmp_path / "eigenflux" / "pending_publish" / "expired" / "1000_1.json"
    assert expired.exists()
    assert r.stdout.strip() == ""


def test_pre_hook_proceeds_when_recent_material_exists(tmp_path):
    memory = tmp_path / ".claude" / "projects" / "-Users-pascal-Desktop-jarvis" / "memory"
    memory.mkdir(parents=True)
    (memory / "new-insight.md").write_text("# A new grounded insight\n", encoding="utf-8")

    r = _run_pre(tmp_path)

    assert r.returncode == 0
    assert "Ready to publish" in r.stdout
    assert "A new grounded insight" in r.stdout


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
