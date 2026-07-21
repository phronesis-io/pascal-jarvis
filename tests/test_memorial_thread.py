"""REQ-118 奏折专属对话: thread root → memorial reverse lookup."""

import json

import pytest

from core import memorial, memorial_thread


@pytest.fixture()
def iso(tmp_path, monkeypatch):
    """Isolated ledger: memorial_thread follows memorial.JARVIS_DIR at call
    time (import-time capture wrote test events into the production ledger —
    red-team 7/21)."""
    monkeypatch.setenv("JARVIS_DIR", str(tmp_path))
    monkeypatch.setattr(memorial, "JARVIS_DIR", tmp_path)
    return tmp_path


def _create(iso, title="测试奏折", body="正文"):
    memorial._append_line(iso / "memorials.jsonl", {
        "id": "mem_t_1", "ev": "create", "ts": "2026-07-21 14:00",
        "epoch": 1, "source": "test", "title": title, "body": body,
    })
    return "mem_t_1"


def test_record_and_find(iso):
    mid = _create(iso)
    memorial_thread.record_sent(mid, "om_abc123")
    assert memorial_thread.find_by_lark_mid("om_abc123") == mid
    assert memorial_thread.find_by_lark_mid("om_nope") == ""
    assert memorial_thread.find_by_lark_mid("") == ""


def test_record_sent_noop_on_empty(iso):
    memorial_thread.record_sent("", "om_x")
    memorial_thread.record_sent("mem_x", "")
    assert not (iso / "memorials.jsonl").exists()


def test_route_prefers_root_falls_back_to_parent(iso):
    mid = _create(iso, title="标题A")
    memorial_thread.record_sent(mid, "om_root")
    # root hit
    got, title = memorial_thread.route("om_root", "om_other")
    assert (got, title) == (mid, "标题A")
    # root miss, parent hit (first-level reply where Lark omits root_id)
    got, title = memorial_thread.route("", "om_root")
    assert got == mid
    # both miss
    assert memorial_thread.route("om_x", "om_y") == ("", "")


def test_sent_event_ignored_by_memorial_fold(iso):
    """Forward-safety: memorial._fold must not break on ev=='sent'."""
    mid = _create(iso)
    memorial_thread.record_sent(mid, "om_abc")
    st = memorial.get_memorial(mid)
    assert st is not None and st["status"] == "pending"


def test_context_block_pins_card(iso):
    mid = _create(iso, title="运动周报", body="本周 3 次")
    block = memorial_thread.context_block(mid)
    assert "运动周报" in block and "本周 3 次" in block
    assert "专属对话" in block
    assert memorial_thread.context_block("mem_missing") == ""


def test_context_block_shows_decision(iso):
    mid = _create(iso)
    memorial._append_line(iso / "memorials.jsonl", {
        "id": mid, "ev": "decide", "opt": "ok", "label": "知道了",
        "ts": "2026-07-21 15:00",
    })
    assert "知道了" in memorial_thread.context_block(mid)
