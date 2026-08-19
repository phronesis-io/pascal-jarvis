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


def _ledger_only(i, ts="2026-08-07 09:01"):
    return {"ev": "delivery", "id": f"mem_{i}", "ts": ts,
            "status": "ledger_only"}


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


def test_sent_count_uses_delivery_receipts_and_excludes_non_cards(
        tmp_path, monkeypatch):
    from core.delivery import DeliveryEnvelope, DeliveryPipeline, TransportResult

    now = NOW.replace(tzinfo=__import__(
        "core.timeutil", fromlist=["now_local"]).now_local().tzinfo).timestamp()
    db_path = tmp_path / "data" / "jarvis.db"
    monkeypatch.setenv("JARVIS_DB_PATH", str(db_path))
    pipe = DeliveryPipeline(
        tmp_path, db_path=db_path,
        transport=lambda _envelope, _channel: TransportResult(True, "om_ok"),
        clock=lambda: now, sleeper=lambda _: None,
    )
    budget = {
        "burst_cap": 20,
        "global_daily_cap": 20,
        "source_daily_cap": 20,
    }
    for index in range(7):
        card_json = json.dumps({
            "config": {},
            "elements": [{"tag": "markdown",
                          "content": f"第 {index} 张卡"}],
        })
        assert pipe.deliver(DeliveryEnvelope(
            source=f"card-{index}", kind="card",
            payload={"card_json": card_json},
            metadata=budget,
        )).state == "delivered"
    assert pipe.deliver(DeliveryEnvelope(
        source="urgent-heartbeat", kind="card", attention="alert",
        payload={"card_json": json.dumps({
            "elements": [{"tag": "markdown", "content": "紧急提醒"}],
        })},
        metadata={**budget, "bypass_throttle": True},
    )).state == "delivered"
    pipe.deliver(DeliveryEnvelope(
        source="plain-text", payload={"text": "不是卡"}, metadata=budget))
    pipe.deliver(DeliveryEnvelope(
        source="deploy-smoke", kind="card",
        payload={"card_json": json.dumps({
            "elements": [{"tag": "markdown", "content": "smoke"}],
        })},
        metadata={**budget, "bypass_throttle": True}))

    # A stale or incomplete memorial side ledger cannot make presence lie.
    _write_ledger(tmp_path, [_sent(0), _sent(1)])
    assert presence.sent_count(now=NOW) == 8


def test_floor_uses_delivery_db_without_memorial_ledger(tmp_path, monkeypatch):
    """Direct heartbeat cards can exist before any memorial is written."""
    from core.delivery import DeliveryEnvelope, DeliveryPipeline, TransportResult

    now = NOW.replace(tzinfo=__import__(
        "core.timeutil", fromlist=["now_local"]).now_local().tzinfo).timestamp()
    db_path = tmp_path / "data" / "jarvis.db"
    monkeypatch.setenv("JARVIS_DB_PATH", str(db_path))
    pipe = DeliveryPipeline(
        tmp_path, db_path=db_path,
        transport=lambda _envelope, _channel: TransportResult(True, "om_ok"),
        clock=lambda: now, sleeper=lambda _: None,
    )
    for index in range(2):
        assert pipe.deliver(DeliveryEnvelope(
            source="heartbeat", kind="card",
            payload={"card_json": json.dumps({
                "elements": [{"tag": "markdown", "content": f"卡片 {index}"}],
            })},
            metadata={
                "burst_cap": 20,
                "global_daily_cap": 20,
                "source_daily_cap": 20,
            },
        )).state == "delivered"

    assert not (tmp_path / "memorials.jsonl").exists()
    assert presence.check(now=NOW) == presence.FLOOR_WARNING


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


def test_digest_batches_ledger_only_cards(tmp_path):
    events = ([_created(i) for i in range(7)] + [_sent(0)]
              + [_ledger_only(i) for i in range(1, 7)])
    _write_ledger(tmp_path, events)
    line = presence.morning_digest_line(now=NOW)
    assert "6 条" in line
    assert "标题6" in line  # newest titles shown
    assert "标题0" not in line  # that one reached Feishu


def test_digest_ignores_queued_lark_cards(tmp_path):
    """Adversarial review (2026-08-11): a Lark card waiting out quiet hours
    has a create event and no sent event YET — counting it would double-expose
    it (digest line at 8:30, real card when the queue flushes). Only rows
    explicitly recorded ledger_only belong to the digest."""
    events = [_created(i) for i in range(6)]  # queued: no delivery event yet
    _write_ledger(tmp_path, events)
    assert presence.morning_digest_line(now=NOW) == ""


def test_digest_stays_quiet_below_the_contract_threshold(tmp_path):
    """攒批≥5条才提一行 — 1-4 条不值得占用晨间锚点。"""
    events = ([_created(i) for i in range(4)]
              + [_ledger_only(i) for i in range(4)])
    _write_ledger(tmp_path, events)
    assert presence.morning_digest_line(now=NOW) == ""


