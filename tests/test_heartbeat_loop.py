"""Tests for core.heartbeat_loop — the main cycle logic (now in Python)."""

import json
from pathlib import Path
from unittest.mock import patch, MagicMock

from core.card import build_card
from core import lark_bot_transport
from core.heartbeat_loop import (
    _route_output, _write_outbox, _record_engagement, _trim_file,
    _sleep_gap_seconds,
)


def test_trim_file(tmp_path):
    f = tmp_path / "test.jsonl"
    f.write_text("\n".join(f"line{i}" for i in range(100)) + "\n")
    _trim_file(f, 10)
    assert len(f.read_text().strip().splitlines()) == 10
    assert "line99" in f.read_text()  # keeps last lines


def test_trim_file_no_op(tmp_path):
    f = tmp_path / "test.jsonl"
    f.write_text("line1\nline2\n")
    _trim_file(f, 10)
    assert f.read_text() == "line1\nline2\n"


def test_trim_file_missing(tmp_path):
    _trim_file(tmp_path / "missing.jsonl", 10)  # should not crash


def test_sleep_gap_seconds_ignores_normal_loop_sleep():
    assert _sleep_gap_seconds(wall_elapsed_s=10.5, mono_elapsed_s=10.5,
                              threshold_s=120) == 0


def test_sleep_gap_seconds_detects_host_sleep_pause():
    assert _sleep_gap_seconds(wall_elapsed_s=400, mono_elapsed_s=10,
                              threshold_s=120) == 390


def test_a_long_model_call_is_not_mislabeled_as_host_sleep():
    """Both clocks advance together while Claude thinks for 10 minutes.

    This is what the old expected-interval instrument was protecting, and the
    new one must keep: a slow cycle is not an absence.
    """
    assert _sleep_gap_seconds(wall_elapsed_s=612.0, mono_elapsed_s=612.0,
                              threshold_s=120) == 0


def test_sleep_that_began_inside_a_model_call_is_seen():
    """The 2026-08-18/19 blind spot. The lid closed mid-cycle, so the gap fell
    outside the loop's own 10s sleep and the old instrument logged nothing:
    daemon.py measured 39.4h of host sleep, sched_events recorded 0.7h.

    Bracketing the whole tick against the monotonic clock sees it: 3868s of
    wall time (the real 2026-08-19 13:03 gap) against ~12s of monotonic time.
    """
    gap = _sleep_gap_seconds(wall_elapsed_s=3880.0, mono_elapsed_s=12.0,
                             threshold_s=120)
    assert round(gap) == 3868


def test_write_outbox(tmp_path):
    _write_outbox("Hello world", tmp_path)
    outbox = tmp_path / "heartbeat_outbox.jsonl"
    assert outbox.exists()
    entry = json.loads(outbox.read_text().strip())
    assert entry["role"] == "assistant"
    assert "Hello world" in entry["text"]


def test_record_engagement_with_source(tmp_path):
    # Write source sidecar file
    (tmp_path / ".heartbeat_last_source").write_text("checkin,content-recommend")
    _record_engagement(tmp_path)

    elog = tmp_path / "engagement_log.jsonl"
    entries = [json.loads(l) for l in elog.read_text().splitlines() if l.strip()]
    assert len(entries) == 2
    sources = {e["source"] for e in entries}
    assert sources == {"checkin", "content-recommend"}
    assert all(e["type"] == "sent" for e in entries)


def test_record_engagement_no_source(tmp_path):
    _record_engagement(tmp_path)
    elog = tmp_path / "engagement_log.jsonl"
    entries = [json.loads(l) for l in elog.read_text().splitlines() if l.strip()]
    assert len(entries) == 1
    assert entries[0]["source"] == "heartbeat"


def test_route_output_plain_text():
    """Plain text should be sent via lark_send_text."""
    with patch("core.heartbeat_loop._lark_send_text") as mock_send:
        mock_send.return_value = True
        _route_output("Hello from heartbeat", "user123", Path("/tmp"))
        mock_send.assert_called_once()
        assert "Hello from heartbeat" in mock_send.call_args[0][0]


