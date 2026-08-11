"""Cross-device handoff state.

The mobile desk is retired (REQ-120): 发到手机 is gone, so "mobile" is no
longer a creatable handoff target and no push accompanies any handoff.
Legacy mobile-bound rows must still render and close (never orphan), which
is why several tests seed them directly the way the old code wrote them.
"""

from __future__ import annotations

import json
import uuid
from contextlib import closing

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
from dashboard.uiutil import surface_from_headers


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


def _seed_legacy_mobile_handoff(entity_type: str, entity_id: str,
                                created_epoch: float = 100.0) -> str:
    """Insert a mobile-bound row exactly as the retired 发到手机 flow did.

    create_handoff refuses new mobile targets (REQ-120), but rows written
    before the retirement still exist in production databases and must keep
    closing through their entity's terminal projection.
    """
    from core import continuity

    handoff_id = f"hop_{uuid.uuid4().hex}"
    with closing(continuity._connect()) as db, db:
        db.execute(
            "INSERT INTO surface_handoffs ("
            "id,entity_type,entity_id,matter_id,from_surface,to_surface,"
            "status,title,note,created_by,created_epoch,metadata"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (handoff_id, entity_type, entity_id, "", "desktop", "mobile",
             "open", "legacy 发到手机", "", "local", created_epoch,
             json.dumps({})),
        )
    return handoff_id


def _delivery_envelope_count() -> int:
    """Direct DB truth for the no-push guard (REQ-120, review finding #11).

    The retired _notify_mobile went through DeliveryPipeline(...).deliver —
    an instance method — so patching module-level core.delivery.deliver
    would never have caught a revival. Counting rows in the isolated
    delivery database catches every possible seam.
    """
    db = db_module.get_db()
    exists = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='delivery_envelopes'").fetchone()
    if not exists:
        return 0
    return int(db.execute(
        "SELECT COUNT(*) FROM delivery_envelopes").fetchone()[0])


def test_surface_identity_defaults_to_desktop_without_device_header():
    assert surface_from_headers({}) == ("desktop", "local")
    assert surface_from_headers(
        {"X-Jarvis-Device": "dev_phone"}) == ("mobile", "dev_phone")


def test_handoff_is_idempotent_claimable_and_completable():
    memorial.create(
        "test", "继续实现", "完整背景", preset="fyi", send=False)
    entity_id = memorial.list_memorials()[0]["id"]
    first = create_handoff(
        "memorial", entity_id, from_surface="mobile",
        to_surface="desktop", title="继续实现",
        clock=lambda: 100.0,
    )
    duplicate = create_handoff(
        "memorial", entity_id, from_surface="mobile",
        to_surface="desktop", title="另一种说法",
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
        to_surface="desktop")
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
        to_surface="desktop")
    legacy_mobile = _seed_legacy_mobile_handoff("memorial", entity_id)

    assert complete_entity_handoffs("memorial", entity_id) == 2
    assert get_handoff(desktop["id"])["status"] == "completed"
    assert get_handoff(legacy_mobile)["status"] == "completed"


def test_projection_owned_handoffs_reopen_to_their_previous_states():
    memorial.create(
        "test", "可逆接力", "保留完成来源", preset="fyi", send=False)
    entity_id = memorial.list_memorials()[0]["id"]
    desktop = create_handoff(
        "memorial", entity_id, from_surface="mobile",
        to_surface="desktop")
    legacy_mobile = _seed_legacy_mobile_handoff("memorial", entity_id)
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
    complete_handoff(legacy_mobile)
    assert reopen_entity_handoffs(
        "memorial",
        entity_id,
        completion_source=source,
    ) == 1

    assert get_handoff(desktop["id"])["status"] == "claimed"
    assert get_handoff(legacy_mobile)["status"] == "completed"
    assert (
        get_handoff(legacy_mobile)["metadata"]["completion_confirmed_by"]
        == "manual"
    )


