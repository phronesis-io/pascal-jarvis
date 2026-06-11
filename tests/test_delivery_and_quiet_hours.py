"""Regression tests for the P1 delivery-assurance + quiet-hours batch.

Covers docs/prd_interaction_quality.md REQ-11 (send retry, delivery ledger,
aggregate alert) and REQ-13 (night queue + morning digest flush).
"""

import json
import time

import core.heartbeat_loop as hbl
from core.heartbeat_loop import (
    DELIVERY_ALERT_COOLDOWN,
    DELIVERY_ALERT_THRESHOLD,
    DELIVERY_STATE_FILE,
    NIGHT_QUEUE_FILE,
    _flush_night_queue,
    _in_quiet_hours,
    _is_urgent,
    _note_delivery,
    _queue_for_morning,
)


# ── REQ-13: quiet hours window ───────────────────────────────────────


def test_quiet_hours_boundaries():
    assert _in_quiet_hours(23 * 60 + 30)      # 23:30 — starts
    assert _in_quiet_hours(0)                 # midnight
    assert _in_quiet_hours(9 * 60 + 59)       # 09:59 — still quiet (9h replies ~6%)
    assert not _in_quiet_hours(10 * 60)       # 10:00 — opens (golden window)
    assert not _in_quiet_hours(13 * 60)       # 13:00 — golden window
    assert not _in_quiet_hours(23 * 60 + 29)  # 23:29 — still open


def test_urgent_source_parsing():
    assert _is_urgent("intention-check")
    assert _is_urgent("eigenflux-feed-triage, calendar-sync")
    assert not _is_urgent("eigenflux-feed-triage,content-recommend")
    assert not _is_urgent("")


def test_night_queue_roundtrip(tmp_path, monkeypatch):
    (tmp_path / ".heartbeat_last_source").write_text("content-recommend")
    _queue_for_morning("深夜推荐内容 A", tmp_path)
    # sidecar consumed so the queued message isn't double-counted as sent
    assert not (tmp_path / ".heartbeat_last_source").exists()
    _queue_for_morning("深夜推荐内容 B", tmp_path)

    sent = []
    monkeypatch.setattr(hbl, "_lark_send_text", lambda text, uid: sent.append(text) or True)
    assert _flush_night_queue(tmp_path, "ou_test")

    assert len(sent) == 1  # ONE digest, not N messages
    assert "深夜推荐内容 A" in sent[0] and "深夜推荐内容 B" in sent[0]
    assert "2" in sent[0]  # count in header
    assert not (tmp_path / NIGHT_QUEUE_FILE).exists()  # cleared after flush


def test_night_queue_kept_when_send_fails(tmp_path, monkeypatch):
    (tmp_path / ".heartbeat_last_source").write_text("heartbeat")
    _queue_for_morning("消息", tmp_path)
    monkeypatch.setattr(hbl, "_lark_send_text", lambda text, uid: False)
    assert not _flush_night_queue(tmp_path, "ou_test")
    assert (tmp_path / NIGHT_QUEUE_FILE).exists()  # retained for retry


def test_flush_empty_queue_is_noop(tmp_path):
    assert not _flush_night_queue(tmp_path, "ou_test")


# ── REQ-11: delivery ledger + aggregate alert ────────────────────────


def _fails(tmp_path):
    try:
        return json.loads((tmp_path / DELIVERY_STATE_FILE).read_text())
    except FileNotFoundError:
        return {}


def test_alert_fires_once_past_threshold(tmp_path, monkeypatch):
    alerts = []
    monkeypatch.setattr(hbl, "_lark_send_text", lambda text, uid: alerts.append(text) or True)

    t0 = time.time()
    for i in range(DELIVERY_ALERT_THRESHOLD + 2):
        _note_delivery(tmp_path, ok=False, user_id="ou_test", now=t0 + i)

    # Threshold crossed once → exactly one alert (cooldown blocks repeats)
    assert len(alerts) == 1
    assert "送达" in alerts[0]
    assert _fails(tmp_path)["consec_fails"] == DELIVERY_ALERT_THRESHOLD + 2


def test_alert_repeats_after_cooldown(tmp_path, monkeypatch):
    alerts = []
    monkeypatch.setattr(hbl, "_lark_send_text", lambda text, uid: alerts.append(text) or True)

    t0 = time.time()
    for i in range(DELIVERY_ALERT_THRESHOLD):
        _note_delivery(tmp_path, ok=False, user_id="u", now=t0 + i)
    _note_delivery(tmp_path, ok=False, user_id="u", now=t0 + DELIVERY_ALERT_COOLDOWN + 60)
    assert len(alerts) == 2


def test_success_resets_counter(tmp_path, monkeypatch):
    monkeypatch.setattr(hbl, "_lark_send_text", lambda text, uid: True)
    _note_delivery(tmp_path, ok=False, user_id="u")
    _note_delivery(tmp_path, ok=False, user_id="u")
    _note_delivery(tmp_path, ok=True, user_id="u")
    assert _fails(tmp_path)["consec_fails"] == 0