def test_route_output_card():
    """CARD: prefixed lines should be sent via lark_send_card."""
    card = json.dumps({"config": {}, "header": {"title": {"content": "Test"}}})
    with patch("core.heartbeat_loop._lark_send_card") as mock_card:
        mock_card.return_value = True
        _route_output(f"CARD:{card}", "user123", Path("/tmp"))
        mock_card.assert_called_once()


def _route_memorial_card(mid="mem_route"):
    return build_card(
        "📜 Route", "body",
        buttons=[{"text": "已阅", "value": {
            "action": "memorial", "id": mid, "opt": "read"}}],
    )


def test_route_output_records_memorial_delivery(tmp_path):
    card = _route_memorial_card()
    with patch("core.heartbeat_loop._lark_send_card", return_value=True):
        assert _route_output("CARD:" + card, "user123", tmp_path)

    rows = [
        json.loads(line)
        for line in (tmp_path / "memorials.jsonl").read_text().splitlines()
    ]
    event = rows[0]
    assert event["id"] == "mem_route" and event["status"] == "delivered"


def test_sqlite_flush_result_projects_back_to_memorial_ledger(
        tmp_path, monkeypatch):
    from core import heartbeat_loop as hl
    from core.delivery import DeliveryEnvelope, DeliveryPipeline, TransportResult

    monkeypatch.setenv("JARVIS_DIR", str(tmp_path))
    pipeline = DeliveryPipeline(
        tmp_path,
        transport=lambda *_args: TransportResult(True, "om_projected"),
    )
    result = pipeline.deliver(DeliveryEnvelope(
        source="test", kind="text", payload={"text": "done"},
        memorial_id="mem_projected", requested_channel="lark",
    ))

    hl._project_delivery_flush_results(tmp_path, [result])

    rows = [
        json.loads(line)
        for line in (tmp_path / "memorials.jsonl").read_text().splitlines()
    ]
    event = rows[0]
    assert event["id"] == "mem_projected"
    assert event["status"] == "delivered"
    assert event["message_id"] == "om_projected"
    assert rows[1]["ev"] == "sent"
    assert rows[1]["lark_message_id"] == "om_projected"


def test_route_output_failed_memorial_keeps_card_not_text_fallback(tmp_path):
    card = _route_memorial_card("mem_failed")
    (tmp_path / ".heartbeat_last_source").write_text("mail-triage")
    with patch("core.heartbeat_loop._lark_send_card", return_value=False), \
         patch("core.heartbeat_loop._lark_send_text") as mock_text:
        assert not _route_output("CARD:" + card, "user123", tmp_path)

    mock_text.assert_not_called()
    from core.delivery import DeliveryPipeline
    queued = DeliveryPipeline(tmp_path).list(state="queued")
    assert queued[0]["memorial_id"] == "mem_failed"
    assert json.loads(
        json.loads(queued[0]["payload"])["card_json"]) == json.loads(card)
    assert not (tmp_path / "memorial_queue.jsonl").exists()
    events = [json.loads(line) for line in
              (tmp_path / "memorials.jsonl").read_text().splitlines()]
    assert events[-1]["status"] == "retry_queued"


def test_route_output_preserves_memorial_attention(tmp_path, monkeypatch):
    """A memorial id proves card ownership, not decision-class attention."""
    monkeypatch.setenv("JARVIS_DIR", str(tmp_path))
    from core import memorial
    from core.delivery import DeliveryPipeline

    notice_id, _ = memorial.create(
        "daily-reflect", "复盘", "只是知会", preset="fyi", send=False)
    decision_id, _ = memorial.create(
        "mail", "需要判断", "回不回复？", preset="decision", send=False)
    (tmp_path / ".heartbeat_last_source").write_text(
        "cross-session-sync,daily-reflect,mail")

    with patch("core.heartbeat_loop._lark_send_card", return_value=False):
        _route_output(
            "CARD:" + memorial.card_json(notice_id),
            "user123", tmp_path,
        )
        _route_output(
            "CARD:" + memorial.card_json(decision_id),
            "user123", tmp_path,
        )

    rows = {row["memorial_id"]: row for row in DeliveryPipeline(tmp_path).list()}
    assert rows[notice_id]["attention"] == "notice"
    assert rows[decision_id]["attention"] == "decision"
    assert rows[notice_id]["source"] == "daily-reflect"
    assert rows[decision_id]["source"] == "mail"


