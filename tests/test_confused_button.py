"""「看不懂」— the confusion signal and the plain-retelling loop.

Owner (2026-08-03): 「我建议再加一个按钮，就是看不懂。有些东西我是真的看不懂
他在说什么，很多东西我都看不懂。」 A card he cannot parse is a style failure;
the tap must (1) be recorded, (2) produce a plain-language retelling without
him typing anything, and (3) teach future card-writing as a negative example.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import memorial  # noqa: E402


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(memorial, "JARVIS_DIR", tmp_path)
    monkeypatch.setattr(memorial, "_desk_reachable", lambda: True)
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    yield


def _card(**kw):
    mid, _ = memorial.create(
        source=kw.pop("source", "metrics-digest"),
        title=kw.pop("title", "难懂的标题"),
        body=kw.pop("body", "一段没人看得懂的话"),
        preset="fyi", send=False, **kw)
    return mid


def test_every_card_offers_the_confused_button():
    mid = _card()
    card = json.loads(memorial.card_json(mid))
    labels = [a["text"]["content"] for el in card["elements"]
              for a in el.get("actions", [])]
    assert "🤔 看不懂" in labels
    values = [a.get("value") for el in card["elements"]
              for a in el.get("actions", [])]
    assert {"action": "memorial", "id": mid, "opt": "confused"} in values


def test_confused_records_queues_and_keeps_the_card_pending():
    mid = _card()
    out = memorial.confused(mid)

    assert out["toast"]["type"] == "success"
    st = memorial.get_memorial(mid)
    # Confusion is NOT an answer — the card must stay pending.
    assert st["status"] == "pending"
    assert st["confused_ts"], "the tap left no trace on the ledger"

    rows = [json.loads(line) for line in
            memorial._explain_queue_path().read_text().splitlines()]
    assert rows and rows[0]["memorial_id"] == mid

    body = json.dumps(out["card"]["data"], ensure_ascii=False)
    assert "大白话版本马上单独发给你" in body


def test_explain_claim_is_crash_safe_and_completes():
    mid = _card()
    memorial.confused(mid)

    req = memorial.explain_claim()
    assert req and req["memorial_id"] == mid
    # Claimed, not deleted: a dead model call must not eat the request.
    assert memorial.explain_claim() is None, "double-claim within the window"
    # After the retake window the same request is offered again.
    late = int(time.time()) + memorial.EXPLAIN_RETAKE_S + 1
    assert memorial.explain_claim(now_epoch=late)["memorial_id"] == mid

    memorial.explain_complete(mid)
    assert memorial.explain_claim(now_epoch=late + 1) is None


def test_confused_option_key_is_reserved():
    with pytest.raises(ValueError):
        memorial.create(source="x", title="t", body="b", send=False,
                        options=[{"key": "confused", "label": "看不懂"}])


def test_recent_confused_surfaces_negative_examples():
    a = _card(title="看不懂的A")
    _card(title="没问题的B")
    memorial.confused(a)
    got = memorial.recent_confused()
    assert [c["id"] for c in got] == [a]
    assert got[0]["title"] == "看不懂的A"


def test_explain_post_delivers_and_settles(tmp_path):
    mid = _card()
    memorial.confused(mid)
    memorial.explain_claim()

    env = {**dict(__import__("os").environ),
           "JARVIS_DIR": str(tmp_path), "PYTHONPATH": str(ROOT)}
    proc = subprocess.run(
        [sys.executable, str(ROOT / "tasks" / "explain_card_post.py")],
        input=f"[explain {mid}] 这是件小事：X 涨了一点，不用你做什么。",
        capture_output=True, text=True, env=env)
    assert proc.returncode == 0
    assert "大白话重讲" in proc.stdout
    assert f"[explain {mid}]" not in proc.stdout, "id marker leaked to the user"
    assert "不用你做什么" in proc.stdout
    # Settled: the queue is empty even past the retake window.
    late = int(time.time()) + memorial.EXPLAIN_RETAKE_S + 5
    assert memorial.explain_claim(now_epoch=late) is None


def test_explain_post_empty_model_output_leaves_the_claim(tmp_path):
    """A model that came back empty must not eat the tap — the claim stays
    and is retaken after the window (an unanswered 看不懂 is a dead end)."""
    mid = _card()
    memorial.confused(mid)
    memorial.explain_claim()

    env = {**dict(__import__("os").environ),
           "JARVIS_DIR": str(tmp_path), "PYTHONPATH": str(ROOT)}
    proc = subprocess.run(
        [sys.executable, str(ROOT / "tasks" / "explain_card_post.py")],
        input="HEARTBEAT_OK", capture_output=True, text=True, env=env)
    assert proc.returncode == 0 and proc.stdout.strip() == ""
    late = int(time.time()) + memorial.EXPLAIN_RETAKE_S + 5
    assert memorial.explain_claim(now_epoch=late)["memorial_id"] == mid
