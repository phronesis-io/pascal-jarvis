"""Unified delivery pipeline contract tests."""

from __future__ import annotations

import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import pytest

import core.delivery as delivery
from core import lark_bot_transport
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


def test_queued_memorial_payload_can_be_revised_without_second_envelope(
        tmp_path):
    now = [_local_ts(2026, 8, 28, 14, 0)]
    pipe = DeliveryPipeline(
        tmp_path,
        db_path=tmp_path / "jarvis.db",
        transport=lambda *_: pytest.fail("forced queue must not send"),
        clock=lambda: now[0],
    )
    old = json.dumps({
        "schema": "2.0",
        "body": {"elements": [{"tag": "markdown", "content": "旧正文"}]},
    }, ensure_ascii=False)
    result = pipe.deliver(DeliveryEnvelope(
        source="eigenflux",
        kind="card",
        payload={"card_json": old, "text": "旧正文"},
        memorial_id="mem_hour",
        metadata={"force_queue": True},
    ))
    assert result.state == "queued"

    new = json.dumps({
        "schema": "2.0",
        "body": {"elements": [{"tag": "markdown", "content": "合并后的正文"}]},
    }, ensure_ascii=False)
    assert pipe.replace_memorial_payload(
        "mem_hour", card_json=new, text="合并后的正文"
    ) == [result.delivery_id]
    row = pipe.get(result.delivery_id)
    assert json.loads(row["payload"])["text"] == "合并后的正文"
    assert len(pipe.list_source("eigenflux")) == 1


def test_alert_without_explicit_key_gets_stable_incident_identity(pipeline):
    pipe, sent, _ = pipeline
    first = pipe.deliver(DeliveryEnvelope(
        source="guardian-daemon", payload={"text": "组件失联"},
        attention="alert",
    ))
    second = pipe.deliver(DeliveryEnvelope(
        source="guardian-daemon", payload={"text": "组件失联"},
        attention="alert",
    ))

    row = pipe.get(first.delivery_id)
    assert row["dedup_key"].startswith("alert:guardian-daemon:")
    assert second.delivery_id == first.delivery_id
    assert second.reason == "duplicate"
    assert len(sent) == 1


def test_explicit_dedup_window_allows_a_later_incident_recurrence(tmp_path):
    now = [_local_ts(2026, 8, 20, 10, 0)]
    sent = []
    pipe = DeliveryPipeline(
        tmp_path,
        db_path=tmp_path / "jarvis.db",
        transport=lambda envelope, _channel: (
            sent.append(envelope.id) or TransportResult(True, f"om_{len(sent)}")
        ),
        clock=lambda: now[0],
        sleeper=lambda _seconds: None,
    )

    def envelope():
        return DeliveryEnvelope(
            source="guardian-daemon",
            payload={"text": "系统已经恢复"},
            attention="alert",
            dedup_key="guardian:bot-restart-recovered",
            metadata={"dedup_window_seconds": 24 * 3600},
        )

    first = pipe.deliver(envelope())
    now[0] += 23 * 3600
    duplicate = pipe.deliver(envelope())
    now[0] += 2 * 3600
    recurrence = pipe.deliver(envelope())

    assert duplicate.delivery_id == first.delivery_id
    assert duplicate.reason == "duplicate"
    assert recurrence.delivery_id != first.delivery_id
    assert recurrence.state == "delivered"
    assert len(sent) == 2