def test_route_output_alert_memorial_bypasses_quiet_hours(tmp_path, monkeypatch):
    """Red-team P2 (PR#97): an urgent (alert-attention) mail card must be
    DELIVERED at 3am, not queued to morning.

    The living urgency signal is the memorial's attention="alert" — never the
    .urgent_send sidecar flag, whose only reader (_should_queue) has no
    production caller. _route_output promotes alert to envelope.urgent, and
    core.delivery's existing bypass-quiet semantics send immediately. Clock
    injected (仓库铁律: a test asserting delivered must not read the wall
    clock).
    """
    from datetime import datetime
    from core import delivery as delivery_mod
    from core import memorial
    from core.timeutil import now_local

    monkeypatch.setenv("JARVIS_DIR", str(tmp_path))
    monkeypatch.setenv("JARVIS_QUIET_START", "23:30")
    monkeypatch.setenv("JARVIS_QUIET_END", "10:00")
    # 03:00 in the pipeline's own local timezone — deep inside quiet hours.
    three_am = datetime(
        2026, 8, 21, 3, 0, tzinfo=now_local().tzinfo).timestamp()

    real_pipeline = delivery_mod.DeliveryPipeline

    def deliver_at_3am(envelope, *, root=None, db_path=None, transport=None):
        return real_pipeline(
            root, db_path=db_path, transport=transport,
            clock=lambda: three_am, sleeper=lambda _s: None,
        ).deliver(envelope)

    monkeypatch.setattr(delivery_mod, "deliver", deliver_at_3am)

    alert_id, _ = memorial.create(
        "mail", "银行安全告警", "凌晨异常扣款", send=False, attention="alert")
    notice_id, _ = memorial.create(
        "mail", "普通来信", "白天再看就好", send=False, attention="notice")
    (tmp_path / ".heartbeat_last_source").write_text("mail-triage")

    with patch("core.heartbeat_loop._lark_send_card",
               return_value=True) as mock_card:
        assert _route_output(
            "CARD:" + memorial.card_json(alert_id) + "\n"
            + "CARD:" + memorial.card_json(notice_id),
            "user123", tmp_path, respect_quiet=True,
        )

    rows = {row["memorial_id"]: row for row in real_pipeline(tmp_path).list()}
    assert rows[alert_id]["state"] == "delivered"   # rang through the night
    assert rows[notice_id]["state"] == "queued"     # notice held to morning
    assert mock_card.call_count == 1
    assert alert_id in mock_card.call_args[0][0]


def test_route_output_blocks_raw_json():
    """Raw JSON that isn't a card should be blocked."""
    with patch("core.heartbeat_loop._lark_send_text") as mock_send:
        _route_output('{"internal": "data"}', "user123", Path("/tmp"))
        mock_send.assert_not_called()


def test_route_output_mixed():
    """Mixed card + text should split correctly."""
    card = json.dumps({"config": {}, "header": {"title": {"content": "T"}}})
    output = f"CARD:{card}\nSome plain text"
    with patch("core.heartbeat_loop._lark_send_card") as mock_card, \
         patch("core.heartbeat_loop._lark_send_text") as mock_text:
        mock_card.return_value = True
        mock_text.return_value = True
        _route_output(output, "user123", Path("/tmp"))
        mock_card.assert_called_once()
        mock_text.assert_called_once()
        assert "plain text" in mock_text.call_args[0][0]


