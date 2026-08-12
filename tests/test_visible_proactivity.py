"""Regression contract for visible, bounded Jarvis proactivity."""

from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

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


def test_proactive_phone_reach_module_stays_retired():
    """REQ-120: core.proactive (paired-phone push policy) is deleted, not
    dormant — a revival needs a new design, not a re-import."""
    assert importlib.util.find_spec("core.proactive") is None


def test_memorial_signal_routes_to_lark_and_ambient_to_ledger(
        tmp_path, monkeypatch):
    """REQ-119: Lark is the only delivery surface, ambient exhaust stays in
    the ledger. (core.proactive, the phone-reach compensation these paths
    once had to avoid, is deleted outright — REQ-120.)"""
    from core import memorial

    monkeypatch.setattr(memorial, "JARVIS_DIR", tmp_path)
    monkeypatch.setattr(memorial, "_send_card", lambda *a, **k: "om_test")
    monkeypatch.setattr(memorial, "_resolve_user_id", lambda: "ou_test")
    monkeypatch.setattr(memorial, "_quiet_hours_now", lambda: False)

    memorial_id, accepted = memorial.create(
        "eigenflux-feed-triage",
        "新信号",
        "一条值得查看的网络更新",
    )
    assert accepted is True
    assert memorial.get_memorial(memorial_id)["delivery_status"] == "delivered"

    ledger_id, ledger_accepted = memorial.create(
        "cross-session-sync", "监控尾气", "只入台账")
    assert ledger_accepted is True
    assert memorial.get_memorial(ledger_id)["delivery_status"] == "ledger_only"


def test_memorial_send_false_leaves_transport_to_the_caller(
        tmp_path, monkeypatch):
    from core import memorial

    monkeypatch.setattr(memorial, "JARVIS_DIR", tmp_path)

    memorial_id, accepted = memorial.create(
        "eigenflux-feed-triage",
        "只入库",
        "由调用方管理传输",
        send=False,
    )

    # The caller owns the transport: nothing is sent, nothing is faked.
    assert accepted is False
    assert memorial.get_memorial(memorial_id)["delivery_status"] == "not_sent"


def test_heartbeat_adapter_renders_signal_for_lark(
        tmp_path, monkeypatch):
    from core import memorial
    from core.card import build_card
    from core.heartbeat import _annotate_card_source

    monkeypatch.setattr(memorial, "JARVIS_DIR", tmp_path)
    card = _annotate_card_source(
        build_card("📡 新信号", "值得查看", source="eigenflux-feed"),
        "eigenflux-feed-triage",
    )

    rendered = memorial.memorialize_output(
        "CARD:" + card,
        "eigenflux-feed-triage",
    )
    assert rendered != ""  # curated signal earns the chat (2026-08-03)
    states = memorial.list_memorials()
    assert [row["source"] for row in states] == ["eigenflux-feed-triage"]
    assert f'"id": "{states[0]["id"]}"' in rendered


def test_mixed_heartbeat_cards_keep_exact_producer_source(
        tmp_path, monkeypatch):
    from core import memorial
    from core.card import build_card
    from core.heartbeat import _annotate_card_source

    monkeypatch.setattr(memorial, "JARVIS_DIR", tmp_path)
    eigenflux = _annotate_card_source(
        build_card("📡 EF", "ef body"), "eigenflux-feed-triage")
    recommendation = _annotate_card_source(
        build_card("📺 推荐", "recommend body"), "content-recommend")

    rendered = memorial.memorialize_output(
        f"CARD:{eigenflux}\nCARD:{recommendation}",
        "eigenflux-feed-triage,content-recommend",
    )
    assert len(rendered.splitlines()) == 2  # both render for Lark
    assert {
        row["title"]: row["source"] for row in memorial.list_memorials()
    } == {
        "EF": "eigenflux-feed-triage",
        "推荐": "content-recommend",
    }


def test_heartbeat_source_annotation_preserves_indented_json_example():
    from core.heartbeat import _annotate_card_source

    example = '    {"config":{},"elements":[]}'
    assert _annotate_card_source(example, "checkin") == example


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


def test_pwa_shell_and_page_use_the_same_fresh_stylesheet_version():
    root = Path(__file__).parent.parent
    version = "20260731-navguard"
    uiutil = (root / "dashboard" / "uiutil.py").read_text(encoding="utf-8")
    worker = (root / "dashboard" / "static" / "sw.js").read_text(
        encoding="utf-8")

    assert f"style.css?v={version}" in uiutil
    assert f"style.css?v={version}" in worker
    assert "jarvis-shell-v10" in worker
    assert 'updateViaCache:"none"' in uiutil
