"""Unified delivery pipeline contract tests."""

from __future__ import annotations

import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import pytest

import core.delivery as delivery
from core.delivery import (
    DeliveryEnvelope,
    DeliveryPipeline,
    TransportResult,
)


def _local_ts(*args) -> float:
    """Epoch for a wall-clock moment in the pipeline's own local timezone.

    core.delivery resolves quiet hours and day boundaries via
    core.timeutil.now_local() (reads /etc/localtime, ignores the TZ env).
    A naive datetime().timestamp() is interpreted under the TZ env instead,
    so on a machine where the two disagree (CI at UTC, a dev shell with
    TZ=UTC) a "14:00" clock silently lands inside quiet hours or on the
    wrong day. Pinning tzinfo makes injected clocks mean what they say
    everywhere (2026-08-11 CI incident: three tests red only at UTC 05:11).
    """
    from core.timeutil import now_local
    return datetime(*args, tzinfo=now_local().tzinfo).timestamp()


def test_default_paths_resolve_environment_at_call_time(tmp_path, monkeypatch):
    """Importing delivery must not pin later JARVIS_DIR test injection."""
    monkeypatch.setenv("JARVIS_DIR", str(tmp_path))
    monkeypatch.delenv("JARVIS_DB_PATH", raising=False)

    pipe = DeliveryPipeline(transport=lambda *_: TransportResult(True))

    assert pipe.root == tmp_path
    assert pipe.path == tmp_path / "data" / "jarvis.db"


@pytest.fixture
def pipeline(tmp_path):
    sent = []
    now = [_local_ts(2026, 7, 23, 14, 0)]

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
        clock=lambda: _local_ts(2026, 7, 23, 14, 0),  # daytime: no quiet queue
        sleeper=lambda _seconds: None,
    )
    assert pipe.deliver(
        DeliveryEnvelope(source="fd-test", payload={"text": "hello"})
    ).state == "delivered"
    assert opened
    assert len(opened) <= 2
    for connection in opened:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connection.execute("SELECT 1")


def test_delivery_schema_is_initialized_once_per_database(
        monkeypatch, tmp_path):
    calls = []
    real_ensure = delivery._ensure_schema

    def tracked_ensure(db):
        calls.append(1)
        real_ensure(db)

    monkeypatch.setattr(delivery, "_ensure_schema", tracked_ensure)
    path = tmp_path / "jarvis.db"
    with delivery.closing(delivery._connect(path)):
        pass
    with delivery.closing(delivery._connect(path)):
        pass

    assert calls == [1]


def test_notices_and_decisions_route_to_lark(pipeline):
    """REQ-119: Lark is the only surface — auto-routing never lands on the
    retired web channel, whatever legacy review_surface metadata says."""
    pipe, sent, _ = pipeline
    notice = pipe.deliver(DeliveryEnvelope(
        source="digest", payload={"text": "今天没有异常"}))
    decision = pipe.deliver(DeliveryEnvelope(
        source="mail", payload={"text": "是否回复"},
        attention="decision", metadata={"review_surface": "phone"}))
    assert notice.channel == "lark"
    assert decision.channel == "lark"
    assert [channel for _, channel in sent] == ["lark", "lark"]


def test_no_path_creates_a_web_envelope(pipeline):
    """Regression guard for the fake web transport: every route the pipeline
    can pick must be a real channel; no new row may carry route_channel=web."""
    pipe, sent, _ = pipeline
    for envelope in (
        DeliveryEnvelope(source="a", payload={"text": "notice"}),
        DeliveryEnvelope(source="b", payload={"text": "alert"},
                         attention="alert"),
        DeliveryEnvelope(source="c", payload={"text": "decision"},
                         attention="decision"),
        DeliveryEnvelope(source="d", payload={"text": "legacy phone"},
                         metadata={"review_surface": "phone"}),
        DeliveryEnvelope(source="e", payload={"text": "legacy none"},
                         metadata={"review_surface": "none"}),
        DeliveryEnvelope(source="f", payload={"text": "legacy web meta"},
                         metadata={"review_surface": "web"}),
    ):
        pipe.deliver(envelope)
    assert sent, "envelopes must actually reach the transport"
    assert all(channel != "web" for _, channel in sent)
    rows = pipe.list(limit=500)
    assert rows and all(row["route_channel"] != "web" for row in rows)