def test_record_engagement_skips_silent_sources(tmp_path):
    """REQ-61: a SILENT source (daily-plan) riding a mixed cycle must NOT get a
    'sent' engagement row — its content was dropped at the delivery gate, so
    logging it makes it a fake guaranteed-0% source skewing keep/cut."""
    import json
    from core import heartbeat_loop as hl
    (tmp_path / ".heartbeat_last_source").write_text("daily-plan,checkin")
    hl._LAST_SENT_IDS.clear()
    hl._record_engagement(tmp_path)
    rows = [json.loads(l) for l in (tmp_path / "engagement_log.jsonl").read_text().splitlines() if l.strip()]
    sources = {r["source"] for r in rows if r["type"] == "sent"}
    assert "checkin" in sources            # non-silent logged
    assert "daily-plan" not in sources     # silent skipped (REQ-61)


def test_record_engagement_uses_actual_sent_memorial_sources(
        tmp_path, monkeypatch):
    """Mixed-cycle sidecars must not credit ledger-only ambient segments."""
    from core import heartbeat_loop as hl
    from core import memorial

    monkeypatch.setenv("JARVIS_DIR", str(tmp_path))
    calendar_id, _ = memorial.create(
        "calendar-sync", "会议冲突", "需要选一个",
        preset="decision", send=False)
    (tmp_path / ".heartbeat_last_source").write_text(
        "cross-session-sync,calendar-sync")
    with patch("core.heartbeat_loop._lark_send_card", return_value=True):
        _route_output(
            "CARD:" + memorial.card_json(calendar_id),
            "user123", tmp_path,
        )

    hl._record_engagement(tmp_path)

    rows = [json.loads(line) for line in
            (tmp_path / "engagement_log.jsonl").read_text().splitlines()]
    assert [row["source"] for row in rows] == ["calendar-sync"]


def test_pure_ambient_ledger_outcome_is_not_counted_as_a_send(
        tmp_path, monkeypatch):
    """Ledger-only content is handled successfully without fake engagement."""
    from core import heartbeat_loop as hl
    from core import memorial

    monkeypatch.setenv("JARVIS_DIR", str(tmp_path))
    (tmp_path / ".heartbeat_last_source").write_text("cross-session-sync")
    output = memorial.memorialize_output(
        "PGC 当前 0 告警，磁盘风险已解除。", "cross-session-sync")

    assert output == ""
    assert _route_output(output, "user123", tmp_path) is True
    assert hl._LAST_ROUTE_ALL_DELIVERED is False
    assert not (tmp_path / hl.DELIVERED_SOURCES_FILE).exists()


def test_beat_throttles_and_keeps_daemon_greppable_format(capsys, monkeypatch):
    """Log-hygiene fix (2026-07-07): per-tick 'Beat sent' was 65% of
    jarvis.log. The throttled line must still (a) emit on the FIRST call of a
    fresh process — daemon.py/doctor.sh find a beat right after start — and
    (b) keep the exact format daemon.py's _find_last_heartbeat regex greps,
    or the daemon restarts a healthy loop."""
    import re
    from core import heartbeat_loop as hl

    monkeypatch.setattr(hl._beat, "_last_emit", 0.0, raising=False)
    assert hl._beat("working") is True            # first call always emits
    line = capsys.readouterr().err.strip()
    # daemon.py:_find_last_heartbeat's bracket-format regex, verbatim.
    assert re.search(
        r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\].*heartbeat.*Beat sent", line)
    assert "(working)" in line

    assert hl._beat("working") is False           # inside interval: suppressed
    assert capsys.readouterr().err == ""

    assert hl._beat("idle", force=True) is True   # transition edge bypasses
    assert "(idle)" in capsys.readouterr().err