def test_send_text_retries_then_succeeds(monkeypatch):
    attempts = []

    class _R:
        def __init__(self, rc):
            self.returncode = rc

    def fake_run(cmd, **kw):
        attempts.append(cmd)
        return _R(1 if len(attempts) < 3 else 0)

    monkeypatch.setattr(hbl.subprocess, "run", fake_run)
    monkeypatch.setattr(hbl.time, "sleep", lambda s: None)

    assert hbl._lark_send_text("hello", "ou_test")
    assert len(attempts) == 3  # failed twice, succeeded on final retry


def test_send_text_gives_up_after_retries(monkeypatch):
    attempts = []

    class _R:
        returncode = 1

    monkeypatch.setattr(hbl.subprocess, "run", lambda cmd, **kw: attempts.append(cmd) or _R())
    monkeypatch.setattr(hbl.time, "sleep", lambda s: None)

    assert not hbl._lark_send_text("hello", "ou_test")
    assert len(attempts) == 1 + len(hbl.SEND_RETRY_DELAYS)


def test_flush_dedups_and_records_engagement(tmp_path, monkeypatch):
    for text in ["重复内容", "重复内容", "另一条"]:
        (tmp_path / ".heartbeat_last_source").write_text("content-recommend")
        _queue_for_morning(text, tmp_path)
    sent = []
    monkeypatch.setattr(hbl, "_lark_send_text", lambda t, u: sent.append(t) or True)
    assert _flush_night_queue(tmp_path, "ou_test")

    assert sent[0].count("重复内容") == 1  # duplicate collapsed
    assert "2 条消息" in sent[0]
    # queued sources are visible to engagement-analyze after the flush
    elog = (tmp_path / "engagement_log.jsonl").read_text()
    entries = [json.loads(l) for l in elog.splitlines()]
    assert any(e["type"] == "sent" and e["source"] == "content-recommend"
               and e.get("via") == "night-digest" for e in entries)


# ── Daytime batching + breakpoint release (R3 research round) ────────


def test_should_queue_general_interest_in_daytime(tmp_path):
    (tmp_path / ".heartbeat_last_source").write_text("eigenflux-feed-triage")
    assert hbl._should_queue(tmp_path, minutes_of_day=14 * 60)  # 14:00 daytime


def test_should_not_queue_task_relevant_in_daytime(tmp_path):
    (tmp_path / ".heartbeat_last_source").write_text("phronesis-monitor")
    assert not hbl._should_queue(tmp_path, minutes_of_day=14 * 60)
    # mixed batch containing task-relevant content sends immediately
    (tmp_path / ".heartbeat_last_source").write_text("eigenflux-feed-triage, phronesis-monitor")
    assert not hbl._should_queue(tmp_path, minutes_of_day=14 * 60)


def test_should_queue_everything_nonurgent_at_night(tmp_path):
    (tmp_path / ".heartbeat_last_source").write_text("phronesis-monitor")
    assert hbl._should_queue(tmp_path, minutes_of_day=2 * 60)  # 02:00
    (tmp_path / ".heartbeat_last_source").write_text("intention-check")
    assert not hbl._should_queue(tmp_path, minutes_of_day=2 * 60)  # urgent bypasses


def test_should_flush_at_window_and_not_before(tmp_path, monkeypatch):
    monkeypatch.setattr(hbl, "_user_recently_active", lambda now=None: False)
    (tmp_path / hbl.NIGHT_QUEUE_FILE).write_text('{"text":"x"}\n')
    now = time.time()
    # last flush long ago; 13:29 is before the 13:30 window (and past 10:00,
    # but a 10:00 flush already happened — stamp it 30min ago)
    hbl._stamp_flush(tmp_path, now=now - 1800)
    assert not hbl._should_flush(tmp_path, minutes_of_day=13 * 60 + 29, now=now)
    # 13:31 — the 13:30 window opened after the last flush
    assert hbl._should_flush(tmp_path, minutes_of_day=13 * 60 + 31, now=now)
    # but not twice: flush stamped just now → no re-flush at 13:35
    hbl._stamp_flush(tmp_path, now=now)
    assert not hbl._should_flush(tmp_path, minutes_of_day=13 * 60 + 35, now=now + 240)


def test_breakpoint_release_flushes_outside_quiet_hours(tmp_path, monkeypatch):
    (tmp_path / hbl.NIGHT_QUEUE_FILE).write_text('{"text":"x"}\n')
    monkeypatch.setattr(hbl, "_user_recently_active", lambda now=None: True)
    assert hbl._should_flush(tmp_path, minutes_of_day=15 * 60)
    # never during quiet hours, even if the user is active
    assert not hbl._should_flush(tmp_path, minutes_of_day=2 * 60)


def test_no_flush_with_empty_queue(tmp_path, monkeypatch):
    monkeypatch.setattr(hbl, "_user_recently_active", lambda now=None: True)
    assert not hbl._should_flush(tmp_path, minutes_of_day=15 * 60)
