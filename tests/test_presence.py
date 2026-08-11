"""Presence floor + morning archive digest.

Owner (2026-08-07): 「飞书里面没有卡片了，jarvis 就没有存在感」. The 7/24
cliff ran ten days green because cards were delivered to surfaces with zero
traffic. These tests pin the two defenses: the floor sentinel pages, and
archive-only cards get one batched shot in the morning anchor.
"""

from __future__ import annotations

import io
import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import core.presence as presence  # noqa: E402

NOW = datetime(2026, 8, 7, 12, 0)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(presence, "JARVIS_DIR", tmp_path)
    yield


def _write_ledger(tmp_path, events):
    lines = "\n".join(json.dumps(e, ensure_ascii=False) for e in events)
    (tmp_path / "memorials.jsonl").write_text(lines + "\n", encoding="utf-8")


def _created(i, ts="2026-08-07 09:00", title="标题"):
    return {"ev": "create", "id": f"mem_{i}", "ts": ts, "title": f"{title}{i}"}


def _sent(i, ts="2026-08-07 09:01"):
    return {"ev": "sent", "id": f"mem_{i}", "ts": ts,
            "lark_message_id": f"om_{i}"}


def test_floor_pages_when_feishu_goes_quiet(tmp_path):
    """The cliff signature: cards created all day, almost none reach Feishu."""
    events = [_created(i) for i in range(20)] + [_sent(0), _sent(1)]
    _write_ledger(tmp_path, events)
    assert presence.check(now=NOW) == presence.FLOOR_WARNING


def test_floor_quiet_on_a_healthy_day(tmp_path):
    events = []
    for i in range(10):
        events += [_created(i), _sent(i)]
    _write_ledger(tmp_path, events)
    assert presence.check(now=NOW) == ""


def test_floor_ignores_stale_sends_outside_the_window(tmp_path):
    """Ten sends last week must not mask a silent today."""
    events = [_sent(i, ts="2026-07-30 09:00") for i in range(10)]
    _write_ledger(tmp_path, events)
    assert presence.check(now=NOW) == presence.FLOOR_WARNING


def test_fresh_install_without_ledger_is_not_an_outage(tmp_path):
    assert presence.check(now=NOW) == ""


def test_warning_text_is_stable_for_alert_dedup():
    """selfmon dedups by line content — a count in the text would re-page
    every 4h for one persisting condition."""
    assert not any(ch.isdigit() and ch != "5" and ch != "2" and ch != "4"
                   for ch in presence.FLOOR_WARNING.replace("7/24", ""))


def test_digest_batches_archive_only_cards(tmp_path):
    events = [_created(i) for i in range(7)] + [_sent(0)]
    _write_ledger(tmp_path, events)
    line = presence.morning_digest_line(now=NOW)
    assert "6 条" in line
    assert "标题6" in line  # newest titles shown
    assert "标题0" not in line  # that one reached Feishu


def test_digest_stays_quiet_below_the_contract_threshold(tmp_path):
    """攒批≥5条才提一行 — 1-4 条不值得占用晨间锚点。"""
    events = [_created(i) for i in range(4)]
    _write_ledger(tmp_path, events)
    assert presence.morning_digest_line(now=NOW) == ""


def test_ledger_only_cards_feed_the_morning_digest(tmp_path, monkeypatch):
    """REQ-119 end-to-end: an ambient card that create() keeps ledger-only
    (no envelope, no Lark send) is exactly what the 攒批 digest line counts."""
    import core.memorial as memorial

    monkeypatch.setattr(memorial, "JARVIS_DIR", tmp_path)
    for i in range(5):
        mid, accepted = memorial.create(
            source="repos-sync", title=f"仓库动态{i}", body=f"第 {i} 条")
        assert accepted is True
        assert memorial.get_memorial(mid)["delivery_status"] == "ledger_only"

    line = presence.morning_digest_line(now=datetime.now())
    assert "5 条" in line
    assert "仓库动态4" in line