def test_beat_touches_stamp_on_every_call_even_when_throttled(tmp_path, monkeypatch):
    """F15 (7/8 audit): the daemon's liveness check stats the stamp file's
    mtime instead of parsing jarvis.log, so the stamp must be touched on
    EVERY _beat() call — including the throttled common path where no log
    line is emitted."""
    import os
    import time
    from core import heartbeat_loop as hl

    monkeypatch.setattr(hl, "_BEAT_STAMP_PATH", None)  # register for restore
    hl._init_beat_stamp(tmp_path)
    assert hl._BEAT_STAMP_PATH == tmp_path / "data" / ".heartbeat_beat"

    monkeypatch.setattr(hl._beat, "_last_emit", 0.0, raising=False)
    assert hl._beat("working") is True
    assert hl._BEAT_STAMP_PATH.exists()

    old = time.time() - 3600
    os.utime(hl._BEAT_STAMP_PATH, (old, old))
    assert hl._beat("working") is False        # log line throttled…
    assert hl._BEAT_STAMP_PATH.stat().st_mtime > old + 3000  # …stamp still fresh


def test_beat_stamp_write_failure_never_kills_the_loop(tmp_path, monkeypatch):
    from core import heartbeat_loop as hl
    monkeypatch.setattr(hl, "_BEAT_STAMP_PATH",
                        tmp_path / "no-such-dir" / ".heartbeat_beat")
    monkeypatch.setattr(hl._beat, "_last_emit", 0.0, raising=False)
    assert hl._beat("working") is True  # OSError swallowed, beat still emitted


def test_beat_interval_plus_max_cycle_stays_under_stale_threshold():
    """daemon.py restarts the heartbeat past HEARTBEAT_STALE_THRESHOLD=1800s.
    Worst-case beat gap = full throttle suppression + one long cycle (heavy
    solo task + shared batch call) — red-team 7/8: at 600s the gap reached
    ~2200s and the daemon killed the stack mid-heavy-task. The invariant in
    heartbeat_loop.py's BEAT_LOG_INTERVAL_S comment, machine-checked."""
    import daemon as daemon_mod
    from core import heartbeat_loop as hl
    from core.heartbeat import HeartbeatRunner

    batch_timeout = 600  # HEARTBEAT_TIMEOUT default (bot.sh → run_loop)
    worst_gap = (hl.BEAT_LOG_INTERVAL_S
                 + HeartbeatRunner.HEAVY_DEFAULT_TIMEOUT
                 + batch_timeout)
    assert worst_gap < daemon_mod.HEARTBEAT_STALE_THRESHOLD, \
        f"worst-case beat gap {worst_gap}s crosses the daemon's " \
        f"{daemon_mod.HEARTBEAT_STALE_THRESHOLD}s stale threshold"


def test_route_output_suppresses_sentinel_text():
    """Prose + trailing HEARTBEAT_OK must never reach the user (2026-07-15)."""
    with patch("core.heartbeat_loop._lark_send_text") as mock_text, \
         patch("core.heartbeat_loop._lark_send_card") as mock_card:
        ok = _route_output(
            "nothing noteworthy here.\n\nHEARTBEAT_OK", "user123", Path("/tmp"))
        mock_text.assert_not_called()
        mock_card.assert_not_called()
        assert ok is True  # silence is a successful outcome, not a failure


def test_route_output_suppresses_sentinel_card_line():
    """Even a pre-built card JSON carrying the sentinel is dropped."""
    card = json.dumps({"config": {}, "elements": [{
        "tag": "div", "text": {"content": "scratch\nHEARTBEAT_OK", "tag": "lark_md"}}]})
    with patch("core.heartbeat_loop._lark_send_card") as mock_card:
        _route_output(f"CARD:{card}", "user123", Path("/tmp"))
        mock_card.assert_not_called()


def test_route_output_clean_lines_still_flow():
    with patch("core.heartbeat_loop._lark_send_text") as mock_text:
        mock_text.return_value = True
        _route_output("正常心跳消息", "user123", Path("/tmp"))
        mock_text.assert_called_once()