def test_verified_transport_recovery_replays_valid_terminal_failure(tmp_path):
    now = [_local_ts(2026, 8, 17, 10, 0)]
    healthy = [False]
    sent = []

    def transport(envelope, _channel):
        sent.append(envelope.id)
        return TransportResult(
            healthy[0],
            f"om_{len(sent)}" if healthy[0] else "",
            "transport unavailable" if not healthy[0] else "",
        )

    pipe = DeliveryPipeline(
        tmp_path, db_path=tmp_path / "jarvis.db", transport=transport,
        clock=lambda: now[0], sleeper=lambda _seconds: None,
    )
    original = pipe.deliver(DeliveryEnvelope(
        source="eigenflux", payload={"text": "仍然有效的研究结论"},
        attention="notice", dedup_key="signal:42",
    ))
    for _ in range(2):
        now[0] += 301
        pipe.flush_due()
    assert pipe.get(original.delivery_id)["state"] == "failed"
    assert len(pipe.pending_dead_letters()) == 1

    healthy[0] = True
    now[0] += 1
    recovery = pipe.deliver(DeliveryEnvelope(
        source="transport-probe", payload={"text": "恢复确认"},
        metadata={"bypass_throttle": True},
    ))

    assert recovery.state == "delivered"
    assert pipe.get(original.delivery_id)["state"] == "queued"
    assert pipe.get(original.delivery_id)["attempts"] == 0
    assert pipe.pending_dead_letters() == []
    replay = pipe.flush_due()
    assert any(row.delivery_id == original.delivery_id
               and row.state == "delivered" for row in replay)


def test_recovery_suppresses_stale_alert_instead_of_replaying(tmp_path):
    now = [_local_ts(2026, 8, 17, 10, 0)]
    healthy = [False]
    sent = []

    def transport(envelope, _channel):
        sent.append(envelope.id)
        return TransportResult(healthy[0], error="down" if not healthy[0] else "")

    pipe = DeliveryPipeline(
        tmp_path, db_path=tmp_path / "jarvis.db", transport=transport,
        clock=lambda: now[0], sleeper=lambda _seconds: None,
    )
    alert = pipe.deliver(DeliveryEnvelope(
        source="selfmon", payload={"text": "十分钟前的异常"},
        attention="alert",
    ))
    for _ in range(2):
        now[0] += 301
        pipe.flush_due()
    assert pipe.get(alert.delivery_id)["state"] == "failed"

    healthy[0] = True
    now[0] += 30 * 60 + 1
    pipe.deliver(DeliveryEnvelope(
        source="transport-probe", payload={"text": "恢复确认"},
        metadata={"bypass_throttle": True},
    ))

    assert pipe.get(alert.delivery_id)["state"] == "suppressed"
    assert pipe.get(alert.delivery_id)["last_error"] == \
        "recovery_replay_expired"


@pytest.mark.parametrize(
    "source",
    [
        "calendar-sync",
        "checkin",
        "guardian-daemon",
        "intention-check",
        "morning-anchor",
        "routine:午间活动",
    ],
)
def test_recovery_never_replays_regenerated_ephemeral_work(tmp_path, source):
    now = [_local_ts(2026, 8, 17, 10, 0)]
    healthy = [False]
    pipe = DeliveryPipeline(
        tmp_path,
        db_path=tmp_path / "jarvis.db",
        transport=lambda _envelope, _channel: TransportResult(
            healthy[0], error="down" if not healthy[0] else ""
        ),
        clock=lambda: now[0],
        sleeper=lambda _seconds: None,
    )
    failed = pipe.deliver(DeliveryEnvelope(
        source=source,
        payload={"text": "会由下一轮重新计算的内容"},
        attention="notice",
    ))
    for _ in range(2):
        now[0] += 301
        pipe.flush_due()
    assert pipe.get(failed.delivery_id)["state"] == "failed"

    healthy[0] = True
    now[0] += 1
    pipe.deliver(DeliveryEnvelope(
        source="transport-probe",
        payload={"text": "恢复确认"},
        metadata={"bypass_throttle": True},
    ))

    row = pipe.get(failed.delivery_id)
    assert row["state"] == "suppressed"
    assert row["last_error"] == "recovery_incident_obsolete"


