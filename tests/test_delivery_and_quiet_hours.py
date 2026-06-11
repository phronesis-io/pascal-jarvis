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
    assert _in_quiet_hours(9 * 60 + 29)       # 09:29 — still quiet
    assert not _in_quiet_hours(9 * 60 + 30)   # 09:30 — opens
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
