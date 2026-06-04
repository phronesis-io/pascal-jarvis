"""Tests for tasks/eigenflux_feed_post.py — the '阅读原文' footer button.

A single-source card gets a tappable "阅读原文" button. A multi-item digest
(the FYI/知会 tier) carries one inline link per item, so a single footer button
would point to only the first item and mislead — it must be suppressed.
"""

import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "tasks" / "eigenflux_feed_post.py"


def _run(payload: str, tmp_path) -> str:
    env = {"JARVIS_DIR": str(tmp_path), "PATH": "/usr/bin:/bin"}
    r = subprocess.run([sys.executable, str(SCRIPT)], input=payload,
                       capture_output=True, text=True, env=env)
    return r.stdout


def test_single_item_card_keeps_read_original_button(tmp_path):
    payload = json.dumps({
        "user_message": "某条值得知会的消息 [link](https://example.com/a)",
    })
    out = _run(payload, tmp_path)
    assert "阅读原文" in out
    assert "https://example.com/a" in out


def test_multi_item_digest_suppresses_footer_button(tmp_path):
    msg = (
        "📡 知会\n"
        "- **A 事件**：详情 [link](https://example.com/a)\n"
        "- **B 事件**：详情 [link](https://example.com/b)\n"
        "- **C 事件**：详情 [link](https://example.com/c)"
    )
    out = _run(json.dumps({"user_message": msg}), tmp_path)
    # No misleading single footer button...
    assert "阅读原文" not in out
    # ...but every per-item inline link survives in the body for navigation.
    assert "https://example.com/a" in out
    assert "https://example.com/b" in out
    assert "https://example.com/c" in out


def test_bare_source_url_field_still_buttons_when_single(tmp_path):
    payload = json.dumps({
        "user_message": "纯分析，正文没有链接",
        "source_url": "https://example.com/only",
    })
    out = _run(payload, tmp_path)
    assert "阅读原文" in out
    assert "https://example.com/only" in out