def test_explicit_web_request_is_refused_not_faked(tmp_path):
    """An explicit requested_channel=web must fail loudly — the old default
    transport returned unconditional success for it (the 1.8%-read fake
    ledger). Exercises the REAL production transport: the web channel falls
    through to the unknown-channel refusal before any subprocess runs."""
    pipe = DeliveryPipeline(
        tmp_path, db_path=tmp_path / "jarvis.db",
        transport=delivery._default_transport(tmp_path),
        clock=lambda: _local_ts(2026, 7, 23, 14, 0),  # daytime: reach transport
        sleeper=lambda _seconds: None,
    )
    result = pipe.deliver(DeliveryEnvelope(
        source="legacy-web-caller", payload={"text": "老调用点"},
        requested_channel="web"))
    assert result.state != "delivered"
    row = pipe.get(result.delivery_id)
    assert "unknown channel: web" in str(row["last_error"])


def test_web_kind_is_rejected():
    with pytest.raises(ValueError):
        DeliveryEnvelope(source="x", kind="web",
                         payload={"text": "t"}).normalized()


def test_flush_sweeps_legacy_web_rows_as_suppressed(pipeline):
    """Rows queued for the retired web surface before REQ-119 must neither
    fake-deliver nor churn into dead letters — they suppress, honestly."""
    pipe, sent, _ = pipeline
    with delivery.closing(delivery._connect(pipe.path)) as db, db:
        db.execute(
            "INSERT INTO delivery_envelopes (id,source,kind,attention,"
            "requested_channel,route_channel,state,content_hash,payload,"
            "metadata,created_epoch,updated_epoch) "
            "VALUES ('dlv_legacy','old-source','text','notice','auto','web',"
            "'queued','h','{\"text\":\"老网页卡\"}','{}',1,1)")
    results = pipe.flush_due()
    legacy = [r for r in results if r.delivery_id == "dlv_legacy"]
    assert legacy and legacy[0].state == "suppressed"
    assert legacy[0].reason == "web_surface_retired"
    assert all(channel != "web" for _, channel in sent)
    assert pipe.get("dlv_legacy")["state"] == "suppressed"