@pytest.mark.parametrize(
    ("terminal_event", "expected_state"),
    [(None, "queued"), ("decide", "suppressed")],
)
def test_recovery_reads_memorial_lifecycle_without_high_level_facade(
    tmp_path, terminal_event, expected_state,
):
    now = [_local_ts(2026, 8, 17, 10, 0)]
    healthy = [False]
    memorial_id = "mem_recovery_contract"
    events = [{
        "ev": "create",
        "id": memorial_id,
        "source": "eigenflux",
        "title": "仍然有效的待处理项",
        "body": "正文",
        "epoch": now[0],
    }]
    if terminal_event:
        events.append({
            "ev": terminal_event,
            "id": memorial_id,
            "opt": "read",
            "label": "已阅",
        })
    (tmp_path / "memorials.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in events),
        encoding="utf-8",
    )
    pipe = DeliveryPipeline(
        tmp_path,
        db_path=tmp_path / "jarvis.db",
        transport=lambda _envelope, _channel: TransportResult(
            healthy[0], error="down" if not healthy[0] else ""
        ),
        clock=lambda: now[0],
        sleeper=lambda _seconds: None,
    )
    failed = pipe.deliver(DeliveryEnvelope(
        source="eigenflux",
        payload={"text": "待处理项"},
        attention="decision",
        memorial_id=memorial_id,
        dedup_key="memorial:recovery-contract",
    ))
    for _ in range(2):
        now[0] += 301
        pipe.flush_due()

    healthy[0] = True
    now[0] += 1
    pipe.deliver(DeliveryEnvelope(
        source="transport-probe",
        payload={"text": "恢复确认"},
        metadata={"bypass_throttle": True},
    ))

    assert pipe.get(failed.delivery_id)["state"] == expected_state
    if terminal_event:
        assert pipe.get(failed.delivery_id)["last_error"] == \
            "recovery_item_resolved"


def test_recovery_scans_past_large_obsolete_prefix_for_valid_work(tmp_path):
    now = [_local_ts(2026, 8, 17, 10, 0)]
    pipe = DeliveryPipeline(
        tmp_path,
        db_path=tmp_path / "jarvis.db",
        transport=lambda _envelope, _channel: TransportResult(True, "om_ok"),
        clock=lambda: now[0],
        sleeper=lambda _seconds: None,
    )
    obsolete_ids = []
    for index in range(201):
        result = pipe.deliver(DeliveryEnvelope(
            source="guardian-daemon",
            payload={"text": f"旧告警 {index}"},
            attention="decision",
            metadata={
                "force_queue": True,
                "bypass_dedup": True,
                "bypass_throttle": True,
            },
        ))
        obsolete_ids.append(result.delivery_id)
    valid = pipe.deliver(DeliveryEnvelope(
        source="eigenflux",
        payload={"text": "仍然有效的待处理研究"},
        attention="notice",
        dedup_key="eigenflux:valid-after-obsolete-prefix",
        metadata={"force_queue": True, "bypass_throttle": True},
    ))
    with delivery.closing(delivery._connect(pipe.path)) as db, db:
        db.execute(
            "UPDATE delivery_envelopes SET state='failed',attempts=?",
            (delivery.MAX_DELIVERY_ATTEMPTS,),
        )

    now[0] += 1
    requeued = pipe.reconcile_failed_after_recovery(
        limit=20, recovery_epoch=now[0],
    )

    assert valid.delivery_id in requeued
    assert pipe.get(valid.delivery_id)["state"] == "queued"
    assert all(pipe.get(delivery_id)["state"] == "suppressed"
               for delivery_id in obsolete_ids)


