"""Regression contract for visible, bounded Jarvis proactivity."""

from __future__ import annotations

import json
import sys
import types
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from core.delivery import DeliveryResult
from core.timeutil import now_local


def _signal(**overrides) -> dict:
    state = {
        "id": "mem_signal",
        "source": "eigenflux-feed-triage",
        "title": "EigenFlux 新信号",
        "body": "一条值得查看的网络更新",
        "attention": "notice",
        "status": "pending",
        "epoch": now_local().timestamp(),
    }
    state.update(overrides)
    return state


@pytest.fixture
def intent_db(tmp_path, monkeypatch):
    import core.intentions as intentions
    import dashboard.db as db_module

    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "intentions.db")
    if db_module._connection is not None:
        db_module._connection.close()
    db_module._connection = None
    intentions._table_ready = False
    yield db_module
    if db_module._connection is not None:
        db_module._connection.close()
    db_module._connection = None
    intentions._table_ready = False


def test_proactive_signal_skips_cleanly_without_paired_phone():
    from core.proactive import maybe_push_signal

    delivered = []
    result = maybe_push_signal(
        _signal(),
        subscription_checker=lambda: False,
        deliverer=lambda *_args, **_kwargs: delivered.append(True),
    )

    assert result["eligible"] is True
    assert result["accepted"] is False
    assert result["reason"] == "no_paired_phone_subscription"
    assert delivered == []


def test_proactive_signal_uses_bounded_push_delivery():
    from core.proactive import maybe_push_signal

    captured = []

    def deliverer(envelope, **_kwargs):
        captured.append(envelope)
        return DeliveryResult(
            envelope.id, True, "delivered", "push", reason="")

    result = maybe_push_signal(
        _signal(),
        subscription_checker=lambda: True,
        deliverer=deliverer,
    )

    assert result["accepted"] is True
    envelope = captured[0]
    assert envelope.kind == "push"
    assert envelope.requested_channel == "push"
    assert envelope.throttle_key == "proactive:eigenflux"
    assert envelope.metadata["metric_daily_cap"] == 2
    assert envelope.metadata["paired_only"] is True
    assert envelope.metadata["optional_no_subscriber"] is True
    assert envelope.payload["url"] == "/items/mem_signal"


def test_proactive_signal_does_not_push_unselected_sources():
    from core.proactive import maybe_push_signal

    result = maybe_push_signal(
        _signal(source="cross-session-sync"),
        subscription_checker=lambda: True,
        deliverer=lambda *_args, **_kwargs: pytest.fail(
            "unselected sources must stay web-only"),
    )
    assert result == {
        "eligible": False,
        "accepted": False,
        "reason": "source_not_selected",
    }


def test_proactive_signal_daily_cap_is_enforced_by_delivery(tmp_path):
    from core.delivery import DeliveryPipeline, TransportResult
    from core.proactive import maybe_push_signal

    sent = []
    pipe = DeliveryPipeline(
        tmp_path,
        db_path=tmp_path / "delivery.db",
        transport=lambda envelope, channel: (
            sent.append((envelope.memorial_id, channel))
            or TransportResult(True)
        ),
        clock=lambda: now_local().replace(
            hour=14, minute=0, second=0, microsecond=0).timestamp(),
        sleeper=lambda _seconds: None,
    )

    results = [
        maybe_push_signal(
            _signal(
                id=f"mem_{index}",
                title=f"signal {index}",
                body=f"body {index}",
            ),
            subscription_checker=lambda: True,
            deliverer=lambda envelope, **_kwargs: pipe.deliver(envelope),
        )
        for index in range(3)
    ]

    assert [result["accepted"] for result in results] == [True, True, False]
    assert results[2]["reason"] == "metric_daily_cap"
    assert sent == [("mem_0", "push"), ("mem_1", "push")]


def test_proactive_signal_cap_is_rechecked_on_actual_send_day(
        tmp_path, monkeypatch):
    from core.delivery import DeliveryPipeline, TransportResult
    from core.proactive import maybe_push_signal

    monkeypatch.setenv("JARVIS_QUIET_START", "23:30")
    monkeypatch.setenv("JARVIS_QUIET_END", "10:00")
    clock = [now_local().replace(
        hour=23, minute=45, second=0, microsecond=0).timestamp()]
    sent = []
    pipe = DeliveryPipeline(
        tmp_path,
        db_path=tmp_path / "delivery.db",
        transport=lambda envelope, _channel: (
            sent.append(envelope.memorial_id) or TransportResult(True)
        ),
        clock=lambda: clock[0],
        sleeper=lambda _seconds: None,
    )

    queued = [
        maybe_push_signal(
            _signal(id=f"night_{index}", title=f"night {index}"),
            subscription_checker=lambda: True,
            deliverer=lambda envelope, **_kwargs: pipe.deliver(envelope),
        )
        for index in range(2)
    ]
    assert [item["state"] for item in queued] == ["queued", "queued"]

    next_day = datetime.fromtimestamp(
        clock[0], tz=now_local().tzinfo) + timedelta(hours=10, minutes=15)
    clock[0] = next_day.timestamp()
    assert [item.state for item in pipe.flush_due()] == [
        "delivered", "delivered"]
    same_day = [
        maybe_push_signal(
            _signal(id=f"day_{index}", title=f"day {index}"),
            subscription_checker=lambda: True,
            deliverer=lambda envelope, **_kwargs: pipe.deliver(envelope),
        )
        for index in range(2)
    ]
    assert [item["accepted"] for item in same_day] == [False, False]
    assert [item["reason"] for item in same_day] == [
        "metric_daily_cap", "metric_daily_cap"]
    assert sent == ["night_0", "night_1"]