def test_morning_anchor_appends_the_digest_below_the_one_liner(
        tmp_path, monkeypatch, capsys):
    import core.lifelog as lifelog
    import tasks.morning_anchor_post as post

    monkeypatch.setattr(lifelog, "morning_anchor_fired", lambda: False)
    monkeypatch.setattr(lifelog, "morning_anchor_mark", lambda *a, **k: None)
    monkeypatch.setattr(post, "morning_anchor_fired", lambda: False)
    monkeypatch.setattr(post, "morning_anchor_mark", lambda *a, **k: None)
    monkeypatch.setattr(post, "morning_anchor_last_text", lambda: "")
    events = [_created(i, ts=datetime.now().strftime("%Y-%m-%d %H:%M"))
              for i in range(6)]
    _write_ledger(tmp_path, events)
    monkeypatch.setattr(sys, "stdin", io.StringIO("早。今天的锚点：死活题。"))
    assert post.main() == 0
    out = capsys.readouterr().out
    assert "死活题" in out
    assert "6 条" in out and "归档" in out


def test_morning_anchor_survives_a_broken_digest(monkeypatch, capsys):
    import tasks.morning_anchor_post as post

    monkeypatch.setattr(post, "morning_anchor_fired", lambda: False)
    monkeypatch.setattr(post, "morning_anchor_mark", lambda *a, **k: None)
    monkeypatch.setattr(post, "morning_anchor_last_text", lambda: "")

    def boom(**kw):
        raise RuntimeError("ledger unreadable")
    monkeypatch.setattr(presence, "morning_digest_line", boom)
    monkeypatch.setattr(sys, "stdin", io.StringIO("早。锚点一行。"))
    assert post.main() == 0
    assert "锚点一行" in capsys.readouterr().out


def test_morning_anchor_skips_same_line_as_yesterday(monkeypatch, capsys):
    """REQ-121: an anchor line substantively identical to yesterday's (and no
    digest riding along) is not resent — the window is consumed instead."""
    import tasks.morning_anchor_post as post

    marks = []
    monkeypatch.setattr(post, "morning_anchor_fired", lambda: False)
    monkeypatch.setattr(post, "morning_anchor_mark",
                        lambda *a, **k: marks.append(k))
    monkeypatch.setattr(post, "morning_anchor_last_text",
                        lambda: "早。今天的锚点：死活题。")
    monkeypatch.setattr(presence, "morning_digest_line", lambda **kw: "")
    monkeypatch.setattr(sys, "stdin",
                        io.StringIO("  早。今天的锚点：死活题。  "))
    assert post.main() == 0
    assert capsys.readouterr().out == ""  # no card
    assert marks, "the day must still be stamped as handled"


def test_morning_anchor_same_line_still_sends_when_digest_rides(
        tmp_path, monkeypatch, capsys):
    """The digest footer has no other surface — fresh counts override the
    same-as-yesterday skip."""
    import tasks.morning_anchor_post as post

    monkeypatch.setattr(post, "morning_anchor_fired", lambda: False)
    monkeypatch.setattr(post, "morning_anchor_mark", lambda *a, **k: None)
    monkeypatch.setattr(post, "morning_anchor_last_text",
                        lambda: "早。今天的锚点：死活题。")
    events = [_created(i, ts=datetime.now().strftime("%Y-%m-%d %H:%M"))
              for i in range(6)]
    _write_ledger(tmp_path, events)
    monkeypatch.setattr(sys, "stdin", io.StringIO("早。今天的锚点：死活题。"))
    assert post.main() == 0
    out = capsys.readouterr().out
    assert "6 条" in out and "归档" in out


def test_morning_anchor_fresh_line_is_sent_and_remembered(monkeypatch, capsys):
    import tasks.morning_anchor_post as post

    remembered = []
    monkeypatch.setattr(post, "morning_anchor_fired", lambda: False)
    monkeypatch.setattr(
        post, "morning_anchor_mark",
        lambda *a, **k: remembered.append(k.get("text", "")))
    monkeypatch.setattr(post, "morning_anchor_last_text",
                        lambda: "早。昨天的锚点：拉伸。")
    monkeypatch.setattr(presence, "morning_digest_line", lambda **kw: "")
    monkeypatch.setattr(sys, "stdin", io.StringIO("早。今天的锚点：死活题。"))
    assert post.main() == 0
    assert "死活题" in capsys.readouterr().out
    assert remembered == ["早。今天的锚点：死活题。"]