def test_reopen_chooses_latest_handoff_per_target_surface():
    memorial.create(
        "test", "连续接力", "只恢复最新接力", preset="fyi", send=False)
    entity_id = memorial.list_memorials()[0]["id"]
    source = f"delegation:{entity_id}:completed"
    older = create_handoff(
        "memorial", entity_id, from_surface="mobile",
        to_surface="desktop", clock=lambda: 100.0)
    complete_entity_handoffs(
        "memorial", entity_id,
        completion_source=source, clock=lambda: 101.0)
    newer = create_handoff(
        "memorial", entity_id, from_surface="mobile",
        to_surface="desktop", clock=lambda: 102.0)
    complete_entity_handoffs(
        "memorial", entity_id,
        completion_source=source, clock=lambda: 103.0)

    assert reopen_entity_handoffs(
        "memorial", entity_id, completion_source=source
    ) == 1
    assert get_handoff(newer["id"])["status"] == "open"
    assert get_handoff(older["id"])["status"] == "completed"
    assert reopen_entity_handoffs(
        "memorial", entity_id, completion_source=source
    ) == 0


def test_invalid_handoff_does_not_create_state():
    memorial.create(
        "test", "错误接力", "完整背景", preset="fyi", send=False)
    entity_id = memorial.list_memorials()[0]["id"]
    with pytest.raises(ValueError):
        create_handoff(
            "memorial", entity_id, from_surface="mobile",
            to_surface="mobile")
    with pytest.raises(ValueError):
        create_handoff(
            "unknown", "mem_x", from_surface="mobile",
            to_surface="desktop")
    with pytest.raises(KeyError):
        create_handoff(
            "memorial", "mem_missing", from_surface="mobile",
            to_surface="desktop")
    assert list_handoffs() == []


def test_mobile_is_no_longer_a_creatable_handoff_target():
    """REQ-120: 发到手机 is retired — the create entrance refuses mobile."""
    memorial_id, _ = memorial.create(
        "test", "发到手机已退役", "创建入口必须拒绝", preset="fyi", send=False)

    with pytest.raises(ValueError, match="REQ-120"):
        create_handoff(
            "memorial", memorial_id, from_surface="desktop",
            to_surface="mobile")

    assert list_handoffs() == []
    assert _delivery_envelope_count() == 0


def test_handoff_create_path_never_touches_the_delivery_pipeline():
    """A successful create writes a handoff row and nothing else (REQ-120).

    Asserted at the database, not by patching a function: the retired
    notifier used an instance-method seam a monkeypatch would miss.
    """
    memorial_id, _ = memorial.create(
        "test", "静默接力", "只写接力行", preset="fyi", send=False)

    result = create_handoff(
        "memorial", memorial_id, from_surface="mobile",
        to_surface="desktop")

    assert result["created"] is True
    assert "delivery_state" not in result
    assert list_handoffs(target_surface="desktop")[0]["id"] == result["id"]
    assert _delivery_envelope_count() == 0


def test_legacy_mobile_rows_still_render_and_close():
    """Read path keeps legacy mobile rows alive so they never orphan."""
    memorial_id, _ = memorial.create(
        "test", "存量手机接力", "只读不孤儿", preset="fyi", send=False)
    legacy = _seed_legacy_mobile_handoff("memorial", memorial_id)

    listed = list_handoffs(target_surface="mobile")
    assert [row["id"] for row in listed] == [legacy]

    assert complete_entity_handoffs("memorial", memorial_id) == 1
    assert get_handoff(legacy)["status"] == "completed"
    assert list_handoffs(target_surface="mobile") == []


def test_memorial_decision_completes_active_handoff():
    memorial_id, _ = memorial.create(
        "test", "跨端决定", "选一个", preset="decision", send=False)
    handoff = create_handoff(
        "memorial", memorial_id, from_surface="mobile",
        to_surface="desktop")

    memorial.decide(memorial_id, "approve")

    assert get_handoff(handoff["id"])["status"] == "completed"


def test_matter_handoff_closes_on_terminal_status():
    from core.matters import create_matter, update_matter

    matter = create_matter("跨设备项目", next_action="继续实现")
    handoff = create_handoff(
        "matter",
        matter["id"],
        from_surface="mobile",
        to_surface="desktop",
        title=matter["title"],
    )

    assert handoff["created"] is True

    update_matter(matter["id"], status="done")
    assert get_handoff(handoff["id"])["status"] == "completed"