def test_proactive_signal_uses_shared_0930_quiet_hours_default(
        tmp_path, monkeypatch):
    from core.delivery import DeliveryPipeline, TransportResult
    from core.proactive import maybe_push_signal

    monkeypatch.delenv("JARVIS_QUIET_START", raising=False)
    monkeypatch.delenv("JARVIS_QUIET_END", raising=False)
    sent = []
    stamp = now_local().replace(
        hour=9, minute=45, second=0, microsecond=0).timestamp()
    pipe = DeliveryPipeline(
        tmp_path,
        db_path=tmp_path / "delivery.db",
        transport=lambda envelope, channel: (
            sent.append((envelope.memorial_id, channel))
            or TransportResult(True)
        ),
        clock=lambda: stamp,
        sleeper=lambda _seconds: None,
    )

    result = maybe_push_signal(
        _signal(id="morning_signal"),
        subscription_checker=lambda: True,
        deliverer=lambda envelope, **_kwargs: pipe.deliver(envelope),
    )

    assert result["state"] == "delivered"
    assert sent == [("morning_signal", "push")]


def test_memorial_invokes_optional_reach_after_durable_signal(
        tmp_path, monkeypatch):
    from core import memorial, proactive

    monkeypatch.setattr(memorial, "JARVIS_DIR", tmp_path)
    reached = []
    monkeypatch.setattr(
        proactive,
        "maybe_push_signal",
        lambda state, **_kwargs: reached.append(state["id"]),
    )

    memorial_id, accepted = memorial.create(
        "eigenflux-feed-triage",
        "新信号",
        "已可靠写入网页",
    )

    assert accepted is True
    assert memorial.get_memorial(memorial_id)["delivery_status"] == "web_only"
    assert reached == [memorial_id]


def test_memorial_send_false_never_creates_optional_push(
        tmp_path, monkeypatch):
    from core import memorial, proactive

    monkeypatch.setattr(memorial, "JARVIS_DIR", tmp_path)
    monkeypatch.setattr(
        proactive,
        "maybe_push_signal",
        lambda *_args, **_kwargs: pytest.fail(
            "send=False must not create outbound reach"),
    )

    memorial_id, accepted = memorial.create(
        "eigenflux-feed-triage",
        "只入库",
        "由调用方管理传输",
        send=False,
    )

    assert accepted is True
    assert memorial.get_memorial(memorial_id)["delivery_status"] == "web_only"


def test_heartbeat_adapter_requests_reach_for_send_false_signal(
        tmp_path, monkeypatch):
    from core import memorial, proactive
    from core.card import build_card
    from core.heartbeat import _annotate_card_source

    monkeypatch.setattr(memorial, "JARVIS_DIR", tmp_path)
    reached = []
    monkeypatch.setattr(
        proactive,
        "maybe_push_signal",
        lambda state, **_kwargs: reached.append(
            (state["id"], state["source"])),
    )
    card = _annotate_card_source(
        build_card("📡 新信号", "值得查看", source="eigenflux-feed"),
        "eigenflux-feed-triage",
    )

    assert memorial.memorialize_output(
        "CARD:" + card,
        "eigenflux-feed-triage",
    ) == ""
    states = memorial.list_memorials()
    assert [(row["source"], row["delivery_status"]) for row in states] == [
        ("eigenflux-feed-triage", "web_only")]
    assert reached == [(states[0]["id"], "eigenflux-feed-triage")]


