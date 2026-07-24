"""Cross-device handoff state and exact Push routing."""

from __future__ import annotations

import types

import pytest

import dashboard.db as db_module
from core import memorial
from core.continuity import (
    claim_handoff,
    complete_entity_handoffs,
    complete_handoff,
    create_handoff,
    get_handoff,
    list_handoffs,
    reopen_entity_handoffs,
)
from core.delivery import DeliveryEnvelope, DeliveryPipeline
from dashboard.pages.items import surface_from_headers


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "jarvis.db")
    if db_module._connection is not None:
        db_module._connection.close()
    db_module._connection = None
    monkeypatch.setattr(memorial, "JARVIS_DIR", tmp_path)
    monkeypatch.setenv("JARVIS_DIR", str(tmp_path))
    yield
    if db_module._connection is not None:
        db_module._connection.close()
    db_module._connection = None


def test_surface_identity_comes_only_from_authenticated_gateway_header():
    assert surface_from_headers({}) == ("desktop", "local")
    assert surface_from_headers(
        {"X-Jarvis-Device": "dev_phone"}) == ("mobile", "dev_phone")


def test_handoff_is_idempotent_claimable_and_completable():
    memorial.create(
        "test", "继续实现", "完整背景", preset="fyi", send=False)
    entity_id = memorial.list_memorials()[0]["id"]
    first = create_handoff(
        "memorial", entity_id, from_surface="mobile",
        to_surface="desktop", title="继续实现", notify=False,
        clock=lambda: 100.0,
    )
    duplicate = create_handoff(
        "memorial", entity_id, from_surface="mobile",
        to_surface="desktop", title="另一种说法", notify=False,
        clock=lambda: 101.0,
    )
    assert first["created"] is True
    assert duplicate["created"] is False
    assert duplicate["id"] == first["id"]
    assert len(list_handoffs(target_surface="desktop")) == 1

    claimed = claim_handoff(
        first["id"], surface="desktop", clock=lambda: 102.0)
    assert claimed["status"] == "claimed"
    assert claimed["claimed_epoch"] == 102.0
    with pytest.raises(ValueError):
        claim_handoff(first["id"], surface="mobile")

    completed = complete_handoff(first["id"], clock=lambda: 103.0)
    assert completed["status"] == "completed"
    assert list_handoffs(target_surface="desktop") == []


def test_every_handoff_connection_is_closed(monkeypatch):
    from core import continuity

    opened = []
    real_connect = continuity._connect

    def tracked_connect():
        connection = real_connect()
        opened.append(connection)
        return connection

    monkeypatch.setattr(continuity, "_connect", tracked_connect)
    memorial_id, _ = memorial.create(
        "test", "连接生命周期", "每次操作都应关闭", preset="fyi", send=False)
    handoff = create_handoff(
        "memorial", memorial_id, from_surface="mobile",
        to_surface="desktop", notify=False)
    assert get_handoff(handoff["id"])
    assert list_handoffs(target_surface="desktop")
    assert claim_handoff(handoff["id"], surface="desktop")["status"] == "claimed"
    assert complete_handoff(handoff["id"])["status"] == "completed"

    assert opened
    for connection in opened:
        with pytest.raises(Exception, match="closed"):
            connection.execute("SELECT 1")


def test_completing_entity_closes_every_direction():
    memorial.create(
        "test", "双向接力", "完整背景", preset="fyi", send=False)
    entity_id = memorial.list_memorials()[0]["id"]
    desktop = create_handoff(
        "memorial", entity_id, from_surface="mobile",
        to_surface="desktop", notify=False)
    mobile = create_handoff(
        "memorial", entity_id, from_surface="desktop",
        to_surface="mobile", notify=False)

    assert complete_entity_handoffs("memorial", entity_id) == 2
    assert get_handoff(desktop["id"])["status"] == "completed"
    assert get_handoff(mobile["id"])["status"] == "completed"


