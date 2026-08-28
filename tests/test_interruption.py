from __future__ import annotations

import json

import pytest

from core import interruption, memorial


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(memorial, "JARVIS_DIR", tmp_path)
    monkeypatch.setattr(memorial, "_quiet_hours_now", lambda: False)
    monkeypatch.setattr(memorial, "_resolve_user_id", lambda: "ou_owner")
    monkeypatch.setattr(memorial, "_send_card", lambda *_a, **_k: "om_test")
    return tmp_path


def test_none_need_keeps_internal_change_in_ledger(env):
    memorial_id, accepted = memorial.create(
        source="attention-roi",
        title="内部调整",
        body="已经调整完",
        work_receipt="复算反馈并应用调整",
        owner_need="none",
        why_now="",
    )

    state = memorial.get_memorial(memorial_id)
    assert accepted is True
    assert state["delivery_status"] == "ledger_only"
    assert state["owner_need"] == "none"
    assert state["owner_need_explicit"] is True


def test_explicit_owner_message_requires_work_and_why_now(env):
    with pytest.raises(ValueError, match="completed-work"):
        memorial.create(
            source="proposal", title="要不要做", body="方案",
            owner_need="judgment", why_now="方案已完成",
            options=[{"key": "yes", "label": "做", "action": None}],
        )
    with pytest.raises(ValueError, match="why-now"):
        memorial.create(
            source="proposal", title="要不要做", body="方案",
            work_receipt="比较了三个方案",
            owner_need="judgment", why_now="",
            options=[{"key": "yes", "label": "做", "action": None}],
        )


def test_judgment_is_delivered_with_structured_reason(env):
    memorial_id, accepted = memorial.create(
        source="proposal", title="要不要做", body="方案",
        work_receipt="比较了三个方案",
        owner_need="judgment",
        why_now="分析已完成，只剩价值取舍",
        owner_action="选择是否执行方案",
        silence_cost="不提示会让方案停在未决状态",
        options=[{"key": "yes", "label": "做", "action": None}],
    )

    state = memorial.get_memorial(memorial_id)
    assert accepted is True
    assert state["delivery_status"] == "delivered"
    assert state["why_now"] == "分析已完成，只剩价值取舍"
    assert state["owner_action"] == "选择是否执行方案"
    assert state["silence_cost"] == "不提示会让方案停在未决状态"
    assert state["message_gate_version"] == 2
    decision = interruption.evaluate(state)
    assert decision["valid"] is True
    assert decision["lane"] == "lark"
    assert decision["label"] == "需要你的判断"
    assert decision["message_goal"] == "unlock_judgment"


@pytest.mark.parametrize(
    ("owner_action", "silence_cost", "error"),
    [
        ("", "不提示会停滞", "minimal owner action"),
        ("选择是否推进", "", "cost-of-silence"),
    ],
)
def test_v2_message_gate_requires_action_and_cost(
        env, owner_action, silence_cost, error):
    with pytest.raises(ValueError, match=error):
        memorial.create(
            source="proposal", title="要不要做", body="方案",
            work_receipt="完成方案比较",
            owner_need="judgment",
            why_now="只剩价值判断",
            owner_action=owner_action,
            silence_cost=silence_cost,
            options=[{"key": "yes", "label": "做", "action": None}],
        )


def test_historical_explicit_message_remains_auditable_under_v1_contract():
    decision = interruption.evaluate({
        "source": "proposal",
        "attention": "decision",
        "owner_need": "judgment",
        "owner_need_explicit": True,
        "work_receipt": "完成方案比较",
        "why_now": "只剩价值判断",
    })

    assert decision["valid"] is True
    assert decision["message_gate_version"] == 0


def test_v2_contract_upgrade_reuses_the_same_pending_legacy_item(env):
    options = [{"key": "yes", "label": "做", "action": None}]
    legacy_id, accepted = memorial.create(
        source="proposal", title="同一件事", body="方案",
        work_receipt="完成方案比较", attention="decision", options=options,
    )
    upgraded_id, upgraded = memorial.create(
        source="proposal", title="同一件事", body="方案",
        work_receipt="完成方案比较", attention="decision", options=options,
        owner_need="judgment", why_now="只剩价值判断",
        owner_action="选择是否推进", silence_cost="不提示会继续停滞",
    )

    assert accepted is True
    assert upgraded is True
    assert upgraded_id == legacy_id
    assert len(memorial.list_memorials()) == 1