def test_mixed_heartbeat_cards_keep_exact_producer_source(
        tmp_path, monkeypatch):
    from core import memorial, proactive
    from core.card import build_card
    from core.heartbeat import _annotate_card_source

    monkeypatch.setattr(memorial, "JARVIS_DIR", tmp_path)
    reached = []
    monkeypatch.setattr(
        proactive,
        "maybe_push_signal",
        lambda state, **_kwargs: reached.append(state["source"]),
    )
    eigenflux = _annotate_card_source(
        build_card("📡 EF", "ef body"), "eigenflux-feed-triage")
    recommendation = _annotate_card_source(
        build_card("📺 推荐", "recommend body"), "content-recommend")

    assert memorial.memorialize_output(
        f"CARD:{eigenflux}\nCARD:{recommendation}",
        "eigenflux-feed-triage,content-recommend",
    ) == ""
    assert {
        row["title"]: row["source"] for row in memorial.list_memorials()
    } == {
        "EF": "eigenflux-feed-triage",
        "推荐": "content-recommend",
    }
    assert reached == ["eigenflux-feed-triage", "content-recommend"]


def test_signal_filter_searches_body_and_has_named_eigenflux_source():
    pytest.importorskip("nicegui", exc_type=ImportError)
    from dashboard.pages.signals import filter_signals

    now = now_local().timestamp()
    states = [
        _signal(epoch=now, body="董责险研究更新"),
        _signal(id="mem_old", epoch=now - 40 * 86400, body="旧内容"),
        _signal(id="mem_other", source="metrics-digest", epoch=now,
                title="指标日报", body="服务正常"),
        _signal(id="mem_decision", attention="decision", epoch=now,
                title="需要批示"),
        _signal(id="mem_read", status="decided", decided_opt="read",
                epoch=now - 60, body="已经读过但仍能搜索"),
        _signal(id="mem_corrupt", epoch="not-a-number", body="旧脏数据"),
    ]

    assert [row["id"] for row in filter_signals(
        states, query="董责险", source="eigenflux", time_window="7d",
        now=now,
    )] == ["mem_signal"]
    assert [row["id"] for row in filter_signals(
        states, source="metrics-digest", time_window="all", now=now,
    )] == ["mem_other"]
    assert [row["id"] for row in filter_signals(
        states, query="已经读过", time_window="all", now=now,
    )] == ["mem_read"]
    assert [row["id"] for row in filter_signals(
        states, query="旧脏数据", time_window="all", now=now,
    )] == ["mem_corrupt"]


def test_agenda_computes_concrete_next_fire_for_all_timer_types():
    pytest.importorskip("nicegui", exc_type=ImportError)
    from dashboard.pages.intentions import intent_next_fire

    now = now_local().replace(second=0, microsecond=0)
    date_target = now + timedelta(hours=2)
    interval_anchor = now - timedelta(minutes=35)
    rows = [
        {
            "trigger_type": "date",
            "trigger_config": json.dumps({"datetime": date_target.isoformat()}),
        },
        {
            "trigger_type": "cron",
            "trigger_config": json.dumps({"expression": "0 9 * * *"}),
            "next_fire_at": (now + timedelta(days=1)).replace(
                hour=9, minute=0).isoformat(),
        },
        {
            "trigger_type": "interval",
            "trigger_config": json.dumps({"seconds": 3600}),
            "created_at": interval_anchor.isoformat(),
        },
    ]

    assert intent_next_fire(rows[0], now=now) == date_target
    assert intent_next_fire(rows[1], now=now) > now
    interval_next = intent_next_fire(rows[2], now=now)
    assert interval_next is not None
    assert 20 * 60 <= (interval_next - now).total_seconds() <= 30 * 60


def test_agenda_keeps_overdue_cron_due_and_contains_malformed_cron():
    pytest.importorskip("nicegui", exc_type=ImportError)
    from dashboard.pages.intentions import intent_next_fire

    now = now_local().replace(second=30, microsecond=0)
    due = now - timedelta(seconds=30)
    assert intent_next_fire({
        "trigger_type": "cron",
        "trigger_config": json.dumps({"expression": "* * * * *"}),
        "next_fire_at": due.isoformat(),
    }, now=now) == due
    assert intent_next_fire({
        "trigger_type": "cron",
        "trigger_config": json.dumps({"expression": "x * * * *"}),
    }, now=now) is None
    for malformed in ("[]", "null", "1", '"x"'):
        assert intent_next_fire({
            "trigger_type": "interval",
            "trigger_config": malformed,
            "created_at": now.isoformat(),
        }, now=now) is None


@pytest.mark.parametrize(
    ("timezone_name", "expected_hour"),
    [("Asia/Shanghai", 9), ("UTC", 1)],
)
def test_agenda_converts_aware_schedule_to_user_timezone(
    timezone_name, expected_hour,
):
    pytest.importorskip("nicegui", exc_type=ImportError)
    from dashboard.pages.intentions import intent_next_fire
    from zoneinfo import ZoneInfo

    user_timezone = ZoneInfo(timezone_name)
    now = datetime(
        2026, 7, 25, 8, 0, 0, tzinfo=user_timezone,
    )
    result = intent_next_fire({
        "trigger_type": "date",
        "trigger_config": json.dumps({
            "datetime": "2026-07-25T01:00:00+00:00",
        }),
    }, now=now)

    assert result is not None
    assert result.tzinfo == now.tzinfo
    assert (result.hour, result.minute) == (expected_hour, 0)