def test_recovery_replay_defers_at_cap_and_delivers_next_window(tmp_path):
    now = [_local_ts(2026, 8, 17, 20, 0)]
    sent = []
    pipe = DeliveryPipeline(
        tmp_path,
        db_path=tmp_path / "jarvis.db",
        transport=lambda envelope, _channel: (
            sent.append(envelope.id) or TransportResult(True, "om_ok")
        ),
        clock=lambda: now[0],
        sleeper=lambda _seconds: None,
    )
    filler = pipe.deliver(DeliveryEnvelope(
        source="ordinary",
        payload={"text": "今天的预算占位"},
        metadata={"global_daily_cap": 1},
    ))
    assert filler.state == "delivered"
    replay = pipe.deliver(DeliveryEnvelope(
        source="eigenflux",
        payload={"text": "仍有效但不应突破预算"},
        attention="notice",
        metadata={
            "force_queue": True,
            "global_daily_cap": 1,
            "replay_count": 1,
            "recovery_requeued_epoch": now[0] - 60,
            "defer_on_cap": True,
            "expires_epoch": now[0] + 48 * 3600,
        },
    ))
    with delivery.closing(delivery._connect(pipe.path)) as db, db:
        pipe._set_state(
            db, replay.delivery_id, "queued", "recovery due now",
            next_attempt_epoch=now[0], last_error="",
        )

    result = next(row for row in pipe.flush_due()
                  if row.delivery_id == replay.delivery_id)

    assert result.state == "queued"
    assert result.reason == "global_daily_cap"
    row = pipe.get(replay.delivery_id)
    assert row["state"] == "queued"
    assert row["next_attempt_epoch"] == _local_ts(2026, 8, 18, 0, 0, 1)
    assert sent == [filler.delivery_id]

    now[0] = _local_ts(2026, 8, 18, 9, 30)
    delivered = pipe.flush_due()
    assert any(item.delivery_id == replay.delivery_id
               and item.state == "delivered" for item in delivered)


def test_recovery_reconciles_rows_already_suppressed_by_cap(tmp_path):
    now = [_local_ts(2026, 8, 17, 20, 0)]
    pipe = DeliveryPipeline(
        tmp_path,
        db_path=tmp_path / "jarvis.db",
        transport=lambda _envelope, _channel: TransportResult(True, "om_ok"),
        clock=lambda: now[0],
        sleeper=lambda _seconds: None,
    )
    replay = pipe.deliver(DeliveryEnvelope(
        source="eigenflux",
        payload={"text": "升级前被预算终止的恢复项"},
        attention="notice",
        metadata={
            "force_queue": True,
            "replay_count": 1,
            "recovery_requeued_epoch": now[0] - 60,
        },
    ))
    with delivery.closing(delivery._connect(pipe.path)) as db, db:
        pipe._set_state(
            db,
            replay.delivery_id,
            "suppressed",
            "global_daily_cap",
            next_attempt_epoch=None,
            last_error="global_daily_cap",
        )

    now[0] += 1
    assert pipe.reconcile_failed_after_recovery(
        recovery_epoch=now[0],
    ) == [replay.delivery_id]

    row = pipe.get(replay.delivery_id)
    metadata = json.loads(row["metadata"])
    assert row["state"] == "queued"
    assert row["attempts"] == 0
    assert metadata["replay_count"] == 1
    assert metadata["defer_on_cap"] is True
    assert metadata["expires_epoch"] == (
        row["created_epoch"] + 24 * 3600
    )


def test_recovery_does_not_migrate_unreceipted_cap_suppression(tmp_path):
    now = [_local_ts(2026, 8, 17, 20, 0)]
    pipe = DeliveryPipeline(
        tmp_path,
        db_path=tmp_path / "jarvis.db",
        transport=lambda _envelope, _channel: TransportResult(True, "om_ok"),
        clock=lambda: now[0],
        sleeper=lambda _seconds: None,
    )
    ordinary = pipe.deliver(DeliveryEnvelope(
        source="ordinary",
        payload={"text": "不是恢复流程产生的历史行"},
        attention="notice",
        metadata={"force_queue": True, "replay_count": 1},
    ))
    with delivery.closing(delivery._connect(pipe.path)) as db, db:
        pipe._set_state(
            db,
            ordinary.delivery_id,
            "suppressed",
            "global_daily_cap",
            next_attempt_epoch=None,
            last_error="global_daily_cap",
        )

    now[0] += 1
    assert pipe.reconcile_failed_after_recovery(
        recovery_epoch=now[0],
    ) == []
    assert pipe.get(ordinary.delivery_id)["state"] == "suppressed"