def test_reply_bypasses_quiet_and_uses_reply_channel(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_QUIET_START", "00:00")
    monkeypatch.setenv("JARVIS_QUIET_END", "23:59")
    sent = []

    def transport(envelope, channel):
        sent.append(channel)
        return TransportResult(True, "om_reply")

    pipe = DeliveryPipeline(
        tmp_path, db_path=tmp_path / "db.sqlite", transport=transport,
        clock=lambda: _local_ts(2026, 7, 23, 3, 0),
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
    now = [_local_ts(2026, 7, 23, 23, 45)]
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
    now[0] = _local_ts(2026, 7, 24, 10, 1)
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
    assert result.state == "queued"
    assert result.reason == "global_daily_cap"
    assert len(sent) == 1


def test_global_daily_budget_releases_next_morning(tmp_path):
    now = [_local_ts(2026, 8, 12, 14, 0)]
    sent = []
    pipe = DeliveryPipeline(
        tmp_path, db_path=tmp_path / "db.sqlite",
        transport=lambda envelope, _channel: (
            sent.append(envelope.payload["text"])
            or TransportResult(True, f"om_{len(sent)}")),
        clock=lambda: now[0], sleeper=lambda _: None,
    )
    metadata = {"global_daily_cap": 1, "source_daily_cap": 9}
    assert pipe.deliver(DeliveryEnvelope(
        source="one", payload={"text": "first"},
        metadata=metadata)).state == "delivered"
    deferred = pipe.deliver(DeliveryEnvelope(
        source="two", payload={"text": "second"},
        metadata=metadata))
    assert deferred.state == "queued"
    assert pipe.get(deferred.delivery_id)["next_attempt_epoch"] > now[0]

    now[0] = _local_ts(2026, 8, 13, 9, 31)
    assert pipe.flush_due()[0].state == "delivered"
    assert sent == ["first", "second"]


def test_alert_and_reply_bypass_proactive_daily_budget(tmp_path):
    now = [_local_ts(2026, 8, 12, 14, 0)]
    sent = []
    pipe = DeliveryPipeline(
        tmp_path, db_path=tmp_path / "db.sqlite",
        transport=lambda envelope, _channel: (
            sent.append(envelope.payload["text"])
            or TransportResult(True, f"om_{len(sent)}")),
        clock=lambda: now[0], sleeper=lambda _: None,
    )
    metadata = {"global_daily_cap": 1, "source_daily_cap": 9}
    pipe.deliver(DeliveryEnvelope(
        source="ordinary", payload={"text": "normal"}, metadata=metadata))
    alert = pipe.deliver(DeliveryEnvelope(
        source="guardian", payload={"text": "urgent alert"},
        attention="alert", metadata=metadata))
    reply = pipe.deliver(DeliveryEnvelope(
        source="bot-reply", kind="reply", attention="reply",
        reply_to="om_user", payload={"text": "reply"}, metadata=metadata))
    assert alert.state == reply.state == "delivered"
    assert sent == ["normal", "urgent alert", "reply"]


def test_burst_budget_queues_fifth_card_without_losing_it(tmp_path):
    now = [_local_ts(2026, 8, 12, 10, 0)]
    sent = []
    pipe = DeliveryPipeline(
        tmp_path, db_path=tmp_path / "db.sqlite",
        transport=lambda envelope, _channel: (
            sent.append(envelope.payload["text"])
            or TransportResult(True, f"om_{len(sent)}")),
        clock=lambda: now[0], sleeper=lambda _: None,
    )
    metadata = {
        "burst_cap": 4,
        "burst_window_seconds": 600,
        "global_daily_cap": 25,
        "source_daily_cap": 9,
    }
    results = [pipe.deliver(DeliveryEnvelope(
        source=f"source-{index}", payload={"text": f"card-{index}"},
        metadata=metadata)) for index in range(5)]
    assert [result.state for result in results] == [
        "delivered", "delivered", "delivered", "delivered", "queued"]
    assert results[-1].reason == "burst_budget"
    assert sent == ["card-0", "card-1", "card-2", "card-3"]

    now[0] += 600.01
    assert pipe.flush_due()[0].state == "delivered"
    assert sent[-1] == "card-4"


def test_burst_budget_reservation_is_atomic_across_workers(tmp_path):
    now = [_local_ts(2026, 8, 12, 10, 0)]
    path = tmp_path / "db.sqlite"
    pipe = DeliveryPipeline(
        tmp_path, db_path=path,
        transport=lambda _envelope, _channel: TransportResult(True),
        clock=lambda: now[0], sleeper=lambda _: None,
    )
    # Initialize the schema before synchronizing worker threads. Otherwise
    # the barrier also measures SQLite's one-time DDL serialization.
    with delivery.closing(delivery._connect(path)):
        pass
    envelopes = [DeliveryEnvelope(
        source=f"burst-source-{index}",
        payload={"text": f"burst {index}"},
        metadata={
            "burst_cap": 4,
            "burst_window_seconds": 600,
            "global_daily_cap": 25,
            "source_daily_cap": 9,
            "force_queue": True,
        },
    ) for index in range(5)]
    assert [pipe.deliver(envelope).state for envelope in envelopes] == [
        "queued", "queued", "queued", "queued", "queued"]
    barrier = threading.Barrier(5)

    def reserve(envelope):
        with delivery.closing(delivery._connect(path)) as db:
            barrier.wait(timeout=5)
            reason, _retry_epoch = pipe._reserve_attempt_cap(
                db, envelope, now[0])
            return reason

    with ThreadPoolExecutor(max_workers=5) as executor:
        outcomes = list(executor.map(reserve, envelopes))

    assert outcomes.count("") == 4
    assert outcomes.count("burst_budget") == 1
    with delivery.closing(delivery._connect(path)) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM delivery_cap_reservations"
        ).fetchone()[0] == 4


def test_burst_overflow_rechecks_when_inflight_reservations_release(tmp_path):
    started = _local_ts(2026, 8, 12, 10, 0)
    now = [started]
    path = tmp_path / "db.sqlite"
    pipe = DeliveryPipeline(
        tmp_path, db_path=path,
        transport=lambda _envelope, _channel: TransportResult(True),
        clock=lambda: now[0], sleeper=lambda _: None,
    )
    limits = {
        "burst_cap": 4,
        "burst_window_seconds": 600,
        "global_daily_cap": 25,
        "source_daily_cap": 9,
        "force_queue": True,
    }
    envelopes = [DeliveryEnvelope(
        source=f"source-{index}", payload={"text": f"item {index}"},
        metadata=limits,
    ) for index in range(5)]
    for envelope in envelopes:
        assert pipe.deliver(envelope).state == "queued"

    with delivery.closing(delivery._connect(path)) as db:
        for envelope in envelopes[:4]:
            assert pipe._reserve_attempt_cap(db, envelope, started)[0] == ""

        now[0] = started + 30
        reason, retry_epoch = pipe._reserve_attempt_cap(
            db, envelopes[4], now[0])
        assert reason == "burst_budget"
        assert retry_epoch <= now[0] + delivery.CAP_RESERVATION_RECHECK_SECONDS + 0.001

        for envelope in envelopes[:4]:
            pipe._release_attempt_cap(db, envelope.id)
        db.commit()
        assert pipe._reserve_attempt_cap(
            db, envelopes[4], retry_epoch)[0] == ""


def test_exempt_inflight_reservations_do_not_consume_ordinary_budget(tmp_path):
    now = [_local_ts(2026, 8, 12, 10, 0)]
    path = tmp_path / "db.sqlite"
    pipe = DeliveryPipeline(
        tmp_path, db_path=path,
        transport=lambda _envelope, _channel: TransportResult(True),
        clock=lambda: now[0], sleeper=lambda _: None,
    )
    limits = {
        "burst_cap": 4,
        "burst_window_seconds": 600,
        "global_daily_cap": 4,
        "source_daily_cap": 9,
        "force_queue": True,
    }
    alerts = [DeliveryEnvelope(
        source=f"alert-{index}", attention="alert",
        payload={"text": f"alert {index}"}, metadata=limits,
    ) for index in range(4)]
    ordinary = DeliveryEnvelope(
        source="ordinary", payload={"text": "ordinary"}, metadata=limits)
    for envelope in [*alerts, ordinary]:
        assert pipe.deliver(envelope).state == "queued"

    with delivery.closing(delivery._connect(path)) as db:
        for alert in alerts:
            assert pipe._reserve_attempt_cap(db, alert, now[0])[0] == ""
        assert pipe._reserve_attempt_cap(db, ordinary, now[0])[0] == ""

        rows = delivery._budgeted_reservations(db, now[0] - 600)
        assert [row["source"] for row in rows] == ["ordinary"]


def test_send_day_metric_cap_reservation_is_atomic_across_workers(tmp_path):
    now = [_local_ts(2026, 7, 22, 23, 0)]
    path = tmp_path / "db.sqlite"
    pipe = DeliveryPipeline(
        tmp_path,
        db_path=path,
        transport=lambda _envelope, _channel: TransportResult(True),
        clock=lambda: now[0],
        sleeper=lambda _seconds: None,
    )
    queued = [
        DeliveryEnvelope(
            source="signal",
            payload={"text": f"queued {index}"},
            requested_channel="lark",
            throttle_key="signal:daily",
            metadata={
                "force_queue": True,
                "metric_daily_cap": 2,
                "source_daily_cap": 9,
                "global_daily_cap": 9,
            },
        )
        for index in range(2)
    ]
    assert [pipe.deliver(item).state for item in queued] == ["queued", "queued"]

    now[0] = _local_ts(2026, 7, 23, 10, 0)
    prior = pipe.deliver(DeliveryEnvelope(
        source="signal",
        payload={"text": "already delivered today"},
        requested_channel="lark",
        throttle_key="signal:daily",
        metadata={
            "metric_daily_cap": 2,
            "source_daily_cap": 9,
            "global_daily_cap": 9,
            "bypass_quiet": True,
        },
    ))
    assert prior.state == "delivered"

    ready = threading.Barrier(2)

    def reserve(envelope):
        with delivery.closing(delivery._connect(path)) as db:
            ready.wait(timeout=5)
            reason, _retry_epoch = pipe._reserve_attempt_cap(
                db, envelope, now[0])
            return reason

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(reserve, queued))

    assert sorted(outcomes) == ["", "metric_daily_cap"]
    with delivery.closing(delivery._connect(path)) as db:
        reservations = db.execute(
            "SELECT delivery_id FROM delivery_cap_reservations"
        ).fetchall()
    assert len(reservations) == 1
    assert reservations[0]["delivery_id"] in {
        queued[0].id,
        queued[1].id,
    }


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


