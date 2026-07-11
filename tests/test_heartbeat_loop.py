"""Tests for core.heartbeat_loop — the main cycle logic (now in Python)."""

import json
from pathlib import Path
from unittest.mock import patch, MagicMock

from core.card import build_card
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
    assert _sleep_gap_seconds(slept_for_s=10.5, expected_s=10, threshold_s=120) == 0


def test_sleep_gap_seconds_detects_host_sleep_pause():
    assert _sleep_gap_seconds(slept_for_s=400, expected_s=10, threshold_s=120) == 390


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

    event = json.loads((tmp_path / "memorials.jsonl").read_text().strip())
    assert event["id"] == "mem_route" and event["status"] == "delivered"


def test_route_output_failed_memorial_keeps_card_not_text_fallback(tmp_path):
    card = _route_memorial_card("mem_failed")
    (tmp_path / ".heartbeat_last_source").write_text("mail-triage")
    with patch("core.heartbeat_loop._lark_send_card", return_value=False), \
         patch("core.heartbeat_loop._lark_send_text") as mock_text:
        assert not _route_output("CARD:" + card, "user123", tmp_path)

    mock_text.assert_not_called()
    queued = json.loads(
        (tmp_path / "memorial_queue.jsonl").read_text().strip())
    assert queued["memorial_id"] == "mem_failed"
    assert queued["card_json"] == card
    events = [json.loads(line) for line in
              (tmp_path / "memorials.jsonl").read_text().splitlines()]
    assert events[-1]["status"] == "retry_queued"


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