def test_low_level_text_send_uses_bot_api_and_records_message_id(monkeypatch):
    from core import heartbeat_loop as hl

    hl._LAST_SENT_IDS.clear()
    monkeypatch.setattr(
        lark_bot_transport,
        "send",
        lambda **_kwargs: lark_bot_transport.BotSendResult(
            True, True, "om_heartbeat"
        ),
    )
    monkeypatch.setattr(
        hl.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("CLI should not run")
        ),
    )

    assert hl._lark_send_text("hello", "ou_owner", retries=False) is True
    assert hl._LAST_SENT_IDS == ["om_heartbeat"]


def test_bot_api_timeout_retries_safely_and_requires_receipt(monkeypatch):
    from core import heartbeat_loop as hl

    calls = []
    outcomes = [
        lark_bot_transport.BotSendResult(True, False, error="timeout"),
        lark_bot_transport.BotSendResult(True, True, "om_after_timeout"),
    ]
    monkeypatch.setattr(
        lark_bot_transport,
        "send",
        lambda **kwargs: (
            calls.append(kwargs)
            or outcomes.pop(0)
        ),
    )
    monkeypatch.setattr(hl.time, "sleep", lambda _delay: None)

    assert hl._lark_send_text(
        "single", "ou_owner", assume_delivered_on_timeout=True,
    ) is True
    assert len(calls) == 2
    assert calls[0]["idempotency_key"] == calls[1]["idempotency_key"]
    assert hl._LAST_SENT_IDS[-1] == "om_after_timeout"
    calls.clear()
    outcomes[:] = [
        lark_bot_transport.BotSendResult(True, False, error="timeout"),
    ]
    assert hl._lark_send_text(
        "durable queue", "ou_owner", assume_delivered_on_timeout=False,
        retries=False,
    ) is False
    assert len(calls) == 1


# --- 2026-08-19: a card the daily cap dropped reached no surface at all -----
# Both suppressed branches in _route_output recorded nothing, so the memorial
# ledger held only the `create` row. core.presence.ledger_only() counts
# explicit ledger-only rows on purpose, so the thirteen cards suppressed with
# `global_daily_cap` between 14:02 and 23:15 never made the morning digest —
# they simply stopped existing.

def _suppressed(reason):
    from core.delivery import DeliveryResult
    return lambda envelope, **kw: DeliveryResult(
        envelope.id, True, "suppressed", "lark", "", reason)


def test_cap_dropped_card_is_recorded_as_ledger_only(tmp_path):
    card = _route_memorial_card("mem_capped")
    with patch("core.delivery.deliver", _suppressed("global_daily_cap")):
        _route_output("CARD:" + card, "user123", tmp_path)

    events = [json.loads(line) for line in
              (tmp_path / "memorials.jsonl").read_text().splitlines()]
    assert [e["status"] for e in events] == ["ledger_only"], (
        "a budget drop is 'no room today', not 'no longer true' — it must "
        "keep the status the morning 攒批 line counts")


def test_obsolete_suppression_is_not_resurfaced(tmp_path):
    """Recovery suppressions are stale by design; the digest must not batch
    them back into Pascal's morning."""
    card = _route_memorial_card("mem_obsolete")
    with patch("core.delivery.deliver",
               _suppressed("recovery_incident_obsolete")):
        _route_output("CARD:" + card, "user123", tmp_path)

    events = [json.loads(line) for line in
              (tmp_path / "memorials.jsonl").read_text().splitlines()]
    assert [e["status"] for e in events] == ["suppressed"]


def test_cap_dropped_config_card_is_recorded_too(tmp_path):
    """The bare-JSON card branch had the same silent hole."""
    card = _route_memorial_card("mem_capped_cfg")
    with patch("core.delivery.deliver", _suppressed("source_daily_cap")):
        _route_output(card, "user123", tmp_path)

    events = [json.loads(line) for line in
              (tmp_path / "memorials.jsonl").read_text().splitlines()]
    assert [e["status"] for e in events] == ["ledger_only"]
