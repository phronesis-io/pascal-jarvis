"""Tests for core.ef_stream_loop delivery accounting + stall watchdog.

Audit 2026-07-10: a failed Lark send used to fall through to remember_seen +
outbox + a "Delivered" log line — the dedup set then guaranteed the message
could never be re-delivered while every ledger claimed success. And a
half-open TCP connection could leave the stream subprocess alive but silent
forever, wedging the blocking stdout read past every process-existence check.
"""

import json
from types import SimpleNamespace

import core.ef_stream_loop as efsl
from core.ef_stream import load_seen


# ---- _deliver_and_mark: only a REAL success is recorded -------------------

def _deliver(monkeypatch, tmp_path, send_ok):
    sent = []

    def fake_send(msg, uid):
        sent.append((msg, uid))
        return send_ok

    monkeypatch.setattr(efsl, "_lark_send", fake_send)
    seen_file = tmp_path / ".ef-seen"
    seen, delivered = efsl._deliver_and_mark(
        "hello from ef", ["id1"], {"conv_id": "c1"}, "u1",
        [], seen_file, tmp_path)
    return seen, delivered, seen_file, sent


def test_failed_send_not_marked_seen_and_deadlettered(monkeypatch, tmp_path):
    seen, delivered, seen_file, sent = _deliver(monkeypatch, tmp_path,
                                                send_ok=False)
    assert sent  # the send was attempted
    assert delivered is False
    assert seen == []                # dedup must NOT swallow a redelivery
    assert not seen_file.exists()
    # no phantom "Delivered" ledger entry
    assert not (tmp_path / "heartbeat_outbox.jsonl").exists()
    # dead-letter row for daemon.py's independent channel
    dl = tmp_path / "data" / ".delivery_deadletter.jsonl"
    assert dl.exists()
    row = json.loads(dl.read_text(encoding="utf-8").splitlines()[-1])
    assert row["kind"] == "ef_stream_send_failed"
    assert "hello from ef" in row["detail"]


def test_successful_send_marks_seen_and_outbox(monkeypatch, tmp_path):
    seen, delivered, seen_file, _ = _deliver(monkeypatch, tmp_path,
                                             send_ok=True)
    assert delivered is True
    assert seen == ["id1"]
    assert load_seen(seen_file) == ["id1"]
    outbox = (tmp_path / "heartbeat_outbox.jsonl").read_text(encoding="utf-8")
    assert "hello from ef" in outbox
    # success writes no dead-letter
    assert not (tmp_path / "data" / ".delivery_deadletter.jsonl").exists()


def test_deadletter_failure_does_not_raise(monkeypatch, tmp_path):
    # Bookkeeping must never kill the stream loop.
    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(efsl, "record_overdue", boom)
    monkeypatch.setattr(efsl, "_lark_send", lambda m, u: False)
    seen, delivered = efsl._deliver_and_mark(
        "msg", ["id2"], {}, "u1", [], tmp_path / ".ef-seen", tmp_path)
    assert delivered is False and seen == []


# ---- _is_stalled: alive-but-silent subprocess detection -------------------

def test_stall_predicate():
    live = SimpleNamespace(poll=lambda: None)
    dead = SimpleNamespace(poll=lambda: 1)
    t = efsl.STALL_KILL_AFTER_S
    assert efsl._is_stalled(live, t + 1)          # alive + long silence → kill
    assert not efsl._is_stalled(live, t - 1)      # silence within budget
    assert not efsl._is_stalled(dead, t + 1)      # exited → respawn path owns it
    assert not efsl._is_stalled(None, t + 1)      # nothing spawned yet
