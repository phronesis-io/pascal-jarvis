"""Regression contract for visible, bounded Jarvis proactivity."""

from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from core.timeutil import now_local


@pytest.fixture
def intent_db(tmp_path, monkeypatch):
    import core.intentions as intentions
    import core.db as db_module

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
    fixed_now = datetime(2032, 1, 15, 10, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
    monkeypatch.setattr(memorial, "now_local", lambda: fixed_now)
    monkeypatch.setattr(
        memorial,
        "now_local_str",
        lambda fmt="%Y-%m-%d %H:%M": fixed_now.strftime(fmt),
    )

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
        intent_db, legacy_config, monkeypatch):
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
    fixed_now = datetime(2032, 1, 15, 10, 30,
                         tzinfo=ZoneInfo("Asia/Shanghai"))
    monkeypatch.setattr("core.intentions.now_local", lambda: fixed_now)
    before = fixed_now.replace(tzinfo=None)

    mark_triggered(iid)
    mark_executed(iid, "handled")

    row = get_intent(iid)
    next_fire = datetime.fromisoformat(row["next_fire_at"])
    assert row["status"] == "pending"
    assert next_fire - before == timedelta(minutes=10)


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