def test_unreceipted_defer_marker_cannot_change_cap_policy(tmp_path):
    now = [_local_ts(2026, 8, 17, 20, 0)]
    pipe = DeliveryPipeline(
        tmp_path,
        db_path=tmp_path / "jarvis.db",
        transport=lambda _envelope, _channel: TransportResult(True, "om_ok"),
        clock=lambda: now[0],
        sleeper=lambda _seconds: None,
    )
    assert pipe.deliver(DeliveryEnvelope(
        source="ordinary",
        payload={"text": "预算占位"},
        metadata={"global_daily_cap": 1},
    )).state == "delivered"
    fake_recovery = pipe.deliver(DeliveryEnvelope(
        source="ordinary",
        payload={"text": "只有布尔标记，没有恢复收据"},
        metadata={
            "global_daily_cap": 1,
            "replay_count": 1,
            "defer_on_cap": True,
        },
    ))

    assert fake_recovery.state == "suppressed"
    assert fake_recovery.reason == "global_daily_cap"


def test_recovery_replay_expires_while_waiting_for_budget(tmp_path):
    now = [_local_ts(2026, 8, 17, 20, 0)]
    sent = []
    pipe = DeliveryPipeline(
        tmp_path,
        db_path=tmp_path / "jarvis.db",
        transport=lambda envelope, _channel: (
            sent.append(envelope.id) or TransportResult(True, "om_ok")
        ),
        clock=lambda: now[0],
        sleeper=lambda _seconds: None,
    )
    replay = pipe.deliver(DeliveryEnvelope(
        source="eigenflux",
        payload={"text": "等待时已经过期"},
        attention="notice",
        metadata={
            "force_queue": True,
            "replay_count": 1,
            "defer_on_cap": True,
            "expires_epoch": now[0] + 10,
        },
    ))
    with delivery.closing(delivery._connect(pipe.path)) as db, db:
        pipe._set_state(
            db, replay.delivery_id, "queued", "recovery due now",
            next_attempt_epoch=now[0], last_error="",
        )

    now[0] += 11
    result = next(row for row in pipe.flush_due()
                  if row.delivery_id == replay.delivery_id)

    assert result.state == "suppressed"
    assert result.reason == "expired_ttl"
    assert sent == []


def test_transport_health_opens_after_three_attempt_failures_and_recovers(
    tmp_path,
):
    now = [_local_ts(2026, 8, 17, 10, 0)]
    healthy = [False]

    pipe = DeliveryPipeline(
        tmp_path,
        db_path=tmp_path / "jarvis.db",
        transport=lambda _envelope, _channel: TransportResult(
            healthy[0], error="down" if not healthy[0] else ""
        ),
        clock=lambda: now[0],
        sleeper=lambda _seconds: None,
    )
    pipe.deliver(DeliveryEnvelope(
        source="selfmon", payload={"text": "transport check"},
        attention="alert",
    ))
    assert pipe.transport_health() == {
        "healthy": False,
        "consecutive_failures": 3,
        "last_success_epoch": 0.0,
        "last_failure_epoch": now[0],
    }

    healthy[0] = True
    now[0] += 1
    pipe.deliver(DeliveryEnvelope(
        source="transport-probe", payload={"text": "recovered"},
        metadata={"bypass_throttle": True},
    ))
    status = pipe.transport_health()
    assert status["healthy"] is True
    assert status["consecutive_failures"] == 0
    assert status["last_success_epoch"] == now[0]


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


