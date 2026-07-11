"""Tests for tasks/eigenflux_publish_post.py — supply/demand-only gate + link.

Pascal's directive: stop over-publishing. Only supply/demand broadcasts tied to
his real situation; "info" relays (papers/news) are banned. The confirmation
card must render the source as a tappable link.
"""

import subprocess
import sys
import json
from pathlib import Path

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


def test_should_publish_false_no_card(tmp_path):
    out = _run('{"should_publish":false}', tmp_path)
    assert out.strip() == ""