def test_projection_owned_handoffs_reopen_to_their_previous_states():
    memorial.create(
        "test", "可逆接力", "保留完成来源", preset="fyi", send=False)
    entity_id = memorial.list_memorials()[0]["id"]
    desktop = create_handoff(
        "memorial", entity_id, from_surface="mobile",
        to_surface="desktop", notify=False)
    mobile = create_handoff(
        "memorial", entity_id, from_surface="desktop",
        to_surface="mobile", notify=False)
    claim_handoff(desktop["id"], surface="desktop")

    source = f"delegation:{entity_id}:completed"
    assert complete_entity_handoffs(
        "memorial", entity_id, completion_source=source
    ) == 2
    assert reopen_entity_handoffs(
        "memorial",
        entity_id,
        completion_source="another-projection",
    ) == 0
    complete_handoff(mobile["id"])
    assert reopen_entity_handoffs(
        "memorial",
        entity_id,
        completion_source=source,
    ) == 1

    assert get_handoff(desktop["id"])["status"] == "claimed"
    assert get_handoff(mobile["id"])["status"] == "completed"
    assert (
        get_handoff(mobile["id"])["metadata"]["completion_confirmed_by"]
        == "manual"
    )


def test_invalid_handoff_does_not_create_state():
    memorial.create(
        "test", "错误接力", "完整背景", preset="fyi", send=False)
    entity_id = memorial.list_memorials()[0]["id"]
    with pytest.raises(ValueError):
        create_handoff(
            "memorial", entity_id, from_surface="mobile",
            to_surface="mobile", notify=False)
    with pytest.raises(ValueError):
        create_handoff(
            "unknown", "mem_x", from_surface="mobile",
            to_surface="desktop", notify=False)
    with pytest.raises(KeyError):
        create_handoff(
            "memorial", "mem_missing", from_surface="mobile",
            to_surface="desktop", notify=False)
    assert list_handoffs() == []


def test_push_transport_preserves_exact_target_url(tmp_path, monkeypatch):
    captured = {}

    def fake_push(title, body, url="/items", matter_id=""):
        captured.update(
            title=title, body=body, url=url, matter_id=matter_id)
        return {"sent": 1, "failed": 0, "disabled": 0}

    monkeypatch.setattr("core.mobile_access.send_push", fake_push)
    pipeline = DeliveryPipeline(
        tmp_path, db_path=tmp_path / "delivery.db",
        sleeper=lambda _seconds: None,
    )
    result = pipeline.deliver(DeliveryEnvelope(
        source="handoff-test",
        kind="push",
        payload={
            "title": "发到手机",
            "text": "完整事项",
            "url": "/items/mem_exact",
        },
        attention="reply",
        requested_channel="push",
        memorial_id="mem_exact",
        metadata={
            "bypass_quiet": True,
            "bypass_throttle": True,
            "bypass_dedup": True,
        },
    ))

    assert result.state == "delivered"
    assert captured == {
        "title": "发到手机",
        "body": "完整事项",
        "url": "/items/mem_exact",
        "matter_id": "mem_exact",
    }


def test_push_exception_keeps_durable_mobile_handoff(monkeypatch):
    from core import continuity

    memorial_id, _ = memorial.create(
        "test", "通知降级", "仍要留在手机接力区", preset="fyi", send=False)
    monkeypatch.setattr(
        continuity, "_notify_mobile",
        lambda _item: (_ for _ in ()).throw(RuntimeError("push offline")),
    )

    result = create_handoff(
        "memorial", memorial_id, from_surface="desktop",
        to_surface="mobile")

    assert result["created"] is True
    assert result["delivery_state"] == "failed"
    assert "push offline" in result["delivery_reason"]
    assert list_handoffs(target_surface="mobile")[0]["id"] == result["id"]


def test_memorial_decision_completes_active_handoff():
    memorial_id, _ = memorial.create(
        "test", "跨端决定", "选一个", preset="decision", send=False)
    handoff = create_handoff(
        "memorial", memorial_id, from_surface="mobile",
        to_surface="desktop", notify=False)

    memorial.decide(memorial_id, "approve")

    assert get_handoff(handoff["id"])["status"] == "completed"


def test_matter_handoff_uses_exact_route_and_closes_on_terminal(monkeypatch):
    from core.matters import create_matter, update_matter

    captured = {}

    def fake_push(title, body, url="/items", matter_id=""):
        captured.update(url=url, matter_id=matter_id)
        return {"sent": 1, "failed": 0, "disabled": 0}

    monkeypatch.setattr("core.mobile_access.send_push", fake_push)
    matter = create_matter("跨设备项目", next_action="继续实现")
    handoff = create_handoff(
        "matter",
        matter["id"],
        from_surface="desktop",
        to_surface="mobile",
        title=matter["title"],
    )

    assert handoff["delivery_state"] == "delivered"
    assert captured == {
        "url": f"/matters/{matter['id']}",
        "matter_id": matter["id"],
    }

    update_matter(matter["id"], status="done")
    assert get_handoff(handoff["id"])["status"] == "completed"