def test_blocked_payloads_keep_distinct_audit_hashes(pipeline):
    pipe, sent, _ = pipeline
    first = pipe.deliver(DeliveryEnvelope(
        source="heartbeat",
        payload={"text": "You've hit your monthly spend limit"},
        requested_channel="lark",
    ))
    second = pipe.deliver(DeliveryEnvelope(
        source="heartbeat",
        payload={"text": "Prompt is too long"},
        requested_channel="lark",
    ))

    assert first.state == second.state == "suppressed"
    assert first.delivery_id != second.delivery_id
    assert pipe.get(first.delivery_id)["content_hash"]
    assert (
        pipe.get(first.delivery_id)["content_hash"]
        != pipe.get(second.delivery_id)["content_hash"]
    )
    assert sent == []


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


def test_user_copy_removes_internal_jargon_without_changing_actions(pipeline):
    pipe, sent, _ = pipeline
    card = {
        "header": {"title": {"tag": "plain_text", "content": "匣子台账"}},
        "elements": [
            {"tag": "markdown", "content": "硬顶后留中。Closure recorded"},
            {"tag": "action", "actions": [{
                "tag": "button",
                "text": {"tag": "plain_text", "content": "查看台账"},
                "value": {
                    "action": "watchlater",
                    "title": "项目台账",
                    "result": "Closure recorded",
                },
            }]},
        ],
    }
    result = pipe.deliver(DeliveryEnvelope(
        source="heartbeat", kind="card",
        payload={"card_json": json.dumps(card, ensure_ascii=False)},
        attention="alert", requested_channel="lark",
    ))
    assert result.state == "delivered"
    delivered = json.loads(sent[0][0].payload["card_json"])
    assert delivered["header"]["title"]["content"] == "待处理事项记录"
    assert delivered["elements"][0]["content"] == (
        "最长等待后自动归档。已记下并关闭")
    button = delivered["elements"][1]["actions"][0]
    assert button["text"]["content"] == "查看记录"
    assert button["value"] == {
        "action": "watchlater",
        "title": "项目台账",
        "result": "Closure recorded",
    }