def test_agenda_rejects_recurring_closure_and_nonpositive_interval():
    pytest.importorskip("nicegui", exc_type=ImportError)
    from dashboard.pages.intentions import agenda_trigger_config

    config, error = agenda_trigger_config(
        "cron", "0 9 * * *", category="external",
        closure_question="进展如何？")
    assert config is None
    assert "周期计划" in error

    config, error = agenda_trigger_config(
        "interval", "0", category="none", closure_question="")
    assert config is None
    assert "大于 0" in error

    config, error = agenda_trigger_config(
        "date", "2026-08-01T09:00", category="hard",
        closure_question="")
    assert config is None
    assert "结果追问" in error

    config, error = agenda_trigger_config(
        "date", "2026-08-01T09:00", category="none",
        closure_question="后来怎么样？")
    assert config is None
    assert "不会追问" in error


def test_agenda_closure_is_attributed_to_dashboard(monkeypatch):
    pytest.importorskip("nicegui", exc_type=ImportError)
    from dashboard.pages import intentions as page

    captured = {}

    class FakeIntentions:
        @staticmethod
        def record_closure(intent_id, **kwargs):
            captured.update(intent_id=intent_id, **kwargs)
            return True

    monkeypatch.setattr(page, "_get_intentions_module", lambda: FakeIntentions)
    assert page.close_intent_from_agenda("int_1") is True
    assert captured["intent_id"] == "int_1"
    assert captured["via"] == "dashboard"


def test_agenda_does_not_claim_success_when_closure_write_is_a_noop(
        monkeypatch):
    pytest.importorskip("nicegui", exc_type=ImportError)
    from dashboard.pages import intentions as page

    class FakeIntentions:
        @staticmethod
        def record_closure(*_args, **_kwargs):
            return False

    monkeypatch.setattr(page, "_get_intentions_module", lambda: FakeIntentions)

    assert page.close_intent_feedback("already_closed") == (
        "这项已经办结，无需重复记录",
        "info",
    )


def test_agenda_shows_inflight_commitments_but_hides_internal_followups():
    pytest.importorskip("nicegui", exc_type=ImportError)
    from dashboard.pages.intentions import agenda_commitments

    class FakeIntentions:
        @staticmethod
        def list_intents(*, status, limit):
            assert limit == 300
            return {
                "pending": [
                    {"id": "user_pending", "parent_intent_id": ""},
                    {"id": "internal_pending", "parent_intent_id": "parent_1"},
                ],
                "triggered": [
                    {"id": "user_triggered", "parent_intent_id": None},
                    {"id": "internal_triggered", "parent_intent_id": "parent_2"},
                ],
            }[status]

    assert [
        item["id"] for item in agenda_commitments(FakeIntentions)
    ] == ["user_pending", "user_triggered"]


def test_fixed_interval_rearms_after_success(intent_db):
    from core.intentions import (
        create_intent,
        get_intent,
        mark_executed,
        mark_triggered,
    )

    iid = create_intent(
        name="每小时观察",
        trigger_type="interval",
        trigger_config={"seconds": 3600},
    )
    db = intent_db.get_db()
    original_created_at = get_intent(iid)["created_at"]
    old_anchor = now_local().replace(tzinfo=None) - timedelta(hours=2)
    db.execute(
        "UPDATE intentions SET next_fire_at=? WHERE id=?",
        (old_anchor.isoformat(timespec="seconds"), iid),
    )
    db.commit()

    mark_triggered(iid)
    mark_executed(iid, "ok")

    row = get_intent(iid)
    assert row["status"] == "pending"
    assert row["attempt"] == 0
    assert row["created_at"] == original_created_at
    assert datetime.fromisoformat(row["next_fire_at"]) == (
        old_anchor + timedelta(hours=1)).replace(microsecond=0)


def test_core_rejects_nonpositive_interval(intent_db):
    from core.intentions import create_intent

    with pytest.raises(ValueError, match="positive"):
        create_intent(
            name="坏周期",
            trigger_type="interval",
            trigger_config={"seconds": -60},
        )


