"""Tests for EigenFlux feed cards: one event per card + source links.

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
    # Force "awake" so these card-rendering assertions are time-independent;
    # the quiet-hours hold/digest behavior is covered in test_ef_delivery.py.
    env = {"JARVIS_DIR": str(tmp_path), "PATH": "/usr/bin:/bin",
           "JARVIS_EF_QUIET_OVERRIDE": "awake"}
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


def test_structured_items_emit_one_card_each(tmp_path):
    payload = json.dumps({
        "user_messages": [
            {"item_id": "1", "title": "知会", "body": "第一件事",
             "source_url": "https://example.com/1"},
            {"item_id": "2", "title": "行动", "body": "第二件事",
             "source_url": "https://example.com/2"},
        ]
    })
    out = _run(payload, tmp_path)
    cards = [json.loads(line) for line in out.splitlines() if line.strip()]
    assert len(cards) == 2
    assert "第一件事" in cards[0]["elements"][0]["text"]["content"]
    assert "第二件事" in cards[1]["elements"][0]["text"]["content"]


def test_bare_source_url_field_still_buttons_when_single(tmp_path):
    payload = json.dumps({
        "user_message": "纯分析，正文没有链接",
        "source_url": "https://example.com/only",
    })
    out = _run(payload, tmp_path)
    assert "阅读原文" in out
    assert "https://example.com/only" in out


def _run_with_fake_cli(payload: str, tmp_path):
    """Run the post script with a fake `eigenflux` on PATH that captures argv.

    Returns the parsed `--items` payload the script tried to submit.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    capture = tmp_path / "items.json"
    fake = bindir / "eigenflux"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, json\n"
        "a = sys.argv[1:]\n"
        "if '--items' in a:\n"
        "    open(r'%s','w').write(a[a.index('--items')+1])\n"
        "print(json.dumps({'processed_count': 1, 'skipped_count': 0}))\n"
        % capture
    )
    fake.chmod(0o755)
    env = {"JARVIS_DIR": str(tmp_path), "PATH": f"{bindir}:/usr/bin:/bin"}
    subprocess.run([sys.executable, str(SCRIPT)], input=payload,
                   capture_output=True, text=True, env=env)
    return json.loads(capture.read_text()) if capture.exists() else None


def test_feedback_item_id_sent_as_string(tmp_path):
    """Regression: the API rejects a numeric item_id with HTTP 400. The script
    cast item_id to int, so every feedback submission was silently black-holed.
    item_id must reach the CLI as a JSON string."""
    payload = json.dumps({
        "feedback": [{"item_id": "320503928905007104", "score": 1, "action": "silent"}],
        "user_message": "",
    })
    items = _run_with_fake_cli(payload, tmp_path)
    assert items is not None, "script never called `eigenflux feed feedback`"
    assert isinstance(items[0]["item_id"], str), "item_id must be a string, not a number"
    assert items[0]["item_id"] == "320503928905007104"
    assert items[0]["score"] == 1


def test_feedback_numeric_item_id_coerced_to_string(tmp_path):
    """The real failure mode: the LLM emits item_id as a JSON NUMBER. It must
    still reach the CLI as a string — a numeric item_id 400s with 'Mismatch type
    string with value number' and black-holes the whole submission."""
    payload = json.dumps({
        "feedback": [{"item_id": 320503928905007104, "score": 1, "action": "silent"}],
        "user_message": "",
    })
    items = _run_with_fake_cli(payload, tmp_path)
    assert items is not None, "script never called `eigenflux feed feedback`"
    assert isinstance(items[0]["item_id"], str), "numeric item_id must be cast to string"
    assert items[0]["item_id"] == "320503928905007104"


def test_feedback_out_of_range_score_clamped(tmp_path):
    """The API silently SKIPS scores outside -1..2 (processed_count stays 0), so
    an out-of-range LLM score must be clamped client-side, not dropped."""
    payload = json.dumps({
        "feedback": [
            {"item_id": "1", "score": 5, "action": "push"},     # → clamp to 2
            {"item_id": "2", "score": -9, "action": "silent"},  # → clamp to -1
        ],
        "user_message": "",
    })
    items = _run_with_fake_cli(payload, tmp_path)
    assert items is not None
    by_id = {it["item_id"]: it["score"] for it in items}
    assert by_id["1"] == 2
    assert by_id["2"] == -1