def test_suppress_queued_source_is_transactional_and_audited(pipeline):
    pipe, sent, _ = pipeline
    for index, source in enumerate(
            ("cross-session-sync", "cross-session-sync", "mail")):
        result = pipe.deliver(DeliveryEnvelope(
            source=source,
            payload={"text": f"queued {source} {index}"},
            attention="decision",
        ))
        assert result.state == "delivered"
    with delivery.closing(delivery._connect(pipe.path)) as db, db:
        db.execute(
            "UPDATE delivery_envelopes SET state='queued', "
            "next_attempt_epoch=9999999999 WHERE source='cross-session-sync'"
        )

    suppressed = pipe.suppress_queued_source(
        "cross-session-sync", reason="ambient_ledger_only")

    assert len(suppressed) == 2
    assert all(pipe.get(delivery_id)["state"] == "suppressed"
               for delivery_id in suppressed)
    assert pipe.list(state="queued") == []
    assert pipe.get(next(row["id"] for row in pipe.list()
                         if row["source"] == "mail"))["state"] == "delivered"
    rows = pipe.list_source(
        "cross-session-sync", state="suppressed",
        last_error="ambient_ledger_only")
    assert [row["id"] for row in rows] == suppressed


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
    assert result.state == "suppressed"
    assert result.reason == "global_daily_cap"
    assert len(sent) == 1


def test_absence_receipt_is_not_eaten_by_a_spent_budget(pipeline):
    """The card that explains a missing day cannot be dropped for being late.

    2026-08-19: 39h of backlog woke up at once and spent all nine budgeted
    slots between 13:03 and 13:26. The first thing the full budget dropped was
    the host-absence receipt — the one card that said why the others were
    missing. It is system-generated and bounded to one per episode, so it
    rides with deploy-smoke outside the attention budget.
    """
    pipe, sent, _ = pipeline
    for index in range(2):
        pipe.deliver(DeliveryEnvelope(
            source=f"s{index}", payload={"text": f"message {index}"},
            requested_channel="lark",
            metadata={"global_daily_cap": 1, "source_daily_cap": 9},
        ))
    assert len(sent) == 1  # budget spent

    result = pipe.deliver(DeliveryEnvelope(
        source="host-absence", attention="notice",
        payload={"text": "我离线了 1 天 15 小时"},
        requested_channel="lark",
        metadata={"global_daily_cap": 1, "source_daily_cap": 9},
    ))

    assert result.state == "delivered"
    assert len(sent) == 2


def test_absence_receipt_does_not_consume_the_budget_either(pipeline):
    pipe, sent, _ = pipeline
    pipe.deliver(DeliveryEnvelope(
        source="host-absence", attention="notice",
        payload={"text": "我离线了 1 天 15 小时"},
        requested_channel="lark",
        metadata={"global_daily_cap": 1, "source_daily_cap": 9},
    ))
    ordinary = pipe.deliver(DeliveryEnvelope(
        source="checkin", payload={"text": "ordinary card"},
        requested_channel="lark",
        metadata={"global_daily_cap": 1, "source_daily_cap": 9},
    ))

    assert ordinary.state == "delivered"
    assert len(sent) == 2