@pytest.mark.parametrize("legacy_config", [[], "legacy", 1])
def test_legacy_nonobject_interval_config_recovers_after_success(
        intent_db, legacy_config):
    from core.intentions import (
        create_intent,
        get_intent,
        mark_executed,
        mark_triggered,
    )

    iid = create_intent(
        name="旧版脏周期",
        trigger_type="interval",
        trigger_config={"seconds": 3600},
    )
    db = intent_db.get_db()
    db.execute(
        "UPDATE intentions SET trigger_config=? WHERE id=?",
        (json.dumps(legacy_config), iid),
    )
    db.commit()
    before = now_local().replace(tzinfo=None)

    mark_triggered(iid)
    mark_executed(iid, "handled")

    row = get_intent(iid)
    next_fire = datetime.fromisoformat(row["next_fire_at"])
    assert row["status"] == "pending"
    assert timedelta(minutes=9, seconds=50) <= next_fire - before
    assert next_fire - before <= timedelta(minutes=10, seconds=10)


@pytest.mark.parametrize("legacy_config", [[], "legacy", 1])
def test_legacy_nonobject_interval_does_not_poison_due_scan(
        intent_db, legacy_config):
    from core.intentions import create_intent, get_due_intents

    valid_id = create_intent(
        name="仍应触发",
        trigger_type="date",
        trigger_config={
            "datetime": (
                now_local().replace(tzinfo=None) - timedelta(minutes=1)
            ).isoformat(timespec="seconds"),
        },
    )
    dirty_id = create_intent(
        name="旧版脏周期",
        trigger_type="interval",
        trigger_config={"seconds": 60},
    )
    db = intent_db.get_db()
    db.execute(
        "UPDATE intentions SET trigger_config=? WHERE id=?",
        (json.dumps(legacy_config), dirty_id),
    )
    db.commit()

    due_ids = {item["id"] for item in get_due_intents()}

    assert valid_id in due_ids
    assert dirty_id not in due_ids


@pytest.mark.parametrize("trigger_type,legacy_config", [
    ("cron", []),
    ("cron", "legacy"),
    ("interval", []),
    ("interval", "legacy"),
])
def test_agenda_contains_nonobject_legacy_recovery_rows(
        intent_db, trigger_type, legacy_config):
    pytest.importorskip("nicegui", exc_type=ImportError)
    from core.intentions import create_intent
    from dashboard.pages.intentions import expired_autopsy, rearm_intent

    valid_config = (
        {"expression": "0 9 * * *"}
        if trigger_type == "cron"
        else {"seconds": 60}
    )
    iid = create_intent(
        name="旧版恢复项",
        trigger_type=trigger_type,
        trigger_config=valid_config,
    )
    db = intent_db.get_db()
    db.execute(
        "UPDATE intentions SET status='expired',trigger_config=? WHERE id=?",
        (json.dumps(legacy_config), iid),
    )
    db.commit()

    rows = expired_autopsy()

    assert next(item for item in rows if item["id"] == iid)["was_due"] == "?"
    assert rearm_intent(iid) is False


@pytest.fixture
def mobile_db(tmp_path, monkeypatch):
    import dashboard.db as db_module

    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "mobile.db")
    if db_module._connection is not None:
        db_module._connection.close()
    db_module._connection = None
    yield db_module
    if db_module._connection is not None:
        db_module._connection.close()
    db_module._connection = None


def test_push_status_distinguishes_paired_phone_from_local_subscription(
        mobile_db):
    from core.mobile_access import push_subscription_status, register_push

    db = mobile_db.get_db()
    db.execute(
        "INSERT INTO mobile_devices "
        "(id,label,token_hash,created_at,last_seen_at) VALUES (?,?,?,?,?)",
        ("dev_phone", "Phone", "hash", "2026-07-25T10:00:00",
         "2026-07-25T10:00:00"),
    )
    db.commit()
    register_push("local", {
        "endpoint": "https://push.example/local",
        "keys": {"p256dh": "local-key", "auth": "local-auth"},
    })
    assert push_subscription_status()["count"] == 1
    assert push_subscription_status(paired_only=True)["enabled"] is False

    register_push("dev_phone", {
        "endpoint": "https://push.example/phone",
        "keys": {"p256dh": "phone-key", "auth": "phone-auth"},
    })
    assert push_subscription_status(
        "dev_phone")["enabled"] is True
    assert push_subscription_status(paired_only=True)["count"] == 1


def test_revoked_device_cannot_reenable_push(mobile_db):
    from core.mobile_access import (
        push_subscription_status,
        register_push,
        revoke_device,
    )

    db = mobile_db.get_db()
    db.execute(
        "INSERT INTO mobile_devices "
        "(id,label,token_hash,created_at,last_seen_at) VALUES (?,?,?,?,?)",
        ("dev_revoked", "Old phone", "hash", "2026-07-25T10:00:00",
         "2026-07-25T10:00:00"),
    )
    db.commit()
    subscription = {
        "endpoint": "https://push.example/revoked",
        "keys": {"p256dh": "revoked-key", "auth": "revoked-auth"},
    }
    register_push("dev_revoked", subscription)
    assert revoke_device("dev_revoked")

    with pytest.raises(ValueError, match="revoked"):
        register_push("dev_revoked", subscription)

    assert push_subscription_status("dev_revoked")["enabled"] is False


