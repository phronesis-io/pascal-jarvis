"""Authoritative Matter closure and linked-state convergence."""

from __future__ import annotations

import pytest

import core.db as db_module
from core import intentions, memorial
from core.continuity import create_handoff, get_handoff
from core.matter_closure import (
    MatterClosureBlocked,
    MatterClosureConflict,
    close_matter,
)
from core.matter_runs import acquire_run
from core.matters import create_matter, get_matter, open_followups, update_matter


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "jarvis.db")
    if db_module._connection is not None:
        db_module._connection.close()
    db_module._connection = None
    intentions._table_ready = False
    monkeypatch.setattr(memorial, "JARVIS_DIR", tmp_path)
    monkeypatch.setattr(memorial, "_sync_lark_card", lambda *_a, **_k: None)
    yield
    if db_module._connection is not None:
        db_module._connection.close()
    db_module._connection = None
    intentions._table_ready = False


def test_owner_closure_reconciles_intent_item_and_handoffs(tmp_path):
    matter = create_matter("白皮书发布", next_action="完成审校")
    intent_id = intentions.create_intent(
        name="提醒发布",
        trigger_type="date",
        trigger_config={"datetime": "2099-08-30T09:00:00+08:00"},
        matter_id=matter["id"],
    )
    memorial_id, _ = memorial.create(
        "test",
        "确认白皮书发布",
        "材料已经准备好",
        preset="decision",
        send=False,
        matter_id=matter["id"],
    )
    matter_handoff = create_handoff(
        "matter",
        matter["id"],
        from_surface="mobile",
        to_surface="desktop",
        title="继续白皮书",
        matter_id=matter["id"],
    )
    item_handoff = create_handoff(
        "memorial",
        memorial_id,
        from_surface="mobile",
        to_surface="desktop",
        title="处理发布卡",
        matter_id=matter["id"],
    )

    result = close_matter(
        matter["id"],
        outcome="白皮书已发布并读回验证",
        confirmation_text="确认这件事已经完成",
        source="codex",
    )
    replay = close_matter(
        matter["id"],
        outcome="白皮书已发布并读回验证",
        confirmation_text="确认这件事已经完成",
        source="codex",
    )

    assert result["status"] == "closed"
    assert replay["closure_id"] == result["closure_id"]
    assert replay["receipt_digest"] == result["receipt_digest"]
    assert get_matter(matter["id"])["status"] == "done"
    assert get_matter(matter["id"])["outcome"] == "白皮书已发布并读回验证"
    assert intentions.get_intent(intent_id)["status"] == "cancelled"
    assert memorial.get_memorial(memorial_id)["status"] == "decided"
    assert memorial.get_memorial(memorial_id)["resolved_label"] == "已随事项闭环"
    assert get_handoff(matter_handoff["id"])["status"] == "completed"
    assert get_handoff(item_handoff["id"])["status"] == "completed"
    assert open_followups(matter["id"]) == []
    assert any(
        event["event_type"] == "matter_closure_completed"
        for event in get_matter(matter["id"])["events"]
    )


def test_closure_requires_explicit_owner_words_before_any_mutation():
    matter = create_matter("不能由模型自说完成")
    intent_id = intentions.create_intent(
        name="仍需跟进",
        trigger_type="date",
        trigger_config={"datetime": "2099-08-30T09:00:00+08:00"},
        matter_id=matter["id"],
    )

    with pytest.raises(ValueError, match="owner confirmation"):
        close_matter(matter["id"], outcome="模型认为完成", confirmation_text="")

    assert get_matter(matter["id"])["status"] == "active"
    assert intentions.get_intent(intent_id)["status"] == "pending"


def test_live_run_blocks_closure_before_linked_state_is_reconciled(tmp_path):
    matter = create_matter("仍在执行")
    intent_id = intentions.create_intent(
        name="仍需跟进",
        trigger_type="date",
        trigger_config={"datetime": "2099-08-30T09:00:00+08:00"},
        matter_id=matter["id"],
    )
    run = acquire_run(
        matter["id"], executor="codex", workspace=tmp_path, now=100.0,
        lease_seconds=600,
    )

    with pytest.raises(MatterClosureBlocked) as caught:
        close_matter(
            matter["id"],
            outcome="不应关闭",
            confirmation_text="确认完成",
            now=101.0,
        )

    assert caught.value.blockers[0]["entity_type"] == "run"
    assert caught.value.blockers[0]["entity_id"] == run["id"]
    assert intentions.get_intent(intent_id)["status"] == "pending"
    assert get_matter(matter["id"])["status"] == "active"


def test_authoritative_closure_repairs_legacy_force_closed_residue():
    matter = create_matter("历史强制完成残留")
    intent_id = intentions.create_intent(
        name="旧提醒",
        trigger_type="date",
        trigger_config={"datetime": "2099-08-30T09:00:00+08:00"},
        matter_id=matter["id"],
    )
    update_matter(matter["id"], status="done", outcome="旧结果", force=True)
    assert open_followups(matter["id"])

    result = close_matter(
        matter["id"],
        outcome="旧结果已复核",
        confirmation_text="确认收掉历史残留",
    )

    assert result["status"] == "closed"
    assert intentions.get_intent(intent_id)["status"] == "cancelled"
    assert open_followups(matter["id"]) == []
    assert get_matter(matter["id"])["outcome"] == "旧结果已复核"


def test_a_different_second_closure_cannot_rewrite_authoritative_history():
    matter = create_matter("闭环收据不可覆盖")
    close_matter(
        matter["id"], outcome="结果甲", confirmation_text="确认结果甲",
    )

    with pytest.raises(MatterClosureConflict, match="different"):
        close_matter(
            matter["id"], outcome="结果乙", confirmation_text="确认结果乙",
        )

    assert get_matter(matter["id"])["outcome"] == "结果甲"


def test_partial_cross_store_failure_is_recoverable(tmp_path, monkeypatch):
    from core import matter_closure

    matter = create_matter("闭环中途失败")
    intent_id = intentions.create_intent(
        name="先取消的提醒",
        trigger_type="date",
        trigger_config={"datetime": "2099-08-30T09:00:00+08:00"},
        matter_id=matter["id"],
    )
    memorial_id, _ = memorial.create(
        "test", "后处理的卡", "待收口", preset="decision", send=False,
        matter_id=matter["id"],
    )
    real_reconcile = matter_closure._reconcile_memorial
    monkeypatch.setattr(
        matter_closure,
        "_reconcile_memorial",
        lambda *_a, **_k: (_ for _ in ()).throw(OSError("interrupted")),
    )

    with pytest.raises(MatterClosureBlocked):
        close_matter(
            matter["id"], outcome="完成", confirmation_text="确认完成",
        )

    assert intentions.get_intent(intent_id)["status"] == "cancelled"
    assert memorial.get_memorial(memorial_id)["status"] == "pending"
    assert get_matter(matter["id"])["status"] == "active"

    monkeypatch.setattr(matter_closure, "_reconcile_memorial", real_reconcile)
    recovered = close_matter(
        matter["id"], outcome="完成", confirmation_text="确认完成",
    )

    assert recovered["status"] == "closed"
    assert memorial.get_memorial(memorial_id)["status"] == "decided"
    assert get_matter(matter["id"])["status"] == "done"