def test_user_copy_rewrites_link_label_without_changing_url(pipeline):
    pipe, sent, _ = pipeline
    destination = "https://example.com/escrow?view=台账"
    text = f"[查看 escrow 台账]({destination})，台账稍后看"

    result = pipe.deliver(DeliveryEnvelope(
        source="heartbeat", payload={"text": text},
        requested_channel="lark",
    ))

    assert result.state == "delivered"
    assert sent[0][0].payload["text"] == (
        f"[查看 待处理记录 记录]({destination})，记录稍后看")


def test_ordinary_reply_preserves_legitimate_domain_terms(pipeline):
    pipe, sent, _ = pipeline
    text = "In this contract, escrow and 台账 are the terms being discussed."
    result = pipe.deliver(DeliveryEnvelope(
        source="bot-reply", kind="reply", attention="reply",
        reply_to="om_user", payload={"text": text},
        requested_channel="lark",
    ))

    assert result.state == "delivered"
    assert sent[0][0].payload["text"] == text


def test_retry_then_delivery_records_attempts(tmp_path):
    calls = []

    def transport(_envelope, _channel):
        calls.append(1)
        return (TransportResult(False, error="no")
                if len(calls) < 3 else TransportResult(True, "om_ok"))

    pipe = DeliveryPipeline(
        tmp_path, db_path=tmp_path / "db.sqlite", transport=transport,
        clock=lambda: _local_ts(2026, 7, 23, 14, 0),
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
        clock=lambda: _local_ts(2026, 7, 23, 14, 0),
        sleeper=lambda _: None,
    )
    result = pipe.deliver(DeliveryEnvelope(
        source="test", payload={"text": "do not lose me"},
        requested_channel="lark"))
    assert result.state == "queued"
    assert pipe.get(result.delivery_id)["attempts"] == 3
    assert pipe.get(result.delivery_id)["last_error"] == "adapter crashed"
    with delivery.closing(delivery._connect(pipe.path)) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM delivery_cap_reservations"
        ).fetchone()[0] == 0