def test_proactive_runtime_root_cannot_read_production_push_subscriptions(
        mobile_db, tmp_path, monkeypatch):
    from core.mobile_access import push_subscription_status, register_push
    from core.proactive import maybe_push_signal

    db = mobile_db.get_db()
    db.execute(
        "INSERT INTO mobile_devices "
        "(id,label,token_hash,created_at,last_seen_at) VALUES (?,?,?,?,?)",
        ("dev_prod", "Production phone", "hash", "2026-07-25T10:00:00",
         "2026-07-25T10:00:00"),
    )
    db.commit()
    register_push("dev_prod", {
        "endpoint": "https://push.example/production",
        "keys": {"p256dh": "prod-key", "auth": "prod-auth"},
    })
    assert push_subscription_status(paired_only=True)["count"] == 1
    # The suite injects JARVIS_DB_PATH to protect the live repo database.
    # Remove that process-wide override to exercise root-based isolation.
    monkeypatch.delenv("JARVIS_DB_PATH", raising=False)

    isolated_root = tmp_path / "isolated-runtime"
    result = maybe_push_signal(_signal(), root=isolated_root)

    assert result["reason"] == "no_paired_phone_subscription"
    assert (isolated_root / "data" / "jarvis.db").exists()
    assert push_subscription_status(
        paired_only=True, root=isolated_root)["count"] == 0


def test_browser_push_reconciliation_disables_only_current_endpoint(
        mobile_db):
    from core.mobile_access import push_subscription_status, register_push
    from dashboard.pages.settings import reconcile_browser_push

    register_push("local", {
        "endpoint": "https://push.example/stale",
        "keys": {"p256dh": "stale-key", "auth": "stale-auth"},
    })
    register_push("local", {
        "endpoint": "https://push.example/valid",
        "keys": {"p256dh": "valid-key", "auth": "valid-auth"},
    })
    assert push_subscription_status("local")["count"] == 2

    state = reconcile_browser_push("local", {
        "status": "denied",
        "endpoint": "https://push.example/stale",
    })
    assert state == {
        "checked": True,
        "enabled": False,
        "reason": "denied",
        "endpoint": "https://push.example/stale",
    }
    assert push_subscription_status("local")["count"] == 1


def test_browser_push_reconciliation_without_endpoint_preserves_other_browsers(
        mobile_db):
    from core.mobile_access import push_subscription_status, register_push
    from dashboard.pages.settings import reconcile_browser_push

    register_push("local", {
        "endpoint": "https://push.example/other-browser",
        "keys": {"p256dh": "other-key", "auth": "other-auth"},
    })

    state = reconcile_browser_push("local", {"status": "unsupported"})

    assert state["enabled"] is False
    assert push_subscription_status("local")["count"] == 1


def test_browser_notification_test_targets_exact_current_endpoint(monkeypatch):
    pytest.importorskip("nicegui", exc_type=ImportError)
    from core import mobile_access
    from dashboard.pages.settings import send_browser_test_notification

    captured = {}

    def fake_send_push(*_args, **kwargs):
        captured.update(kwargs)
        return {"sent": 1, "failed": 0, "disabled": 0}

    monkeypatch.setattr(mobile_access, "send_push", fake_send_push)

    result = send_browser_test_notification(
        "local", "https://push.example/current-browser")

    assert result["sent"] == 1
    assert captured == {
        "device_id": "local",
        "endpoint": "https://push.example/current-browser",
    }


def test_browser_notification_test_without_current_endpoint_does_not_broadcast(
        monkeypatch):
    pytest.importorskip("nicegui", exc_type=ImportError)
    from core import mobile_access
    from dashboard.pages.settings import send_browser_test_notification

    monkeypatch.setattr(
        mobile_access,
        "send_push",
        lambda *_args, **_kwargs: pytest.fail("must not broadcast"),
    )

    result = send_browser_test_notification("local", "")

    assert result["sent"] == 0
    assert result["reason"] == "no_current_subscription"


@pytest.mark.parametrize(
    ("result", "message_fragment", "notification_type"),
    [
        (
            {"sent": 1, "failed": 0, "disabled": 0},
            "推送服务已接收",
            "positive",
        ),
        (
            {
                "sent": 0,
                "failed": 0,
                "disabled": 0,
                "reason": "no_subscriber",
            },
            "没有可用的通知订阅",
            "warning",
        ),
        (
            {
                "sent": 0,
                "failed": 1,
                "disabled": 0,
                "error": "push gateway timeout",
            },
            "push gateway timeout",
            "negative",
        ),
        (
            {"sent": 0, "failed": 1, "disabled": 1},
            "通知订阅已失效",
            "warning",
        ),
    ],
)
def test_push_test_feedback_distinguishes_subscription_and_transport_failures(
        result, message_fragment, notification_type):
    pytest.importorskip("nicegui", exc_type=ImportError)
    from dashboard.pages.settings import push_test_feedback

    message, kind = push_test_feedback(result)

    assert message_fragment in message
    assert kind == notification_type