def test_invalid_semantic_lane_is_rejected_before_ledger_write(env):
    with pytest.raises(ValueError, match="decision lane"):
        memorial.create(
            source="proposal", title="通知", body="方案",
            work_receipt="比较了三个方案",
            owner_need="judgment",
            why_now="分析已完成",
            preset="fyi",
            attention="notice",
        )
    assert memorial.list_memorials() == []


def test_only_deadline_may_bypass_quiet_hours_as_alert(env):
    with pytest.raises(ValueError, match="only deadline"):
        memorial.create(
            source="selfmon", title="需要授权", body="请重新授权",
            work_receipt="已完成自动恢复尝试",
            owner_need="authority",
            why_now="授权已经失效",
            attention="alert",
        )


def test_unknown_explicit_owner_need_is_rejected(env):
    with pytest.raises(ValueError, match="unknown owner need"):
        memorial.create(
            source="proposal", title="通知", body="方案",
            work_receipt="完成分析",
            owner_need="maybe-later",
            why_now="现在可能要看",
            attention="notice",
        )


def test_legacy_card_is_inferred_without_claiming_explicit_evidence(env):
    memorial_id, accepted = memorial.create(
        source="legacy-source", title="旧通知", body="结果",
        preset="fyi",
    )

    state = memorial.get_memorial(memorial_id)
    assert accepted is True
    assert state["owner_need"] == "requested_result"
    assert state["owner_need_explicit"] is False


def test_audit_counts_needs_and_invalid_rows():
    report = interruption.audit([
        {
            "source": "x", "attention": "notice",
            "owner_need": "none", "owner_need_explicit": True,
        },
        {
            "source": "x", "attention": "decision",
            "owner_need": "judgment", "owner_need_explicit": True,
            "work_receipt": "完成比较", "why_now": "只剩判断",
            "owner_action": "选一个方案", "silence_cost": "不提示会停滞",
            "message_gate_version": 2,
        },
        {"source": "legacy", "attention": "alert"},
    ])

    assert report == {
        "items": 3,
        "explicit": 2,
        "legacy_inferred": 1,
        "invalid": 0,
        "explicit_invalid": 0,
        "legacy_mismatch": 0,
        "gate_v2_visible": 1,
        "legacy_explicit_visible": 0,
        "by_owner_need": {"deadline": 1, "judgment": 1, "none": 1},
        "by_message_goal": {
            "protect_time_or_opportunity": 1,
            "preserve_state": 1,
            "unlock_judgment": 1,
        },
        "by_lane": {"lark": 2, "ledger": 1},
    }


def test_audit_separates_legacy_mismatch_from_new_contract_failure():
    report = interruption.audit([
        {"source": "delegation", "attention": "notice"},
        {
            "source": "proposal", "attention": "notice",
            "owner_need": "judgment", "owner_need_explicit": True,
            "work_receipt": "完成比较", "why_now": "只剩判断",
            "owner_action": "选一个方案", "silence_cost": "不提示会停滞",
            "message_gate_version": 2,
        },
    ])

    assert report["invalid"] == 2
    assert report["legacy_mismatch"] == 1
    assert report["explicit_invalid"] == 1


def test_interruption_audit_cli_is_machine_readable(env, capsys, monkeypatch):
    memorial.create(
        source="attention-roi", title="内部调整", body="完成",
        work_receipt="完成复算", owner_need="none", why_now="",
    )
    from core import interruption_audit
    monkeypatch.setattr(
        "core.interruption_audit.time.time", lambda: 2_000_000_000)

    assert interruption_audit.main([]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["items"] == 1
    assert payload["by_lane"] == {"ledger": 1}

    assert interruption_audit.main(["--days", "0.000001"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["items"] == 0
