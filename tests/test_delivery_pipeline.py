"""Unified delivery pipeline contract tests."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime

import pytest

import core.delivery as delivery
from core.delivery import (
    DeliveryEnvelope,
    DeliveryPipeline,
    TransportResult,
)


@pytest.fixture
def pipeline(tmp_path):
    sent = []
    now = [datetime(2026, 7, 23, 14, 0).timestamp()]

    def transport(envelope, channel):
        sent.append((envelope, channel))
        return TransportResult(True, f"om_{len(sent)}")

    pipe = DeliveryPipeline(
        tmp_path, db_path=tmp_path / "jarvis.db",
        transport=transport, clock=lambda: now[0],
        sleeper=lambda _seconds: None,
    )
    return pipe, sent, now


def test_state_machine_deliver_read_acted(pipeline):
    pipe, sent, _ = pipeline
    result = pipe.deliver(DeliveryEnvelope(
        source="test", kind="text", payload={"text": "需要马上知道"},
        attention="alert",
    ))
    assert result.state == "delivered"
    assert result.channel == "lark"
    assert sent[0][1] == "lark"
    assert pipe.confirm(result.delivery_id, "read").state == "read"
    assert pipe.confirm(result.delivery_id, "acted").state == "acted"
    assert pipe.get(result.delivery_id)["acted_epoch"]


def test_every_delivery_connection_is_closed(monkeypatch, tmp_path):
    """A resident heartbeat must not leak one SQLite FD per state change."""
    opened = []
    real_connect = delivery._connect

    def tracked_connect(path):
        connection = real_connect(path)
        opened.append(connection)
        return connection

    monkeypatch.setattr(delivery, "_connect", tracked_connect)
    pipe = DeliveryPipeline(
        tmp_path,
        db_path=tmp_path / "jarvis.db",
        transport=lambda _envelope, _channel: TransportResult(True, "msg-ok"),
        sleeper=lambda _seconds: None,
    )
    assert pipe.deliver(
        DeliveryEnvelope(source="fd-test", payload={"text": "hello"})
    ).state == "delivered"
    assert opened
    for connection in opened:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connection.execute("SELECT 1")


def test_notice_and_phone_decision_route_to_web(pipeline):
    pipe, sent, _ = pipeline
    notice = pipe.deliver(DeliveryEnvelope(
        source="digest", payload={"text": "今天没有异常"}))
    decision = pipe.deliver(DeliveryEnvelope(
        source="mail", payload={"text": "是否回复"},
        attention="decision", metadata={"review_surface": "phone"}))
    assert notice.channel == "web"
    assert decision.channel == "web"
    assert [channel for _, channel in sent] == ["web", "web"]


def test_reply_bypasses_quiet_and_uses_reply_channel(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_QUIET_START", "00:00")
    monkeypatch.setenv("JARVIS_QUIET_END", "23:59")
    sent = []

    def transport(envelope, channel):
        sent.append(channel)
        return TransportResult(True, "om_reply")

    pipe = DeliveryPipeline(
        tmp_path, db_path=tmp_path / "db.sqlite", transport=transport,
        clock=lambda: datetime(2026, 7, 23, 3, 0).timestamp(),
        sleeper=lambda _: None,
    )
    result = pipe.deliver(DeliveryEnvelope(
        source="bot-reply", kind="reply", payload={"text": "在"},
        attention="reply", reply_to="om_incoming",
    ))
    assert result.state == "delivered"
    assert sent == ["lark_reply"]


def test_reply_can_return_user_requested_json(tmp_path):
    sent = []
    pipe = DeliveryPipeline(
        tmp_path, db_path=tmp_path / "db.sqlite",
        transport=lambda envelope, channel: (
            sent.append((envelope.payload["text"], channel))
            or TransportResult(True, "om_json")),
        sleeper=lambda _: None,
    )
    result = pipe.deliver(DeliveryEnvelope(
        source="bot-reply",
        kind="reply",
        payload={"text": '{"answer": 42}'},
        attention="reply",
        reply_to="om_question",
    ))
    assert result.state == "delivered"
    assert sent == [('{"answer": 42}', "lark_reply")]


def test_quiet_hours_queue_then_flush(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_QUIET_START", "23:30")
    monkeypatch.setenv("JARVIS_QUIET_END", "10:00")
    now = [datetime(2026, 7, 23, 23, 45).timestamp()]
    sent = []

    def transport(envelope, channel):
        sent.append(channel)
        return TransportResult(True)

    pipe = DeliveryPipeline(
        tmp_path, db_path=tmp_path / "db.sqlite", transport=transport,
        clock=lambda: now[0], sleeper=lambda _: None,
    )
    result = pipe.deliver(DeliveryEnvelope(
        source="calendar", payload={"text": "明早安排"},
        requested_channel="lark",
    ))
    assert result.state == "queued"
    assert result.reason == "quiet_hours"
    assert not sent
    now[0] = datetime(2026, 7, 24, 10, 1).timestamp()
    flushed = pipe.flush_due()
    assert flushed[0].state == "delivered"
    assert sent == ["lark"]


def test_global_content_dedup_crosses_sources(pipeline):
    pipe, sent, now = pipeline
    first = pipe.deliver(DeliveryEnvelope(
        source="heartbeat-a", payload={"text": "同一件事情"},
        requested_channel="lark"))
    second = pipe.deliver(DeliveryEnvelope(
        source="heartbeat-b", payload={"text": "  同一件事情  "},
        requested_channel="lark"))
    assert second.delivery_id == first.delivery_id
    assert second.reason == "duplicate"
    assert len(sent) == 1
    now[0] += 6 * 3600 + 1
    third = pipe.deliver(DeliveryEnvelope(
        source="heartbeat-b", payload={"text": "同一件事情"},
        requested_channel="lark"))
    assert third.delivery_id != first.delivery_id


def test_dynamic_memorial_ids_do_not_defeat_dedup(pipeline):
    pipe, sent, _ = pipeline
    one = pipe.deliver(DeliveryEnvelope(
        source="x", payload={"text": "请处理 mem_123_1_1"},
        requested_channel="lark"))
    two = pipe.deliver(DeliveryEnvelope(
        source="y", payload={"text": "请处理 mem_999_2_2"},
        requested_channel="lark"))
    assert two.delivery_id == one.delivery_id
    assert len(sent) == 1


def test_metric_and_source_throttle_are_durable(pipeline):
    pipe, sent, _ = pipeline
    one = pipe.deliver(DeliveryEnvelope(
        source="monitor", payload={"text": "CPU high"},
        requested_channel="lark", throttle_key="cpu:high"))
    two = pipe.deliver(DeliveryEnvelope(
        source="monitor", payload={"text": "CPU still high"},
        requested_channel="lark", throttle_key="cpu:high"))
    assert one.state == "delivered"
    assert two.state == "suppressed"
    assert two.reason == "metric_daily_cap"
    assert len(sent) == 1


def test_global_daily_cap(pipeline):
    pipe, sent, _ = pipeline
    for index in range(2):
        result = pipe.deliver(DeliveryEnvelope(
            source=f"s{index}", payload={"text": f"message {index}"},
            requested_channel="lark",
            metadata={"global_daily_cap": 1, "source_daily_cap": 9},
        ))
    assert result.state == "suppressed"
    assert result.reason == "global_daily_cap"
    assert len(sent) == 1


@pytest.mark.parametrize("text,reason", [
    ("HEARTBEAT_OK", "idle_sentinel"),
    ("You've hit your monthly spend limit", "error_surface"),
    ('{"internal":"payload"}', "raw_json"),
    ("I'll inspect the repository now.", "tool_narration"),
    ("🔧 Execution error", "tool_narration"),
])
def test_sanitize_blocks_internal_surfaces(pipeline, text, reason):
    pipe, sent, _ = pipeline
    result = pipe.deliver(DeliveryEnvelope(
        source="heartbeat", payload={"text": text},
        requested_channel="lark"))
    assert result.state == "suppressed"
    assert result.reason == reason
    assert not sent


def test_sanitize_strips_plan_line_but_keeps_answer(pipeline):
    pipe, sent, _ = pipeline
    result = pipe.deliver(DeliveryEnvelope(
        source="heartbeat",
        payload={"text": "I'll inspect that now.\n真正需要你看的结论"},
        requested_channel="lark",
    ))
    assert result.state == "delivered"
    assert sent[0][0].payload["text"] == "真正需要你看的结论"


def test_card_sanitization_rewrites_markdown(pipeline):
    pipe, sent, _ = pipeline
    card = {
        "config": {},
        "elements": [{"tag": "markdown",
                      "content": "Reading the file now.\n真正内容"}],
    }
    result = pipe.deliver(DeliveryEnvelope(
        source="heartbeat", kind="card",
        payload={"card_json": json.dumps(card)},
        attention="alert", requested_channel="lark",
    ))
    assert result.state == "delivered"
    delivered = json.loads(sent[0][0].payload["card_json"])
    assert delivered["elements"][0]["content"] == "真正内容"


def test_retry_then_delivery_records_attempts(tmp_path):
    calls = []

    def transport(_envelope, _channel):
        calls.append(1)
        return (TransportResult(False, error="no")
                if len(calls) < 3 else TransportResult(True, "om_ok"))

    pipe = DeliveryPipeline(
        tmp_path, db_path=tmp_path / "db.sqlite", transport=transport,
        clock=lambda: datetime(2026, 7, 23, 14, 0).timestamp(),
        sleeper=lambda _: None,
    )
    result = pipe.deliver(DeliveryEnvelope(
        source="test", payload={"text": "retry me"},
        requested_channel="lark"))
    assert result.state == "delivered"
    assert result.message_id == "om_ok"
    assert pipe.get(result.delivery_id)["attempts"] == 3


def test_attempt_claim_prevents_duplicate_transport(tmp_path):
    sent = []
    pipe = DeliveryPipeline(
        tmp_path, db_path=tmp_path / "db.sqlite",
        transport=lambda _e, _c: (
            sent.append(1) or TransportResult(True, "om_once")),
        sleeper=lambda _: None,
    )
    envelope = DeliveryEnvelope(
        source="test",
        payload={"text": "claim once"},
        requested_channel="lark",
        metadata={"force_queue": True},
    )
    queued = pipe.deliver(envelope)
    assert queued.state == "queued"
    assert pipe._attempt(envelope, "lark").state == "delivered"
    assert pipe._attempt(envelope, "lark").state == "delivered"
    assert sent == [1]


def test_transport_exception_is_retried_and_durably_queued(tmp_path):
    pipe = DeliveryPipeline(
        tmp_path, db_path=tmp_path / "db.sqlite",
        transport=lambda _e, _c: (_ for _ in ()).throw(
            RuntimeError("adapter crashed")),
        clock=lambda: datetime(2026, 7, 23, 14, 0).timestamp(),
        sleeper=lambda _: None,
    )
    result = pipe.deliver(DeliveryEnvelope(
        source="test", payload={"text": "do not lose me"},
        requested_channel="lark"))
    assert result.state == "queued"
    assert pipe.get(result.delivery_id)["attempts"] == 3
    assert pipe.get(result.delivery_id)["last_error"] == "adapter crashed"


def test_retry_exhaustion_is_durable_and_dead_lettered(tmp_path):
    pipe = DeliveryPipeline(
        tmp_path, db_path=tmp_path / "db.sqlite",
        transport=lambda _e, _c: TransportResult(False, error="offline"),
        clock=lambda: datetime(2026, 7, 23, 14, 0).timestamp(),
        sleeper=lambda _: None,
    )
    result = pipe.deliver(DeliveryEnvelope(
        source="test", payload={"text": "keep me"},
        requested_channel="lark"))
    assert result.accepted is True
    assert result.state == "queued"
    assert pipe.get(result.delivery_id)["last_error"] == "offline"
    dead = pipe.pending_dead_letters()
    assert dead[0]["delivery_id"] == result.delivery_id
    pipe.mark_dead_letters_notified([dead[0]["id"]])
    assert pipe.pending_dead_letters() == []
    pipe.flush_due()
    assert pipe.pending_dead_letters() == []


def test_provider_and_model_are_recorded(pipeline):
    pipe, _, _ = pipeline
    result = pipe.deliver(DeliveryEnvelope(
        source="test", payload={"text": "model trace"},
        provider="GPT fallback", model="gpt-test"))
    row = pipe.get(result.delivery_id)
    assert row["provider"] == "GPT fallback"
    assert row["model"] == "gpt-test"


def test_confirm_rejects_undelivered_state(tmp_path):
    pipe = DeliveryPipeline(
        tmp_path, db_path=tmp_path / "db.sqlite",
        transport=lambda _e, _c: TransportResult(False, error="offline"),
        sleeper=lambda _: None,
    )
    result = pipe.deliver(DeliveryEnvelope(
        source="test", payload={"text": "not yet"},
        requested_channel="lark"))
    with pytest.raises(ValueError):
        pipe.confirm(result.delivery_id, "read")