def test_runtime_db_override_unifies_push_preflight_with_dashboard_path(
        tmp_path, monkeypatch):
    import sqlite3

    import dashboard.db as db_module
    from core.mobile_access import push_subscription_status

    shared = tmp_path / "state" / "shared.db"
    runtime_root = tmp_path / "runtime"
    monkeypatch.setattr(db_module, "DB_PATH", db_module._DEFAULT_DB_PATH)
    monkeypatch.setenv("JARVIS_DIR", str(runtime_root))
    monkeypatch.setenv("JARVIS_DB_PATH", str(shared))
    assert db_module._db_path() == shared

    assert push_subscription_status(
        paired_only=True, root=runtime_root)["count"] == 0
    with sqlite3.connect(shared) as db:
        db.execute(
            "INSERT INTO mobile_devices "
            "(id,label,token_hash,created_at,last_seen_at) VALUES (?,?,?,?,?)",
            (
                "dev_shared",
                "Phone",
                "hash",
                "2026-07-25T10:00:00",
                "2026-07-25T10:00:00",
            ),
        )
        db.execute(
            "INSERT INTO matter_push_subscriptions "
            "(device_id,endpoint,p256dh,auth,enabled,created_at,updated_at) "
            "VALUES (?,?,?,?,1,?,?)",
            (
                "dev_shared",
                "https://push.example/shared",
                "key",
                "auth",
                "2026-07-25T10:00:00",
                "2026-07-25T10:00:00",
            ),
        )

    assert push_subscription_status(
        paired_only=True, root=runtime_root)["count"] == 1


def test_push_sender_excludes_dirty_subscription_for_revoked_device(
        mobile_db, monkeypatch):
    from core.mobile_access import send_push

    db = mobile_db.get_db()
    db.execute(
        "INSERT INTO mobile_devices "
        "(id,label,token_hash,created_at,last_seen_at,revoked_at) "
        "VALUES (?,?,?,?,?,?)",
        (
            "dev_dirty_revoked", "Old phone", "hash",
            "2026-07-25T10:00:00", "2026-07-25T10:00:00",
            "2026-07-25T11:00:00",
        ),
    )
    db.execute(
        "INSERT INTO matter_push_subscriptions "
        "(device_id,endpoint,p256dh,auth,enabled,created_at,updated_at) "
        "VALUES (?,?,?,?,1,?,?)",
        (
            "dev_dirty_revoked", "https://push.example/dirty-revoked",
            "key", "auth", "2026-07-25T10:00:00",
            "2026-07-25T10:00:00",
        ),
    )
    db.commit()
    monkeypatch.setitem(
        sys.modules,
        "pywebpush",
        types.SimpleNamespace(
            WebPushException=RuntimeError,
            webpush=lambda **_kwargs: pytest.fail(
                "revoked device must not receive Push"),
        ),
    )

    result = send_push("title", "body", device_id="dev_dirty_revoked")

    assert result["sent"] == 0
    assert result["reason"] == "no_subscriber"


def test_push_transport_requests_paired_devices_only(monkeypatch, tmp_path):
    from core import mobile_access
    from core.delivery import DeliveryEnvelope, _default_transport

    captured = {}

    def fake_send_push(*_args, **kwargs):
        captured.update(kwargs)
        return {"sent": 1, "failed": 0, "disabled": 0}

    monkeypatch.setattr(mobile_access, "send_push", fake_send_push)
    envelope = DeliveryEnvelope(
        source="test",
        kind="push",
        payload={"title": "t", "text": "b"},
        requested_channel="push",
        metadata={"paired_only": True},
    )
    result = _default_transport(tmp_path)(envelope, "push")
    assert result.ok is True
    assert captured["paired_only"] is True
    assert captured["root"] == tmp_path


def test_push_transport_uses_pipeline_database_scope(monkeypatch, tmp_path):
    from core import mobile_access
    from core.delivery import DeliveryEnvelope, DeliveryPipeline

    captured = {}

    def fake_send_push(*_args, **kwargs):
        captured.update(kwargs)
        return {"sent": 1, "failed": 0, "disabled": 0}

    monkeypatch.setattr(mobile_access, "send_push", fake_send_push)
    database = tmp_path / "alternate.db"
    pipeline = DeliveryPipeline(
        tmp_path,
        db_path=database,
        sleeper=lambda _seconds: None,
    )

    result = pipeline.deliver(DeliveryEnvelope(
        source="test",
        kind="push",
        payload={"title": "t", "text": "b"},
        attention="reply",
        requested_channel="push",
        metadata={
            "bypass_quiet": True,
            "bypass_throttle": True,
            "bypass_dedup": True,
        },
    ))

    assert result.state == "delivered"
    assert captured["root"] == tmp_path
    assert captured["db_path"] == database