def test_retry_exhaustion_reaches_terminal_failure_and_stops(tmp_path, capsys):
    now = [_local_ts(2026, 7, 23, 14, 0)]
    calls = []
    pipe = DeliveryPipeline(
        tmp_path, db_path=tmp_path / "db.sqlite",
        transport=lambda _e, _c: (
            calls.append(1) or TransportResult(False, error="offline")),
        clock=lambda: now[0],
        sleeper=lambda _: None,
    )
    result = pipe.deliver(DeliveryEnvelope(
        source="test", payload={"text": "keep me"},
        requested_channel="lark"))
    assert result.accepted is True
    assert result.state == "queued"
    assert pipe.pending_dead_letters() == []

    now[0] += 301
    assert pipe.flush_due()[0].state == "queued"
    now[0] += 301
    terminal = pipe.flush_due()[0]
    assert terminal.state == "failed"
    terminal_event = json.loads(capsys.readouterr().err.splitlines()[-1])
    assert terminal_event["component"] == "delivery"
    assert terminal_event["msg"] == "terminal_failure"
    assert terminal_event["attempts"] == delivery.MAX_DELIVERY_ATTEMPTS
    assert "offline" not in json.dumps(terminal_event)
    assert pipe.get(result.delivery_id)["attempts"] == delivery.MAX_DELIVERY_ATTEMPTS
    assert pipe.get(result.delivery_id)["last_error"] == "offline"
    dead = pipe.pending_dead_letters()
    assert dead[0]["delivery_id"] == result.delivery_id
    pipe.mark_dead_letters_notified([dead[0]["id"]])
    assert pipe.pending_dead_letters() == []
    now[0] += 301
    assert pipe.flush_due() == []
    assert len(calls) == delivery.MAX_DELIVERY_ATTEMPTS
    assert pipe.pending_dead_letters() == []


def test_broken_log_sink_does_not_abort_terminal_delivery_state(
    tmp_path, monkeypatch,
):
    now = [_local_ts(2026, 7, 23, 14, 0)]
    monkeypatch.setattr(
        delivery, "log",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("log down")),
    )
    pipe = DeliveryPipeline(
        tmp_path, db_path=tmp_path / "db.sqlite",
        transport=lambda _e, _c: TransportResult(False, error="offline"),
        clock=lambda: now[0], sleeper=lambda _: None,
    )
    result = pipe.deliver(DeliveryEnvelope(
        source="test", payload={"text": "keep me"},
        requested_channel="lark",
    ))
    for _ in range(2):
        now[0] += 301
        terminal = pipe.flush_due()[0]

    assert result.accepted is True
    assert terminal.state == "failed"
    assert pipe.get(result.delivery_id)["attempts"] == (
        delivery.MAX_DELIVERY_ATTEMPTS
    )


def test_state_updates_reject_unknown_columns(tmp_path):
    pipe = DeliveryPipeline(
        tmp_path, db_path=tmp_path / "db.sqlite",
        transport=lambda _e, _c: TransportResult(True),
        sleeper=lambda _: None,
    )
    result = pipe.deliver(DeliveryEnvelope(
        source="test", payload={"text": "state allowlist"}))
    with delivery.closing(delivery._connect(pipe.path)) as db:
        with pytest.raises(ValueError, match="unsupported delivery fields"):
            pipe._set_state(
                db, result.delivery_id, "delivered",
                unexpected_column="not allowed",
            )


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