def test_ledger_only_cards_feed_the_morning_digest(tmp_path, monkeypatch):
    """REQ-119 end-to-end: an ambient card that create() keeps ledger-only
    (no envelope, no Lark send) is exactly what the 攒批 digest line counts."""
    import core.memorial as memorial

    monkeypatch.setattr(memorial, "JARVIS_DIR", tmp_path)
    for i in range(5):
        mid, accepted = memorial.create(
            source="cross-session-sync", title=f"会话动态{i}", body=f"第 {i} 条")
        assert accepted is True
        assert memorial.get_memorial(mid)["delivery_status"] == "ledger_only"

    line = presence.morning_digest_line(now=datetime.now())
    assert "5 条" in line
    assert "会话动态4" in line


def test_morning_anchor_appends_the_digest_below_the_one_liner(
        tmp_path, monkeypatch, capsys):
    import core.lifelog as lifelog
    import tasks.morning_anchor_post as post

    monkeypatch.setattr(lifelog, "morning_anchor_fired", lambda: False)
    monkeypatch.setattr(lifelog, "morning_anchor_mark", lambda *a, **k: None)
    monkeypatch.setattr(post, "morning_anchor_fired", lambda: False)
    monkeypatch.setattr(post, "morning_anchor_mark", lambda *a, **k: None)
    monkeypatch.setattr(post, "morning_anchor_last_text", lambda: "")
    now_ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    events = ([_created(i, ts=now_ts) for i in range(6)]
              + [_ledger_only(i, ts=now_ts) for i in range(6)])
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
    now_ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    events = ([_created(i, ts=now_ts) for i in range(6)]
              + [_ledger_only(i, ts=now_ts) for i in range(6)])
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


# ── absence: a shut lid reads exactly like a broken pipe ─────────────
#
# 2026-08-18/19 the MacBook was closed for ~39h. Card output fell 76/day → 2,
# so the floor sentinel fired — pointing at the delivery chain, which was
# healthy. Meanwhile nothing anywhere told Pascal he had been offline for a
# day and a half; he found out because the cards thinned out.


def _sleep_gaps(tmp_path, *durations_s, ts="2026-08-07 03:00"):
    lines = "\n".join(json.dumps({
        "ts": ts, "event": "sleep_gap", "task": "", "run_id": "",
        "source": "heartbeat_loop", "duration_s": d,
    }, ensure_ascii=False) for d in durations_s)
    (tmp_path / "sched_events.jsonl").write_text(lines + "\n", encoding="utf-8")


def test_quiet_because_the_host_slept_does_not_blame_delivery(tmp_path):
    events = [_created(i) for i in range(20)] + [_sent(0), _sent(1)]
    _write_ledger(tmp_path, events)
    _sleep_gaps(tmp_path, 4181, 5481, 4286, 4568, 3674)   # 8/18, ~6h of it

    line = presence.check(now=NOW)

    assert line == presence.ABSENCE_WARNING
    assert line != presence.FLOOR_WARNING
    assert "投递" in line          # names the thing it is ruling OUT


def test_quiet_on_an_awake_host_still_blames_delivery(tmp_path):
    """The 7/24 cliff must keep firing — that outage had no host sleep."""
    events = [_created(i) for i in range(20)] + [_sent(0), _sent(1)]
    _write_ledger(tmp_path, events)
    _sleep_gaps(tmp_path, 300)     # one nap, nowhere near the threshold

    assert presence.check(now=NOW) == presence.FLOOR_WARNING


def test_a_healthy_day_is_silent_even_after_a_nap(tmp_path):
    """Sleep is not itself an alert — only an explanation for a quiet one."""
    events = []
    for i in range(20):
        events += [_created(i), _sent(i)]
    _write_ledger(tmp_path, events)
    _sleep_gaps(tmp_path, 4181, 5481, 4286)

    assert presence.check(now=NOW) == ""


def test_absence_line_reports_the_hours_and_asks_for_nothing(tmp_path):
    _sleep_gaps(tmp_path, 4181, 5481, 4286, 4568, 3674, 3595)  # 7.2h

    line = presence.absence_line(now=NOW)

    assert "7 小时" in line
    assert "知道就行" in line       # style contract: no action ⇒ say so


def test_absence_line_stays_quiet_for_an_ordinary_nap(tmp_path):
    _sleep_gaps(tmp_path, 900, 1200)      # 35min — not a receipt-worthy gap

    assert presence.absence_line(now=NOW) == ""


def test_absence_line_survives_a_missing_event_log(tmp_path):
    assert presence.host_asleep_seconds(now=NOW) == 0.0
    assert presence.absence_line(now=NOW) == ""