def test_push_transport_keeps_legacy_four_argument_adapter(
        monkeypatch, tmp_path):
    from core import mobile_access
    from core.delivery import DeliveryEnvelope, DeliveryPipeline

    captured = {}

    def legacy_push(title, body, url="/items", matter_id=""):
        captured.update(
            title=title, body=body, url=url, matter_id=matter_id)
        return {"sent": 1, "failed": 0, "disabled": 0}

    monkeypatch.setattr(mobile_access, "send_push", legacy_push)
    pipeline = DeliveryPipeline(
        tmp_path,
        db_path=tmp_path / "alternate.db",
        sleeper=lambda _seconds: None,
    )

    result = pipeline.deliver(DeliveryEnvelope(
        source="test",
        kind="push",
        payload={"title": "t", "text": "b", "url": "/items/mem_1"},
        attention="reply",
        requested_channel="push",
        memorial_id="mem_1",
        metadata={
            "bypass_quiet": True,
            "bypass_throttle": True,
            "bypass_dedup": True,
        },
    ))

    assert result.state == "delivered"
    assert captured == {
        "title": "t",
        "body": "b",
        "url": "/items/mem_1",
        "matter_id": "mem_1",
    }


def test_push_subscription_revoked_during_delivery_is_cleanly_suppressed(
        monkeypatch, tmp_path):
    from core import delivery
    from core import mobile_access
    from core.delivery import DeliveryEnvelope, DeliveryPipeline

    calls = []

    def no_subscriber(*_args, **_kwargs):
        calls.append(1)
        return {
            "sent": 0,
            "failed": 0,
            "disabled": 0,
            "reason": "no_subscriber",
        }

    monkeypatch.setattr(mobile_access, "send_push", no_subscriber)
    pipe = DeliveryPipeline(
        tmp_path,
        db_path=tmp_path / "delivery.db",
        clock=lambda: now_local().replace(
            hour=14, minute=0, second=0, microsecond=0).timestamp(),
        sleeper=lambda _seconds: None,
    )
    result = pipe.deliver(DeliveryEnvelope(
        source="proactive-eigenflux",
        kind="push",
        payload={"title": "signal", "text": "body"},
        attention="notice",
        requested_channel="push",
        metadata={
            "paired_only": True,
            "optional_no_subscriber": True,
        },
    ))

    assert result.state == "suppressed"
    assert result.reason == "no_paired_phone_subscription"
    assert pipe.get(result.delivery_id)["attempts"] == 1
    assert pipe.pending_dead_letters() == []
    assert pipe.flush_due() == []
    assert calls == [1]
    with delivery.closing(delivery._connect(pipe.path)) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM delivery_cap_reservations"
        ).fetchone()[0] == 0


def test_required_mobile_push_without_subscriber_remains_retryable(
        monkeypatch, tmp_path):
    from core import mobile_access
    from core.delivery import DeliveryEnvelope, DeliveryPipeline

    calls = []

    def no_subscriber(*_args, **_kwargs):
        calls.append(1)
        return {
            "sent": 0,
            "failed": 0,
            "disabled": 0,
            "reason": "no_subscriber",
        }

    monkeypatch.setattr(mobile_access, "send_push", no_subscriber)
    pipe = DeliveryPipeline(
        tmp_path,
        db_path=tmp_path / "delivery.db",
        clock=lambda: now_local().replace(
            hour=14, minute=0, second=0, microsecond=0).timestamp(),
        sleeper=lambda _seconds: None,
    )

    result = pipe.deliver(DeliveryEnvelope(
        source="surface-handoff",
        kind="push",
        payload={"title": "handoff", "text": "continue on phone"},
        attention="reply",
        requested_channel="push",
        metadata={
            "paired_only": True,
            "bypass_quiet": True,
            "bypass_throttle": True,
        },
    ))

    assert result.state == "queued"
    assert result.reason == "no_subscriber"
    assert pipe.get(result.delivery_id)["next_attempt_epoch"] is not None
    assert calls == [1, 1, 1]


def test_pwa_shell_and_page_use_the_same_fresh_stylesheet_version():
    root = Path(__file__).parent.parent
    version = "20260725-proactivity"
    uiutil = (root / "dashboard" / "uiutil.py").read_text(encoding="utf-8")
    worker = (root / "dashboard" / "static" / "sw.js").read_text(
        encoding="utf-8")

    assert f"style.css?v={version}" in uiutil
    assert f"style.css?v={version}" in worker
    assert "jarvis-shell-v8" in worker