def test_evening_anchor_keeps_one_slot_of_a_spent_budget(pipeline):
    """daily-reflect must not lose to the cards that merely fired earlier.

    2026-08-14..22 prod: nine budgeted cards were gone by ~13:00 every day and
    the 20:55 daily-reflect — the two-way check-in Pascal asked for on
    2026-06-20, acted 4/5 days while it still reached him — was suppressed
    with global_daily_cap on every one of those nights. Ordinary cards see a
    budget of cap-1 until the anchor has sent; the anchor sees the full cap.
    """
    pipe, sent, _ = pipeline
    meta = {"global_daily_cap": 3, "source_daily_cap": 9}
    states = [
        pipe.deliver(DeliveryEnvelope(
            source=f"s{index}", payload={"text": f"message {index}"},
            requested_channel="lark", metadata=dict(meta),
        )).state
        for index in range(3)
    ]
    assert states == ["delivered", "suppressed", "suppressed"]
    assert len(sent) == 1

    decision = pipe.deliver(DeliveryEnvelope(
        source="intention-check", attention="decision",
        payload={"text": "需要你拍板"},
        requested_channel="lark", metadata=dict(meta),
    ))
    assert decision.state == "delivered"
    assert len(sent) == 2

    reflect = pipe.deliver(DeliveryEnvelope(
        source="daily-reflect", attention="notice",
        payload={"text": "今天怎么样？"},
        requested_channel="lark", metadata=dict(meta),
    ))
    assert reflect.state == "delivered"
    assert len(sent) == 3

    # The reservation is released once the anchor has sent: the day still
    # totals exactly the cap, never cap+1.
    late = pipe.deliver(DeliveryEnvelope(
        source="s9", payload={"text": "late card"},
        requested_channel="lark", metadata=dict(meta),
    ))
    assert late.state == "suppressed"
    assert late.reason == "global_daily_cap"
    assert len(sent) == 3


def test_second_decision_over_daily_cap_waits_for_next_send_day(tmp_path):
    now = [_local_ts(2026, 8, 12, 14, 0)]
    sent = []
    pipe = DeliveryPipeline(
        tmp_path, db_path=tmp_path / "db.sqlite",
        transport=lambda envelope, _channel: (
            sent.append(envelope.payload["text"])
            or TransportResult(True, f"om_{len(sent)}")),
        clock=lambda: now[0], sleeper=lambda _: None,
    )
    meta = {"global_daily_cap": 2, "source_daily_cap": 9}
    first = pipe.deliver(DeliveryEnvelope(
        source="decision-a", attention="decision",
        payload={"text": "first decision"}, metadata=dict(meta)))
    reflect = pipe.deliver(DeliveryEnvelope(
        source="daily-reflect", attention="notice",
        payload={"text": "evening anchor"}, metadata=dict(meta)))
    second = pipe.deliver(DeliveryEnvelope(
        source="decision-b", attention="decision",
        payload={"text": "second decision"}, metadata=dict(meta)))

    assert [first.state, reflect.state, second.state] == [
        "delivered", "delivered", "queued"]
    assert second.reason == "global_daily_cap"
    row = pipe.get(second.delivery_id)
    assert row["next_attempt_epoch"] > now[0]

    now[0] = _local_ts(2026, 8, 13, 10, 1)
    replay = pipe.flush_due()
    assert replay[-1].state == "delivered"
    assert sent[-1] == "second decision"


def test_decision_over_source_cap_waits_for_next_send_day(tmp_path):
    """Same-source decision bursts must not become terminal ledger-only rows."""
    now = [_local_ts(2026, 8, 12, 14, 0)]
    sent = []
    pipe = DeliveryPipeline(
        tmp_path, db_path=tmp_path / "db.sqlite",
        transport=lambda envelope, _channel: (
            sent.append(envelope.payload["text"])
            or TransportResult(True, f"om_{len(sent)}")),
        clock=lambda: now[0], sleeper=lambda _: None,
    )
    meta = {"global_daily_cap": 9, "source_daily_cap": 1}
    first = pipe.deliver(DeliveryEnvelope(
        source="intention-check", attention="decision",
        payload={"text": "first decision"}, metadata=dict(meta)))
    second = pipe.deliver(DeliveryEnvelope(
        source="intention-check", attention="decision",
        payload={"text": "second decision"}, metadata=dict(meta)))

    assert first.state == "delivered"
    assert second.state == "queued"
    assert second.reason == "source_daily_cap"
    assert pipe.get(second.delivery_id)["next_attempt_epoch"] > now[0]

    now[0] = _local_ts(2026, 8, 13, 10, 1)
    replay = pipe.flush_due()
    assert replay[-1].state == "delivered"
    assert sent == ["first decision", "second decision"]


def test_evening_anchor_reservation_is_released_after_it_sends(pipeline):
    pipe, sent, _ = pipeline
    meta = {"global_daily_cap": 3, "source_daily_cap": 9}
    pipe.deliver(DeliveryEnvelope(
        source="daily-reflect", payload={"text": "今天怎么样？"},
        requested_channel="lark", metadata=dict(meta),
    ))
    states = [
        pipe.deliver(DeliveryEnvelope(
            source=f"s{index}", payload={"text": f"message {index}"},
            requested_channel="lark", metadata=dict(meta),
        )).state
        for index in range(3)
    ]
    assert states == ["delivered", "suppressed", "suppressed"]
    assert len(sent) == 2


def test_evening_anchor_reservation_never_empties_a_tiny_budget(pipeline):
    pipe, sent, _ = pipeline
    result = pipe.deliver(DeliveryEnvelope(
        source="checkin", payload={"text": "only card"},
        requested_channel="lark",
        metadata={"global_daily_cap": 1, "source_daily_cap": 9},
    ))
    assert result.state == "delivered"
    assert len(sent) == 1


def test_global_daily_budget_does_not_create_a_next_morning_backlog(tmp_path):
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
    overflow = pipe.deliver(DeliveryEnvelope(
        source="two", payload={"text": "second"},
        metadata=metadata))
    assert overflow.state == "suppressed"
    assert overflow.reason == "global_daily_cap"
    assert pipe.get(overflow.delivery_id)["next_attempt_epoch"] is None

    now[0] = _local_ts(2026, 8, 13, 9, 31)
    assert pipe.flush_due() == []
    assert sent == ["first"]


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
    metadata = {
        "global_daily_cap": 1,
        "source_daily_cap": 1,
        "metric_daily_cap": 1,
    }
    pipe.deliver(DeliveryEnvelope(
        source="ordinary", payload={"text": "normal"},
        throttle_key="same-signal", metadata=metadata))
    alert = pipe.deliver(DeliveryEnvelope(
        source="ordinary", payload={"text": "urgent alert"},
        attention="alert", throttle_key="same-signal", metadata=metadata))
    reply = pipe.deliver(DeliveryEnvelope(
        source="ordinary", kind="reply", attention="reply",
        throttle_key="same-signal", reply_to="om_user",
        payload={"text": "reply"}, metadata=metadata))
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


def test_default_transport_prefers_bot_api_and_keeps_real_receipt(
    tmp_path, monkeypatch,
):
    calls = []
    monkeypatch.setattr(
        lark_bot_transport,
        "send",
        lambda **kwargs: (
            calls.append(kwargs)
            or lark_bot_transport.BotSendResult(True, True, "om_direct")
        ),
    )
    monkeypatch.setattr(
        delivery.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("lark-cli must not run when bot API is configured")
        ),
    )

    result = delivery._default_transport(tmp_path)(
        DeliveryEnvelope(
            source="test", payload={"text": "hello"}, chat_id="oc_chat",
            id="dlv_test",
        ),
        "lark",
    )

    assert result == TransportResult(True, "om_direct", "")
    assert calls == [{
        "card_json": "",
        "text": "hello",
        "chat_id": "oc_chat",
        "user_id": "",
        "idempotency_key": "dlv_test",
        "root": tmp_path,
    }]


def test_default_transport_bot_api_failure_is_not_fabricated(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(
        lark_bot_transport,
        "send",
        lambda **_kwargs: lark_bot_transport.BotSendResult(
            True, False, error="message_receipt_missing"
        ),
    )

    result = delivery._default_transport(tmp_path)(
        DeliveryEnvelope(
            source="reply", kind="reply", payload={"text": "answer"},
            reply_to="om_incoming",
        ),
        "lark_reply",
    )

    assert result.ok is False
    assert result.message_id == ""
    assert result.error == "message_receipt_missing"
