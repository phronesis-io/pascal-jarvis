"""Tests for core.memorial — the 奏折 (memorial) card framework.

Covers: create / ledger fold / decide idempotence / action execution via
ActionProcessor / chat injection into bot.sh's pending-merge channel /
CLI parsing / preset expansion / card JSON structure (button value
round-trip). All lark-cli sends are mocked — nothing real is sent.
"""

import io
import json
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

import core.memorial as memorial
import core.memorial_reader as memorial_reader
from core.memorial_revision import revise_pending
from core.card import build_card
from core.matter_bridge import bind_conversation
from core.matters import create_matter


# ── fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated JARVIS_DIR + mocked send channels. Returns a recorder."""
    monkeypatch.setattr(memorial, "JARVIS_DIR", tmp_path)
    rec = SimpleNamespace(dir=tmp_path, cards=[], texts=[], send_ok=True)

    def fake_send_card(card_json_str, chat_id=""):
        rec.cards.append((card_json_str, chat_id))
        # str contract since REQ-118: message_id on success, "" on failure.
        # Returning bool True here once leaked "True" ids into the
        # PRODUCTION ledger via record_sent (red-team 7/21 finding 1).
        return "om_test_fixture" if rec.send_ok else ""

    def fake_send_text(text, chat_id=""):
        rec.texts.append((text, chat_id))
        return "om_test_fixture" if rec.send_ok else ""

    monkeypatch.setattr(memorial, "_send_card", fake_send_card)
    monkeypatch.setattr(memorial, "_send_text", fake_send_text)
    monkeypatch.setattr(memorial, "_resolve_user_id", lambda: "ou_test")
    monkeypatch.setattr(memorial, "_quiet_hours_now", lambda: False)
    fixed_now = datetime(2032, 1, 15, 10, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
    monkeypatch.setattr(memorial, "now_local", lambda: fixed_now)
    monkeypatch.setattr(
        memorial,
        "now_local_str",
        lambda fmt="%Y-%m-%d %H:%M": fixed_now.strftime(fmt),
    )
    # Most tests care about chat semantics, not thread scheduling. Keep them
    # deterministic; the dedicated async test below covers non-blocking send.
    monkeypatch.setattr(memorial, "_send_opener_async",
                        lambda text, chat_id, continuation=None:
                        memorial._deliver_opener(text, chat_id, continuation))
    return rec


def _ledger_events(tmp_path) -> list[dict]:
    p = tmp_path / "memorials.jsonl"
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()]


def _action_rows(card: dict) -> list[list[dict]]:
    return [el["actions"] for el in card.get("elements", [])
            if el.get("tag") == "action"]


def _actions(card: dict) -> list[dict]:
    return [action for row in _action_rows(card) for action in row]


# ── create ───────────────────────────────────────────────────────────────


def test_runtime_paths_follow_late_jarvis_dir_injection(tmp_path, monkeypatch):
    """Collection-time import must not pin memorial writes to the checkout."""
    monkeypatch.setattr(memorial, "JARVIS_DIR", memorial._IMPORTED_JARVIS_DIR)
    monkeypatch.setenv("JARVIS_DIR", str(tmp_path))

    assert memorial._ledger_path() == tmp_path / "memorials.jsonl"
    assert memorial._pending_merge_path() == tmp_path / "jobs" / "pending_merge.jsonl"
    assert memorial._outbox_path() == tmp_path / "heartbeat_outbox.jsonl"
    assert memorial._explain_queue_path() == tmp_path / "data" / "explain_queue.jsonl"
    assert memorial._reply_followup_queue_path() == (
        tmp_path / "data" / "reply_followup_queue.jsonl")


def test_create_routes_ordinary_decision_to_lark(env):
    """REQ-119: Lark is the only delivery surface — an ordinary decision is a
    real Lark card, not a row parked on a desk that never rang (7/24)."""
    mid, sent = memorial.create("mail", "测试标题", "正文内容", preset="decision")

    assert mid.startswith("mem_")
    assert sent is True
    events = _ledger_events(env.dir)
    assert [e["ev"] for e in events] == ["create", "delivery", "sent"]
    ev = events[0]
    assert ev["ev"] == "create" and ev["id"] == mid
    assert ev["source"] == "mail" and ev["title"] == "测试标题"
    assert [o["key"] for o in ev["options"]] == ["approve", "defer", "reject"]
    assert ev["review_surface"] == "lark"
    assert events[1]["status"] == "delivered"
    assert len(env.cards) == 1
    assert (env.dir / "heartbeat_outbox.jsonl").exists()


def test_create_card_structure_and_button_value_round_trip(env):
    mid, _ = memorial.create("mail", "标题", "正文", preset="decision")
    card = json.loads(memorial.card_json(mid))

    assert card["header"]["title"]["content"] == "📜 📬 标题"
    # Role line first (8/3): the card opens by saying what it wants of him.
    assert card["elements"][0]["text"]["content"] == "🎯 等你拍一个\n\n正文"
    rows = _action_rows(card)
    assert [len(row) for row in rows] == [3, 2]  # choices, then Chat+看不懂
    actions = _actions(card)
    assert len(actions) == 5
    assert actions[0]["type"] == "primary"
    assert actions[-1]["text"]["content"] == "🤔 看不懂"
    for a, opt in zip(actions, ("approve", "defer", "reject", "chat", "confused")):
        # the value dict must round-trip exactly as the sidecar will see it
        v = json.loads(json.dumps(a["value"]))
        assert v == {"action": "memorial", "id": mid, "opt": opt}


def test_revise_pending_updates_ledger_delivery_and_visible_card(
        env, monkeypatch):
    synced = []
    monkeypatch.setattr(
        memorial,
        "_sync_lark_card",
        lambda memorial_id, card: synced.append((memorial_id, card)),
    )
    mid, _ = memorial.create("eigenflux", "一封来信", "旧正文", preset="fyi")

    assert revise_pending(
        mid,
        title="两封合并来信",
        body="我看过了：值得看\n原话：两封原文",
        context='{"external_event_ids":["a","b"]}',
        options=memorial.PRESETS["fyi"],
        work_receipt="逐封核验并合并",
        authoring_audit_text="值得看",
    ) is True

    state = memorial.get_memorial(mid)
    assert state["title"] == "两封合并来信"
    assert state["body"].startswith("我看过了：")
    assert [option["key"] for option in state["options"]] == ["read", "watch"]
    assert [event["ev"] for event in _ledger_events(env.dir)].count("revise") == 1
    from core.delivery import DeliveryPipeline
    row = DeliveryPipeline(env.dir).list_source("eigenflux")[0]
    assert "两封原文" in json.loads(row["payload"])["text"]
    assert synced and synced[0][0] == mid

    assert memorial.lapse(mid, "hour closed") is True
    assert revise_pending(mid, body="不应重开") is False
    assert memorial.get_memorial(mid)["body"] != "不应重开"


def test_create_defaults_to_fyi_preset(env):
    mid, _ = memorial.create("selfmon", "t", "b")
    st = memorial.get_memorial(mid)
    assert [o["label"] for o in st["options"]] == ["已阅", "标为重点"]
    assert st["attention"] == "notice"
    assert memorial.requires_decision(st) is False
    assert memorial.review_surface(st) == "none"
    # REQ-119: a notice is a Lark card too — nothing routes to the web.
    assert st["delivery_status"] == "delivered"
    assert len(env.cards) == 1


def _routine_options(routine_id: str = "rt_test") -> list[dict]:
    # Exact shape core.routines._card_options emits: ack plus the pause mute.
    return [
        {"key": "ack", "label": "知道了", "action": None},
        {"key": "pause", "label": "这条以后别发了",
         "action": {"type": "routine_pause", "params": {"id": routine_id}}},
    ]


def test_routine_card_is_notice_not_pending_decision(env):
    # 8/3–8/10: the pause key promoted all 51 起来动动 rehab cards to
    # decision class, so each carried a 48h 待批 deadline and 54 clogged the
    # escrow docket — against the rule that rehab never becomes a demand.
    mid, _ = memorial.create("routine:起来动动", "起来动动·肩胛",
                             "把肩胛骨往后收几下。",
                             options=_routine_options())
    st = memorial.get_memorial(mid)
    assert st["attention"] == "notice"
    assert memorial.requires_decision(st) is False

    from datetime import datetime, timedelta
    created = datetime.strptime(st["ts"], "%Y-%m-%d %H:%M")
    # Past the decision deadline it must NOT surface as 待批; notices leave
    # the live attention queue after 24h instead.
    scan = memorial.escrow_scan(now=created + timedelta(hours=72))
    assert scan["overdue"] == []
    assert [s["id"] for s, _ in scan["lapse"]] == [mid]


def test_routine_notice_reaches_lark(env):
    # The product's own voice must not go invisible (the 7/24 regression):
    # a routine notice rings Lark like checkin does.
    _, sent = memorial.create("routine:起来动动", "起来动动·眼睛",
                              "看远处 20 秒。", options=_routine_options())
    assert sent is True
    assert len(env.cards) == 1


def test_pause_mute_neither_promotes_nor_speaks_but_still_acts(env,
                                                               monkeypatch):
    # 「这条以后别发了」 silences a source: it is not an ask (no decision
    # class on any source using it), its label is not a sentence Pascal said
    # (no injected context), yet the bound routine_pause action still runs.
    assert memorial._infer_attention(
        [{"key": "ack"}, {"key": "pause"}], []) == memorial.ATTENTION_NOTICE

    ran = []
    monkeypatch.setattr(memorial, "_execute_action",
                        lambda action, **kw: ran.append(action) or "已暂停")
    mid, _ = memorial.create("mail", "t", "b", options=_routine_options())
    memorial.decide(mid, "pause")

    assert ran and ran[0]["type"] == "routine_pause"
    pm = env.dir / "jobs" / "pending_merge.jsonl"
    lines = pm.read_text().splitlines() if pm.exists() else []
    assert not any("memorial-decision" in l for l in lines)


def test_review_surface_matrix_preserves_attention_budget(env):
    """REQ-119: every decision reviews on Lark; every card is a Lark card."""
    ordinary_id, _ = memorial.create("project", "方案", "选一个",
                                     preset="decision")
    lark_id, _ = memorial.create(
        "project", "现在决定", "正在飞书里聊", preset="decision",
        chat_id="oc_live")
    calendar_id, _ = memorial.create(
        "calendar-sync", "日程冲突", "挪哪一个", preset="decision")
    alert_id, _ = memorial.create(
        "selfmon", "服务异常", "需要立即看", preset="fyi", urgent=True)

    ordinary = memorial.get_memorial(ordinary_id)
    lark = memorial.get_memorial(lark_id)
    calendar = memorial.get_memorial(calendar_id)
    alert = memorial.get_memorial(alert_id)

    assert memorial.review_surface(ordinary) == "lark"
    assert ordinary["delivery_status"] == "delivered"
    assert memorial.review_surface(lark) == "lark"
    assert memorial.review_surface(calendar) == "lark"
    assert memorial.review_surface(alert) == "none"
    assert alert["attention"] == "alert"
    assert len(env.cards) == 4
    # 8/3: the ask moved to a role line at the TOP of every card (the old
    # bottom status pair only covered two classes and sat below the fold).
    assert "🎯 等你拍一个" in env.cards[0][0]
    assert "🎯 等你拍一个" in env.cards[1][0]
    assert "🎯 等你拍一个" in env.cards[2][0]
    assert "⚡ 即时提醒 · 不用批" in env.cards[3][0]


def test_old_delivered_decision_truthfully_stays_lark_routed():
    old = {
        "source": "heartbeat",
        "attention": "decision",
        "options": [{"key": "approve", "label": "同意"}],
        "extra_buttons": [],
        "delivery_status": "delivered",
    }
    assert memorial.review_surface(old) == "lark"


def test_attention_class_fully_determines_review_surface(env):
    """REQ-119: the legacy review_at hint is ignored — a decision reviews on
    Lark and a notice reviews nowhere, whatever the caller asks for."""
    mid, _ = memorial.create(
        "mail", "通知", "看看", preset="fyi", review_at="lark")
    assert memorial.review_surface(memorial.get_memorial(mid)) == "none"
    mid2, _ = memorial.create(
        "mail", "决定", "选一个", preset="decision", review_at="none")
    assert memorial.review_surface(memorial.get_memorial(mid2)) == "lark"


def test_hard_immediacy_cannot_be_downgraded_to_phone(env):
    mid, _ = memorial.create(
        "calendar-sync", "马上冲突", "挪哪一个", preset="decision",
        review_at="phone")
    assert memorial.review_surface(memorial.get_memorial(mid)) == "lark"
    assert len(env.cards) == 1


def test_card_compacts_long_body_but_ledger_keeps_full_context(env):
    body = "\n".join(f"第{i}行 " + "细节" * 80 for i in range(12))
    mid, _ = memorial.create("mail", "长邮件", body, preset="fyi")
    card_body = json.loads(memorial.card_json(mid))["elements"][0]["text"]["content"]
    assert len(card_body) < len(body)
    assert memorial.CLIP_NOTICE in card_body
    assert memorial.get_memorial(mid)["body"] == body


def test_create_rejects_reserved_chat_key_and_unknown_preset(env):
    with pytest.raises(ValueError):
        memorial.create("x", "t", "b",
                        options=[{"key": "chat", "label": "撞框架保留键"}])
    with pytest.raises(ValueError):
        memorial.create("x", "t", "b", preset="nonsense")
    with pytest.raises(ValueError):
        memorial.create("x", "t", "b",
                        options=[{"key": "a", "label": "一"},
                                 {"key": "a", "label": "二"}])
    assert _ledger_events(env.dir) == []  # nothing recorded on bad input


def test_create_send_failure_still_ledgered(env):
    env.send_ok = False
    mid, sent = memorial.create(
        "mail", "t", "b", preset="decision", review_at="lark")
    assert sent is False
    assert memorial.get_memorial(mid)["status"] == "pending"
    assert memorial.get_memorial(mid)["delivery_status"] == "retry_queued"
    # no outbox mirror for an unsent card
    assert not (env.dir / "heartbeat_outbox.jsonl").exists()
    # Exact Lark-routed card is retained once in SQLite; buttons stay intact.
    from core.delivery import DeliveryPipeline
    queued = DeliveryPipeline(env.dir).list(state="queued")
    assert queued[0]["memorial_id"] == mid
    payload = json.loads(queued[0]["payload"])
    assert json.loads(payload["card_json"])["elements"][1]["tag"] == "action"
    assert not (env.dir / memorial.MEMORIAL_QUEUE_FILE).exists()
    # card_json also lets a caller explicitly inspect/re-deliver it.
    card = json.loads(memorial.card_json(mid))
    assert card["header"]["title"]["content"].startswith("📜")


# ── decide ───────────────────────────────────────────────────────────────


def test_decide_records_and_replaces_card(env):
    mid, _ = memorial.create("mail", "标题", "正文", preset="decision")
    payload = memorial.decide(mid, "approve")

    assert payload["toast"]["type"] == "success"
    assert "已批：同意" in payload["toast"]["content"]
    assert payload["card"]["type"] == "raw"
    card = payload["card"]["data"]
    body = card["elements"][0]["text"]["content"]
    assert "正文" in body and "✅ 已批：同意" in body
    # The decision choices are removed, but Chat remains available.
    assert [a["text"]["content"] for a in _actions(card)] == ["💬 聊聊这个", "🤔 看不懂"]
    st = memorial.get_memorial(mid)
    assert st["status"] == "decided" and st["decided_opt"] == "approve"
    decision = next(json.loads(line) for line in
                    (env.dir / "jobs" / "pending_merge.jsonl").read_text().splitlines()
                    if "memorial-decision" in line)
    assert "选择了「同意」" in decision["summary"]


def test_fyi_tap_does_not_inject_context(env):
    mid, _ = memorial.create("mail", "通知", "无需回复", preset="fyi")
    memorial.decide(mid, "read")

    pm = env.dir / "jobs" / "pending_merge.jsonl"
    lines = pm.read_text().splitlines() if pm.exists() else []
    assert not any("memorial-decision" in l for l in lines)


def test_decide_is_idempotent(env):
    mid, _ = memorial.create("mail", "t", "b", preset="fyi")
    memorial.decide(mid, "read")
    payload = memorial.decide(mid, "watch")  # second tap, different button

    assert payload["toast"]["type"] == "info"
    assert "已批过" in payload["toast"]["content"]
    decides = [e for e in _ledger_events(env.dir) if e["ev"] == "decide"]
    assert len(decides) == 1 and decides[0]["opt"] == "read"


def test_decide_unknown_id_and_unknown_opt(env):
    payload = memorial.decide("mem_nope", "read")
    assert payload["toast"]["type"] == "info"
    assert "card" not in payload

    mid, _ = memorial.create("mail", "t", "b", preset="fyi")
    payload = memorial.decide(mid, "nonsense")
    assert payload["toast"]["type"] == "info"
    assert memorial.get_memorial(mid)["status"] == "pending"


def test_decide_runs_action_through_action_processor(env, monkeypatch):
    calls = []

    class FakeAP:
        def __init__(self, **kw):
            pass

        def _do_intent_close(self, raw):
            calls.append(raw)
            return "Closure recorded"

    import core.actions as actions
    monkeypatch.setattr(actions, "ActionProcessor", FakeAP)

    mid, _ = memorial.create(
        "intent", "t", "b",
        options=[{"key": "done", "label": "做了",
                  "action": {"type": "intent_close",
                             "params": {"id": "int_1", "outcome": "done"}}}])
    payload = memorial.decide(mid, "done")

    assert calls == ["id=int_1|outcome=done"]
    assert payload["toast"]["type"] == "success"
    results = [e for e in _ledger_events(env.dir) if e["ev"] == "action_result"]
    assert results[0]["result"] == "Closure recorded"
    # result surfaced on the replacement card
    assert "Closure recorded" in payload["card"]["data"]["elements"][0]["text"]["content"]


def test_owner_action_requires_authenticated_memorial_surface(
    env, monkeypatch,
):
    calls = []

    class FakeAP:
        def __init__(self, **kwargs):
            self.owner_authenticated = kwargs.get(
                "owner_authenticated", False
            )

        def _do_delegation_confirm(self, raw):
            calls.append((self.owner_authenticated, raw))
            if not self.owner_authenticated:
                raise RuntimeError("owner decision required")
            return "confirmed"

    import core.actions as actions
    monkeypatch.setattr(actions, "ActionProcessor", FakeAP)
    option = [{
        "key": "confirm",
        "label": "确认",
        "action": {
            "type": "delegation_confirm",
            "params": {"id": "dlg_1", "version": "1"},
        },
    }]
    denied, _ = memorial.create("test", "Denied", "body", options=option)
    accepted, _ = memorial.create("test", "Accepted", "body", options=option)

    denied_result = memorial.decide(denied, "confirm")
    accepted_result = memorial.decide(
        accepted,
        "confirm",
        owner_authenticated=True,
    )

    assert denied_result["toast"]["type"] == "info"
    assert accepted_result["toast"]["type"] == "success"
    assert calls == [
        (False, "id=dlg_1|version=1"),
        (True, "id=dlg_1|version=1"),
    ]


def test_decide_action_failure_still_records_with_info_toast(env, monkeypatch):
    class FakeAP:
        def __init__(self, **kw):
            pass

        def _do_boom(self, raw):
            raise RuntimeError("boom")

    import core.actions as actions
    monkeypatch.setattr(actions, "ActionProcessor", FakeAP)

    mid, _ = memorial.create(
        "x", "t", "b",
        options=[{"key": "go", "label": "去",
                  "action": {"type": "boom", "params": {}}}])
    payload = memorial.decide(mid, "go")

    assert payload["toast"]["type"] == "info"
    assert "出错" in payload["toast"]["content"]
    results = [e for e in _ledger_events(env.dir) if e["ev"] == "action_result"]
    assert results[0]["result"].startswith("FAILED")
    assert memorial.get_memorial(mid)["status"] == "decided"


def test_resolve_overrides_old_reply_and_syncs_external_truth(env, monkeypatch):
    synced = []
    monkeypatch.setattr(
        memorial, "_sync_lark_card",
        lambda memorial_id, card: synced.append((memorial_id, card)))
    mid, _ = memorial.create(
        "eigenflux-friends", "好友申请", "某 Agent 请求加好友",
        options=[{"key": "accept", "label": "通过", "action": None}])
    memorial.decide(mid, "accept")

    assert memorial.resolve(
        mid, "已通过（服务端确认）", "EigenFlux 好友关系已生效") is True

    state = memorial.get_memorial(mid)
    assert state["decided_opt"] == "__external__"
    assert state["resolved_label"] == "已通过（服务端确认）"
    assert state["action_result"] == "EigenFlux 好友关系已生效"
    content = synced[-1][1]["elements"][0]["text"]["content"]
    assert "已通过（服务端确认）" in content
    assert "EigenFlux 好友关系已生效" in content
    assert memorial.resolve(
        mid, "已通过（服务端确认）", "EigenFlux 好友关系已生效") is False


def test_lark_card_sync_rejected_is_observable(monkeypatch):
    events = []
    monkeypatch.setattr(
        "core.memorial_thread.sent_message_ids", lambda _mid: ["om_fixture"]
    )
    monkeypatch.setattr(memorial, "_ops_log", lambda event, **kw: events.append((event, kw)))

    memorial._sync_lark_card(
        "mem_fixture",
        {"schema": "2.0"},
        runner=lambda *args, **kwargs: subprocess.CompletedProcess(args, 8, "", "denied"),
    )

    assert events == [("lark_card_sync_rejected", {
        "level": "warn",
        "memorial_id": "mem_fixture",
        "message_id": "om_fixture",
        "returncode": 8,
    })]


def test_lark_card_sync_failure_is_observable(monkeypatch):
    events = []
    monkeypatch.setattr(
        "core.memorial_thread.sent_message_ids", lambda _mid: ["om_fixture"]
    )
    monkeypatch.setattr(memorial, "_ops_log", lambda event, **kw: events.append((event, kw)))

    def fail(*args, **kwargs):
        raise OSError("offline")

    memorial._sync_lark_card("mem_fixture", {"schema": "2.0"}, runner=fail)

    assert events == [("lark_card_sync_failed", {
        "level": "warn",
        "memorial_id": "mem_fixture",
        "message_id": "om_fixture",
        "error_type": "OSError",
    })]


def test_lark_card_sync_prefers_keychain_independent_bot_patch(monkeypatch):
    from core.lark_bot_transport import BotSendResult

    calls = []
    monkeypatch.setattr(
        "core.memorial_thread.sent_message_ids", lambda _mid: ["om_fixture"]
    )
    monkeypatch.setattr(
        "core.lark_bot_transport.update_card",
        lambda message_id, card_json, **kwargs: (
            calls.append((message_id, json.loads(card_json), kwargs))
            or BotSendResult(True, True, message_id=message_id)
        ),
    )
    monkeypatch.setattr(
        memorial,
        "_LARK_CARD_SYNC_RUNNER",
        lambda *_args, **_kwargs: pytest.fail(
            "configured bot transport must not fall back to keychain CLI"
        ),
    )

    memorial._sync_lark_card("mem_fixture", {"schema": "2.0"})

    assert calls[0][0] == "om_fixture"
    assert calls[0][1] == {"schema": "2.0"}


def test_confirmed_thread_reply_resolves_only_its_pending_memorial(env):
    mid, _ = memorial.create("mail", "邮件标题", "正文", preset="fyi")

    assert memorial.resolve_thread_conversation(
        f"memorial:{mid}", "这件事我已经说明白了") is True
    state = memorial.get_memorial(mid)
    assert state["status"] == "decided"
    assert state["resolved_label"] == "已转入对话"
    assert state["action_result"] == "这件事我已经说明白了"
    assert memorial.resolve_thread_conversation(
        f"memorial:{mid}", "重复投递") is False
    assert memorial.resolve_thread_conversation("p2p:ordinary", "普通对话") is False
    assert memorial.resolve_thread_conversation("memorial:missing", "找不到") is False


def test_concurrent_thread_replies_claim_one_terminal_transition(env, monkeypatch):
    mid, _ = memorial.create("mail", "邮件标题", "正文", preset="fyi")
    synced = []
    handed_off = []
    monkeypatch.setattr(
        memorial, "_sync_lark_card",
        lambda memorial_id, card: synced.append((memorial_id, card)),
    )
    monkeypatch.setattr(
        memorial, "_complete_surface_handoffs", handed_off.append,
    )
    workers = 8
    ready = threading.Barrier(workers)

    def close(index):
        ready.wait()
        return memorial.resolve_thread_conversation(
            f"memorial:{mid}", f"并发回复 {index}")

    with ThreadPoolExecutor(max_workers=workers) as pool:
        outcomes = list(pool.map(close, range(workers)))

    assert outcomes.count(True) == 1
    assert outcomes.count(False) == workers - 1
    assert len([event for event in _ledger_events(env.dir)
                if event.get("id") == mid and event.get("ev") == "resolve"]) == 1
    assert [item[0] for item in synced] == [mid]
    assert handed_off == [mid]


def test_card_decision_cannot_execute_after_thread_resolution(env, monkeypatch):
    calls = []
    mid, _ = memorial.create(
        "mail", "邮件标题", "正文",
        options=[{
            "key": "publish",
            "label": "发布",
            "action": {"type": "publish", "params": {}},
        }],
    )
    stale_pending = memorial.get_memorial(mid)
    monkeypatch.setattr(
        memorial, "_execute_action",
        lambda action, **kwargs: calls.append(action) or "published",
    )

    assert memorial.resolve_thread_conversation(
        f"memorial:{mid}", "已经在对话里处理") is True
    # Reproduce a worker that read pending just before the thread worker won.
    monkeypatch.setattr(
        memorial, "get_memorial", lambda _memorial_id: dict(stale_pending))

    payload = memorial.decide(mid, "publish")

    assert calls == []
    assert "已批过" in payload["toast"]["content"]
    terminal = [event for event in _ledger_events(env.dir)
                if event.get("id") == mid
                and event.get("ev") in {"resolve", "decide"}]
    assert [event["ev"] for event in terminal] == ["resolve"]


# ── chat ─────────────────────────────────────────────────────────────────


def test_chat_sends_opener_and_injects_pending_merge(env):
    mid, _ = memorial.create("mail", "邮件标题", "正文" * 200,
                             preset="fyi", context="来自 alice 的邮件")
    payload = memorial.chat(mid)

    # 1. opener is concise and does not repeat the card body
    opener = env.texts[0][0]
    assert opener.startswith("📜 已带上「邮件标题」的背景")
    assert "正文" not in opener
    assert len(opener) < 100

    # 2. context injected into bot.sh's pending-merge channel
    pm = env.dir / "jobs" / "pending_merge.jsonl"
    entry = json.loads(pm.read_text().splitlines()[0])
    assert entry["conv_key"] == "ou_test"
    assert entry["job_id"] == f"memorial:{mid}"
    assert entry["summary"].startswith("[奏折上下文]")
    assert "邮件标题" in entry["summary"]
    assert "来自 alice" in entry["summary"]
    assert "待批" in entry["summary"]
    assert len(entry["summary"]) <= memorial.CHAT_CONTEXT_MAX_CHARS

    # 3. ledger event + replacement card keeps remaining options, drops 聊聊
    # ("sent" = REQ-118 thread-lookup event appended after delivery)
    assert [e["ev"] for e in _ledger_events(env.dir)] == [
        "create", "delivery", "sent", "chat", "chat_continuation"
    ]
    card = payload["card"]["data"]
    body = card["elements"][0]["text"]["content"]
    assert "💬 聊天中" in body
    labels = [a["text"]["content"] for a in _actions(card)]
    assert labels == ["已阅", "标为重点"]  # no 聊聊这个 button


def test_chat_injection_is_scoped_to_matching_matter(env):
    matter = create_matter("卡片所属事项")
    bind_conversation("ou_test", matter["id"], destination_id="ou_test")
    mid, _ = memorial.create(
        "mail", "事项内卡片", "正文", preset="fyi",
        matter_id=matter["id"], send=False,
    )

    memorial.chat(mid)

    entry = json.loads(
        (env.dir / "jobs" / "pending_merge.jsonl").read_text().splitlines()[0])
    assert entry["context_key"] == f"matter:{matter['id']}"


def test_unrelated_card_never_injects_into_selected_matter(env):
    selected = create_matter("当前正在做")
    unrelated = create_matter("另一件事")
    bind_conversation("ou_test", selected["id"], destination_id="ou_test")
    mid, _ = memorial.create(
        "mail", "别的事项卡片", "正文", preset="fyi",
        matter_id=unrelated["id"], send=False,
    )

    memorial.chat(mid)

    entry = json.loads(
        (env.dir / "jobs" / "pending_merge.jsonl").read_text().splitlines()[0])
    assert entry["context_key"] == "conversation:ou_test"


def test_pending_context_db_failure_is_visible_and_fails_to_unbound_scope(
        env, monkeypatch):
    events = []

    def fail(*_args, **_kwargs):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr("core.conversation_context.context_snapshot", fail)
    monkeypatch.setattr(
        memorial,
        "_ops_log",
        lambda message, **fields: events.append((message, fields)),
    )

    assert memorial._pending_context_key(
        "ou_test", {"matter_id": "matter-private"}
    ) == "conversation:ou_test"
    assert events == [(
        "pending_context_lookup_failed",
        {
            "level": "warn",
            "has_matter": True,
            "error_type": "RuntimeError",
        },
    )]


def test_chat_on_a_clipped_card_sends_the_full_text(env):
    """He taps 聊聊这个 BECAUSE the card was cut. The opener must carry the
    missing text — loading context for the model alone told him nothing
    (2026-08-11: "我只能看到一堆截断")."""
    body = "\n".join(f"第{i}件事，细节在这里" for i in range(30))
    mid, _ = memorial.create("memorial-escrow", "待批 14 件", body,
                             preset="fyi", context="来自晨间台账")
    card_body = json.loads(memorial.card_json(mid))["elements"][0]["text"]["content"]
    assert memorial.CLIP_NOTICE in card_body      # precondition: it WAS clipped
    assert "第29件事" not in card_body

    memorial.chat(mid)
    opener = env.texts[0][0]
    assert "全文" in opener
    assert "第0件事" in opener and "第29件事" in opener   # nothing dropped
    assert "来自晨间台账" in opener                       # source text stays literal
    assert len(opener) <= memorial.FULL_TEXT_MAX_CHARS


def test_chat_on_a_short_card_keeps_the_concise_opener(env):
    """Un-clipped cards must not have their body pasted back at him."""
    mid, _ = memorial.create("mail", "短卡", "一句话就说完了", preset="fyi")
    memorial.chat(mid)
    opener = env.texts[0][0]
    assert opener.startswith("📜 已带上「短卡」的背景")
    assert "一句话就说完了" not in opener


def test_clipped_opener_announces_the_cut_and_keeps_the_background(env):
    """A message headed「全文」must never be silently cut — a silent cut IS
    the 2026-08-11 complaint. Over FULL_TEXT_MAX_CHARS the remainder is
    announced with a follow-up offer, and 背景 still rides along (it has its
    own budget instead of being appended last and amputated first)."""
    body = "\n".join(f"第{i}段，" + "内容细节" * 20 for i in range(80))
    assert len(body) > memorial.FULL_TEXT_MAX_CHARS
    mid, _ = memorial.create("mail", "超长邮件", body, preset="fyi",
                             context="来自晨间台账")
    memorial.chat(mid)
    opener = env.texts[0][0]
    assert "原文还有约" in opener and "继续发" in opener
    assert "—— 背景 ——" in opener and "来自晨间台账" in opener
    # honest bound: body part capped, plus the announcement and background
    assert len(opener) <= (memorial.FULL_TEXT_MAX_CHARS
                           + memorial.CHAT_OPENER_CONTEXT_MAX + 200)


def test_clipped_opener_continues_until_the_full_body_is_delivered(env):
    body = "\n".join(f"段{i:03d}:" + (chr(65 + i % 26) * 90)
                     for i in range(120))
    mid, _ = memorial.create("mail", "需要续传的长文", body, preset="fyi")

    memorial.chat(mid)
    opener = env.texts[0][0]
    assert "段000:" in opener and "继续发" in opener

    replies = []
    for _ in range(10):
        result = memorial.continue_chat_body("ou_test")
        if not result["handled"]:
            break
        replies.append(result)
        assert memorial.commit_chat_continuation(
            "ou_test", result["state_conv_key"], result["memorial_id"],
            result["expected_offset"], result["next_offset"])
        if result["remaining_chars"] == 0:
            break

    assert replies
    assert replies[-1]["remaining_chars"] == 0
    assert "原文已发完" in replies[-1]["reply"]
    assert "段119:" in replies[-1]["reply"]
    assert memorial.continue_chat_body("ou_test")["handled"] is False

    continuation_events = [e for e in _ledger_events(env.dir)
                           if e["ev"] == "chat_continuation"]
    offsets = [e["offset"] for e in continuation_events]
    assert offsets == sorted(set(offsets))
    assert continuation_events[-1]["done"] is True

    pending = [json.loads(line) for line in
               (env.dir / "jobs" / "pending_merge.jsonl").read_text().splitlines()]
    continuation_jobs = [row for row in pending
                         if row["job_id"].startswith("memorial-continuation:")]
    assert len(continuation_jobs) == len(replies)
    assert len({row["job_id"] for row in continuation_jobs}) == len(replies)


def test_new_short_chat_supersedes_an_old_continuation(env):
    long_body = "\n".join(f"旧文{i}:" + "很长" * 80 for i in range(80))
    old_mid, _ = memorial.create("mail", "旧长文", long_body, preset="fyi")
    memorial.chat(old_mid)
    assert memorial._latest_chat_continuation(["ou_test"])["done"] is False

    new_mid, _ = memorial.create("mail", "新短文", "已经说完", preset="fyi")
    memorial.chat(new_mid)

    latest = memorial._latest_chat_continuation(["ou_test"])
    assert latest["id"] == new_mid and latest["done"] is True
    assert memorial.continue_chat_body("ou_test")["handled"] is False


def test_continuation_survives_lark_chat_and_thread_routing_keys(env):
    body = "\n".join(f"线程正文{i}:" + "内容" * 100 for i in range(80))
    mid, _ = memorial.create("mail", "线程长文", body, preset="fyi",
                             chat_id="oc_direct_chat")
    memorial.chat(mid)

    result = memorial.continue_chat_body(
        f"memorial:{mid}", lookup_keys=["oc_direct_chat"], memorial_id=mid)

    assert result["handled"] is True
    assert memorial.commit_chat_continuation(
        f"memorial:{mid}", result["state_conv_key"], result["memorial_id"],
        result["expected_offset"], result["next_offset"])
    pending = [json.loads(line) for line in
               (env.dir / "jobs" / "pending_merge.jsonl").read_text().splitlines()]
    continuation = [row for row in pending
                    if row["job_id"].startswith("memorial-continuation:")]
    assert continuation[-1]["conv_key"] == f"memorial:{mid}"


def test_continuation_does_not_advance_before_delivery_commit(env):
    body = "\n".join(f"可靠续文{i}:" + "正文" * 100 for i in range(80))
    mid, _ = memorial.create("mail", "发送失败也不能丢", body, preset="fyi")
    memorial.chat(mid)
    before = memorial._latest_chat_continuation(["ou_test"])

    first = memorial.continue_chat_body("ou_test")
    retry = memorial.continue_chat_body("ou_test")

    assert first["reply"] == retry["reply"]
    assert memorial._latest_chat_continuation(["ou_test"])["offset"] == before["offset"]
    pending = [json.loads(line) for line in
               (env.dir / "jobs" / "pending_merge.jsonl").read_text().splitlines()]
    assert not any(row["job_id"].startswith("memorial-continuation:")
                   for row in pending)

    assert memorial.commit_chat_continuation(
        "ou_test", first["state_conv_key"], mid,
        first["expected_offset"], first["next_offset"])
    assert memorial._latest_chat_continuation(["ou_test"])["offset"] == first["next_offset"]
    assert memorial.commit_chat_continuation(
        "ou_test", first["state_conv_key"], mid,
        first["expected_offset"], first["next_offset"]) is False


def test_failed_opener_restarts_continuation_from_the_beginning(env, monkeypatch):
    body = "\n".join(f"首段{i}:" + "正文" * 100 for i in range(80))
    mid, _ = memorial.create("mail", "首段也不能丢", body, preset="fyi")
    monkeypatch.setattr(memorial, "_send_opener_async",
                        lambda text, chat_id, continuation=None:
                        memorial._finish_opener_continuation(
                            continuation or {}, delivered=False))

    memorial.chat(mid)
    state = memorial._latest_chat_continuation(["ou_test"])
    assert state["offset"] == 0
    assert state["done"] is False
    assert state["awaiting_opener"] is False
    retry = memorial.continue_chat_body("ou_test")
    assert "首段0:" in retry["reply"]


def test_continue_while_opener_is_in_flight_does_not_advance(env, monkeypatch):
    body = "\n".join(f"等待{i}:" + "正文" * 100 for i in range(80))
    monkeypatch.setattr(memorial, "_send_opener_async",
                        lambda *_args, **_kwargs: None)
    mid, _ = memorial.create("mail", "正在发送", body, preset="fyi")
    memorial.chat(mid)

    waiting = memorial.continue_chat_body("ou_test")

    assert waiting["handled"] is True
    assert waiting["awaiting_opener"] is True
    assert "还在发送" in waiting["reply"]
    state = memorial._latest_chat_continuation(["ou_test"])
    assert state["offset"] == 0 and state["awaiting_opener"] is True


def test_chatting_card_on_a_clipped_body_keeps_the_chat_button(env):
    """The clipped card's CLIP_NOTICE names the 聊聊 button, and a failed
    opener send has no other retry surface — removing the button while the
    body still points at it would be a rendered dead end."""
    body = "\n".join(f"第{i}件事，细节在这里" for i in range(30))
    mid, _ = memorial.create("mail", "长卡", body, preset="fyi")
    payload = memorial.chat(mid)
    card = payload["card"]["data"]
    labels = [a["text"]["content"] for a in _actions(card)]
    assert memorial.CHAT_BUTTON_LABEL in labels


def test_clipped_card_exposes_a_dedicated_full_text_button(env):
    body = "\n".join(f"第{i}件事，细节在这里" for i in range(30))
    mid, _ = memorial.create("mail", "长卡", body, preset="fyi")

    card = json.loads(memorial.card_json(mid))
    actions = _actions(card)
    full = next(action for action in actions
                if action["text"]["content"] == memorial.FULL_TEXT_BUTTON_LABEL)

    assert full["type"] == "primary"
    assert full["value"] == {
        "action": "memorial",
        "id": mid,
        "opt": memorial.FULL_TEXT_OPT_KEY,
    }
    assert "查看全文" in card["elements"][0]["text"]["content"]
    rows = [element["actions"] for element in card["elements"]
            if element.get("tag") == "action"]
    assert [action["text"]["content"] for action in rows[0]] == [
        memorial.FULL_TEXT_BUTTON_LABEL]
    visible = card["elements"][0]["text"]["content"]
    # Six source lines plus the compact card role stay within one ordinary
    # phone viewport, so the dedicated first action row is visible without a
    # hunt through a wall of text.
    assert len(visible.split(memorial.CLIP_NOTICE)[0].splitlines()) <= 10
    assert len(visible.split(memorial.CLIP_NOTICE)[0]) <= 600


def test_view_full_sends_every_chunk_without_more_user_input(env):
    body = "\n".join(f"自动段{i:03d}:" + (chr(65 + i % 26) * 90)
                     for i in range(120))
    mid, _ = memorial.create("mail", "一次发完的长文", body, preset="fyi")

    payload = memorial.read_full(mid)
    memorial_reader.current_thread().join(timeout=10)

    assert payload["toast"]["type"] == "success"
    assert memorial_reader.current_thread().is_alive() is False
    assert len(env.texts) > 1
    delivered = "\n".join(text for text, _chat_id in env.texts)
    assert "自动段000:" in delivered and "自动段119:" in delivered
    assert "再回一句「继续发」" not in delivered
    assert "原文已发完" in env.texts[-1][0]
    state = memorial._latest_chat_continuation(["ou_test"], memorial_id=mid)
    assert state["done"] is True and state["offset"] == len(body)
    assert not (env.dir / "jobs" / "pending_merge.jsonl").exists()

    first_transfer_count = len(env.texts)
    memorial.read_full(mid)
    memorial_reader.current_thread().join(timeout=10)
    assert len(env.texts) == first_transfer_count * 2
    transfers = {
        event.get("transfer_id")
        for event in _ledger_events(env.dir)
        if event.get("ev") == "chat_continuation"
    }
    assert len(transfers) == 2


def test_overlong_task_card_reaches_phone_reader_without_source_loss(env):
    body = "\n".join(
        f"跨层段{i:03d}：" + (chr(65 + i % 26) * 90)
        for i in range(120)
    )
    legacy = build_card("跨层长文", body, source="daily-reflect")

    rendered = memorial.adopt_card("daily-reflect", legacy)
    assert "__jarvis_full_body" not in rendered
    state = memorial.list_memorials()[0]
    assert state["body"] == body

    payload = memorial.read_full(state["id"])
    memorial_reader.current_thread().join(timeout=10)

    assert payload["toast"]["type"] == "success"
    delivered = "\n".join(text for text, _chat_id in env.texts)
    assert "跨层段000" in delivered
    assert "跨层段119" in delivered
    assert memorial._latest_chat_continuation(
        ["ou_test"], memorial_id=state["id"]
    )["done"] is True


def test_view_full_resumes_from_last_confirmed_chunk(env, monkeypatch):
    body = "\n".join(f"断点段{i:03d}:" + ("正文" * 80) for i in range(80))
    mid, _ = memorial.create("mail", "需要断点续传", body, preset="fyi")
    outcomes = iter([True, False])
    monkeypatch.setattr(
        memorial_reader,
        "_deliver_chunk",
        lambda _api, chunk, _chat_id: next(outcomes),
    )

    memorial.read_full(mid)
    memorial_reader.current_thread().join(timeout=10)
    interrupted = memorial._latest_chat_continuation(
        ["ou_test"], memorial_id=mid)

    assert interrupted["done"] is False
    assert 0 < interrupted["offset"] < len(body)

    resumed_offsets = []

    def succeed(chunk, _chat_id):
        resumed_offsets.append(chunk["expected_offset"])
        return True

    monkeypatch.setattr(
        memorial_reader,
        "_deliver_chunk",
        lambda _api, chunk, chat_id: succeed(chunk, chat_id),
    )
    memorial.read_full(mid)
    memorial_reader.current_thread().join(timeout=10)

    assert resumed_offsets[0] == interrupted["offset"]
    completed = memorial._latest_chat_continuation(
        ["ou_test"], memorial_id=mid)
    assert completed["done"] is True and completed["offset"] == len(body)


def test_view_full_ignores_duplicate_taps_while_worker_is_active(
        env, monkeypatch):
    body = "\n".join(f"并发段{i}:" + ("正文" * 80) for i in range(30))
    mid, _ = memorial.create("mail", "不要并发重发", body, preset="fyi")
    started = threading.Event()
    release = threading.Event()

    def blocked_worker(memorial_id, _conv_key, _chat_id):
        started.set()
        release.wait(timeout=5)
        memorial_reader.finish_job(memorial_id)

    monkeypatch.setattr(
        memorial_reader,
        "_run",
        lambda _api, memorial_id, conv_key, chat_id:
        blocked_worker(memorial_id, conv_key, chat_id),
    )

    first = memorial.read_full(mid)
    assert started.wait(timeout=2)
    duplicate = memorial.read_full(mid)
    release.set()
    memorial_reader.current_thread().join(timeout=5)

    assert first["toast"]["type"] == "success"
    assert duplicate["toast"]["type"] == "info"
    assert "不用重复点" in duplicate["toast"]["content"]


def test_chat_context_keeps_state_when_body_and_background_are_huge(env):
    mid, _ = memorial.create("mail", "很长的上下文", "正文" * 2000,
                             preset="fyi", context="背景" * 2000)
    memorial.chat(mid)
    entry = json.loads(
        (env.dir / "jobs" / "pending_merge.jsonl").read_text().splitlines()[0])
    summary = entry["summary"]
    assert len(summary) <= memorial.CHAT_CONTEXT_MAX_CHARS
    assert "当前状态: 待批" in summary
    assert summary.endswith("直接接住话题，不要复述卡片。")


def test_chat_after_decide_shows_status_no_buttons(env):
    mid, _ = memorial.create("mail", "t", "b", preset="fyi")
    memorial.decide(mid, "read")
    payload = memorial.chat(mid)

    entries = [json.loads(line) for line in
               (env.dir / "jobs" / "pending_merge.jsonl").read_text().splitlines()]
    entry = next(e for e in entries if e["job_id"] == f"memorial:{mid}")
    assert "已批：已阅" in entry["summary"]
    card = payload["card"]["data"]
    assert all(el.get("tag") != "action" for el in card["elements"])


def test_chat_unknown_id(env):
    payload = memorial.chat("mem_nope")
    assert payload["toast"]["type"] == "info"
    assert env.texts == []


def test_chat_group_card_uses_chat_id_as_conv_key(env):
    mid, _ = memorial.create("mail", "t", "b", preset="fyi", chat_id="oc_group1")
    memorial.chat(mid)
    assert env.texts[0][1] == "oc_group1"  # opener goes to the group
    entry = json.loads(
        (env.dir / "jobs" / "pending_merge.jsonl").read_text().splitlines()[0])
    assert entry["conv_key"] == "oc_group1"


def test_chat_retap_does_not_duplicate_opener_or_injection(env):
    mid, _ = memorial.create("mail", "t", "b", preset="fyi")
    memorial.chat(mid)
    payload = memorial.chat(mid)

    assert payload["toast"]["type"] == "info"
    assert len(env.texts) == 1
    pending = (env.dir / "jobs" / "pending_merge.jsonl").read_text().splitlines()
    assert len(pending) == 1
    assert len([e for e in _ledger_events(env.dir) if e["ev"] == "chat"]) == 1


# ── ledger fold / list ───────────────────────────────────────────────────


def test_list_memorials_and_pending_filter(env):
    m1, _ = memorial.create("mail", "一", "b", preset="fyi")
    m2, _ = memorial.create("mail", "二", "b", preset="fyi")
    memorial.decide(m1, "read")

    assert {s["id"] for s in memorial.list_memorials()} == {m1, m2}
    pending = memorial.list_memorials(pending_only=True)
    assert [s["id"] for s in pending] == [m2]


def test_cancelled_intent_convergence_resolves_only_linked_pending_cards(env):
    linked = [{
        "key": "done", "label": "做了",
        "action": {"type": "intent_close", "params": {"id": "int_stop"}},
    }]
    other = [{
        "key": "done", "label": "做了",
        "action": {"type": "intent_close", "params": {"id": "int_keep"}},
    }]
    first, _ = memorial.create("intentions", "旧催办 1", "body", options=linked)
    second, _ = memorial.create("intentions", "旧催办 2", "body", options=linked)
    already_done, _ = memorial.create(
        "intentions", "已处理", "body", options=linked)
    unrelated, _ = memorial.create("intentions", "别的提醒", "body", options=other)
    memorial.resolve(already_done, "用户已处理")
    cards_before = len(env.cards)

    resolved = memorial.resolve_cancelled_intent_memorials(
        "int_stop", root=env.dir, reason="功能已关闭")

    assert resolved == [first, second]
    assert memorial.get_memorial(first)["decided_label"] == "已停止追踪"
    assert memorial.get_memorial(second)["action_result"] == "功能已关闭"
    assert memorial.get_memorial(already_done)["decided_label"] == "用户已处理"
    assert memorial.get_memorial(unrelated)["status"] == "pending"
    assert len(env.cards) == cards_before  # no bulk Lark edits or sends


def test_local_resolve_can_close_orphan_without_editing_lark(env):
    mid, _ = memorial.create("intentions", "orphan", "body", preset="decision")
    cards_before = len(env.cards)

    assert memorial.resolve(
        mid, "已停止追踪", "功能已关闭", sync_lark=False) is True

    assert memorial.get_memorial(mid)["status"] == "decided"
    assert len(env.cards) == cards_before


def test_fold_skips_malformed_ledger_lines(env):
    mid, _ = memorial.create("mail", "t", "b", preset="fyi")
    with open(env.dir / "memorials.jsonl", "a") as f:
        f.write("not json at all\n")
    assert memorial.get_memorial(mid)["status"] == "pending"


def test_ids_are_unique(env):
    ids = {memorial.create("x", "t", f"b{i}", preset="fyi")[0]
           for i in range(5)}
    assert len(ids) == 5


def test_adopt_readonly_card_preserves_link_and_adds_fyi_chat(env):
    legacy = build_card("📡 EigenFlux", "一件外部动态",
                        buttons=[{"text": "阅读原文",
                                  "url": "https://example.com/a"}])
    adopted = json.loads(memorial.adopt_card("eigenflux-feed-triage", legacy))
    rows = _action_rows(adopted)
    assert [[a["text"]["content"] for a in row] for row in rows] == [
        ["已阅", "标为重点"], ["阅读原文"], ["💬 聊聊这个", "🤔 看不懂"]]
    actions = _actions(adopted)
    assert next(a for a in actions if a["text"]["content"] == "阅读原文")["url"] == "https://example.com/a"


def test_adopt_action_card_preserves_native_choice_and_adds_chat_only(env):
    legacy = build_card(
        "🎯 Intent", "这件事做了吗？",
        buttons=[{"text": "做了", "value": {
            "action": "intent_close", "id": "int_1", "outcome": "done"}}])
    adopted = json.loads(memorial.adopt_card("intention-check", legacy))
    actions = _actions(adopted)
    assert [a["text"]["content"] for a in actions] == ["做了", "💬 聊聊这个", "🤔 看不懂"]
    assert actions[0]["value"]["action"] == "intent_close"


def test_strict_native_card_requires_structured_work_receipt(env):
    legacy = build_card("📡 EigenFlux", "一件外部动态")

    assert memorial.adopt_card(
        "eigenflux-feed-triage", legacy, require_work_receipt=True,
    ) == ""
    assert memorial.list_memorials() == []


def test_strict_native_card_persists_its_producers_work_receipt(env):
    legacy = build_card(
        "📡 EigenFlux", "一件外部动态",
        work_receipt="拉取信号、核验来源并完成重复性筛选",
    )

    rendered = memorial.adopt_card(
        "eigenflux-feed-triage", legacy, require_work_receipt=True,
    )

    state = memorial.list_memorials()[0]
    assert state["work_receipt"] == "拉取信号、核验来源并完成重复性筛选"
    assert "__jarvis_work_receipt" not in rendered
    assert "**已完成：**" not in rendered
    assert "一件外部动态" in rendered


def test_strict_multisection_native_card_keeps_receipt_on_each_split(env):
    legacy = json.loads(build_card(
        "📋 汇总", "第一件事",
        work_receipt="读取两组证据并分别完成核验",
    ))
    legacy["elements"].append({
        "tag": "div",
        "text": {"tag": "lark_md", "content": "第二件事"},
    })

    rendered = memorial.adopt_card(
        "heartbeat", json.dumps(legacy, ensure_ascii=False),
        require_work_receipt=True,
    )

    assert len(rendered.splitlines()) == 2
    assert [row["work_receipt"] for row in memorial.list_memorials()] == [
        "读取两组证据并分别完成核验",
        "读取两组证据并分别完成核验",
    ]


def test_proactive_work_receipt_is_required_and_stored_but_not_rendered(env):
    missing = memorial.memorialize_output(
        "TITLE: 需要拍板\n方案已经列好。\nOPTIONS: 同意 | 不做",
        "intention-check",
        require_work_receipt=True,
    )
    assert missing == ""
    assert memorial.list_memorials() == []

    rendered = memorial.memorialize_output(
        "TITLE: 需要拍板\n"
        "WORKED: 对照了三份记录并复算影响范围\n"
        "方案已经列好。\nOPTIONS: 同意 | 不做",
        "intention-check",
        require_work_receipt=True,
    )

    state = memorial.list_memorials()[0]
    assert state["work_receipt"] == "对照了三份记录并复算影响范围"
    assert "WORKED" not in state["body"]
    card_body = json.loads(rendered)["elements"][0]["text"]["content"]
    assert "**已完成：**" not in card_body
    assert "方案已经列好" in card_body


def test_quoted_worked_example_does_not_satisfy_receipt_gate(env):
    rendered = memorial.memorialize_output(
        "TITLE: 协议示例\n> WORKED: 这只是外部引文\n正文",
        "intention-check",
        require_work_receipt=True,
    )
    assert rendered == ""
    assert memorial.list_memorials() == []


def test_malformed_worked_directive_never_leaks_as_body_text(env):
    memorial.memorialize_output(
        "TITLE: 兼容输入\nWORKED:\n正文",
        "intention-check",
    )
    state = memorial.list_memorials()[0]
    assert state["body"] == "正文"


def test_title_only_card_with_work_receipt_keeps_title_as_body(env):
    rendered = memorial.memorialize_output(
        "TITLE: 检查完成\nWORKED: 跑完组件检查，17 项全部健康",
        "selfmon",
        require_work_receipt=True,
    )
    state = memorial.list_memorials()[0]
    assert state["title"] == "检查完成"
    assert state["body"] == "检查完成"
    assert json.loads(rendered)["elements"]


def test_content_dedup_distinguishes_new_work_receipt(env):
    first, _ = memorial.create(
        "mail", "同一标题", "同一正文", preset="fyi",
        work_receipt="只读了标题",
    )
    second, _ = memorial.create(
        "mail", "同一标题", "同一正文", preset="fyi",
        work_receipt="读完正文并核验发件人",
    )
    assert first != second


def test_memorialize_output_keeps_ambient_prose_ledger_only(env):
    output = "跨 Session 有一件进展\n---\n另一件独立进展"
    rendered = memorial.memorialize_output(output, "cross-session-sync")
    assert rendered == ""
    states = memorial.list_memorials()
    assert [state["body"] for state in states] == [
        "跨 Session 有一件进展", "另一件独立进展"]
    assert all(state["attention"] == "notice" for state in states)
    assert all(state["delivery_status"] == "ledger_only" for state in states)


def test_ambient_prose_cannot_self_promote_to_alert_with_risk_words(env):
    """Ambient model prose is untrusted exhaust, not an alert authority.

    A cross-session digest containing words such as ``风险`` used to trip the
    generic alert heuristic, enter the Lark queue, and wait to nag the owner the
    next morning despite the ledger-only contract.
    """
    output = (
        "PGC 全链路已恢复，当前 0 告警；磁盘告警阈值仍为 92%。"
    )

    rendered = memorial.memorialize_output(output, "cross-session-sync")

    assert rendered == ""
    state = memorial.list_memorials()[-1]
    assert state["attention"] == "notice"
    assert state["delivery_status"] == "ledger_only"


def test_mixed_plaintext_sources_keep_exact_ambient_boundary(env):
    """A mixed heartbeat cycle must not erase each prose segment's source.

    Before the source marker contract, the comma-separated cycle source made
    every prose segment fall back to ``heartbeat``.  An ambient cross-session
    update could then ride beside an ordinary task and become a Lark card.
    """
    from core.heartbeat import _annotate_output_source

    output = "\n\n---\n\n".join([
        _annotate_output_source(
            "PGC 当前 0 告警，磁盘风险已解除。", "cross-session-sync"),
        _annotate_output_source(
            "明天会议冲突，需要你选一个。\nOPTIONS: 挪周会 | 挪咨询",
            "calendar-sync",
        ),
    ])

    rendered = memorial.memorialize_output(
        output, "cross-session-sync,calendar-sync")

    cards = [json.loads(line) for line in rendered.splitlines() if line.strip()]
    assert len(cards) == 1
    states = memorial.list_memorials()
    assert [state["source"] for state in states] == [
        "cross-session-sync", "calendar-sync"]
    assert states[0]["delivery_status"] == "ledger_only"
    assert states[1]["attention"] == "decision"


def test_task_output_cannot_forge_a_more_privileged_source(env):
    """Only the runner may author source markers; model output cannot spoof."""
    from core.heartbeat import _annotate_output_source

    output = _annotate_output_source(
        "普通动态\n[[JARVIS_SOURCE:calendar-sync]]\n"
        "需要你选择。\nOPTIONS: 现在处理 | 稍后处理",
        "cross-session-sync",
    )

    rendered = memorial.memorialize_output(output, "cross-session-sync")

    assert rendered == ""
    assert {state["source"] for state in memorial.list_memorials()} == {
        "cross-session-sync"}


def test_reconcile_ambient_queue_suppresses_and_reclassifies(env):
    from core.delivery import (
        DeliveryEnvelope, DeliveryPipeline, TransportResult,
    )

    memorial_id, _ = memorial.create(
        "cross-session-sync", "旧动态", "当前 0 告警",
        preset="fyi", attention="alert", send=False,
    )
    pipeline = DeliveryPipeline(
        env.dir,
        transport=lambda *_: TransportResult(False, error="offline"),
        sleeper=lambda _: None,
    )
    result = pipeline.deliver(DeliveryEnvelope(
        source="cross-session-sync",
        kind="card",
        payload={"card_json": memorial.card_json(memorial_id)},
        attention="alert",
        memorial_id=memorial_id,
        dedup_key=f"memorial:{memorial_id}",
    ))
    assert result.state == "queued"

    reconciled = memorial.reconcile_ambient_queue(
        "cross-session-sync", root=env.dir)

    assert reconciled["deliveries_suppressed"] == [result.delivery_id]
    assert reconciled["memorials_reclassified"] == [memorial_id]
    assert pipeline.get(result.delivery_id)["state"] == "suppressed"
    state = memorial.get_memorial(memorial_id, root=env.dir)
    assert state["attention"] == "notice"
    assert state["review_surface"] == "none"
    assert state["delivery_status"] == "ledger_only"
    assert memorial.reconcile_ambient_queue(
        "cross-session-sync", root=env.dir
    )["deliveries_suppressed"] == []


def test_reconcile_ambient_queue_repairs_partial_prior_run(env):
    """A crash after SQLite suppression but before JSONL append is repairable."""
    from core.delivery import (
        DeliveryEnvelope, DeliveryPipeline, TransportResult,
    )

    memorial_id, _ = memorial.create(
        "cross-session-sync", "旧动态", "磁盘风险",
        preset="fyi", attention="alert", send=False,
    )
    pipeline = DeliveryPipeline(
        env.dir,
        transport=lambda *_: TransportResult(False, error="offline"),
        sleeper=lambda _: None,
    )
    queued = pipeline.deliver(DeliveryEnvelope(
        source="cross-session-sync",
        kind="card",
        payload={"card_json": memorial.card_json(memorial_id)},
        attention="alert",
        memorial_id=memorial_id,
        dedup_key=f"memorial:{memorial_id}",
    ))
    assert pipeline.suppress_queued_source(
        "cross-session-sync", reason="ambient_ledger_only",
    ) == [queued.delivery_id]
    assert memorial.get_memorial(memorial_id, root=env.dir)["attention"] == "alert"

    repaired = memorial.reconcile_ambient_queue(
        "cross-session-sync", root=env.dir)

    assert repaired["deliveries_suppressed"] == []
    assert repaired["memorials_reclassified"] == [memorial_id]
    state = memorial.get_memorial(memorial_id, root=env.dir)
    assert state["attention"] == "notice"
    assert state["delivery_status"] == "ledger_only"


def test_memorialize_output_renders_ordinary_choices_for_lark(env):
    rendered = memorial.memorialize_output(
        "要采用哪条路径？\nOPTIONS: 路径 A | 路径 B",
        "heartbeat",
    )
    card = json.loads(rendered)
    assert [action["text"]["content"] for action in _actions(card)] == [
        "路径 A", "路径 B", "💬 聊聊这个", "🤔 看不懂"]
    state = memorial.list_memorials()[-1]
    assert state["attention"] == "decision"
    assert memorial.review_surface(state) == "lark"


def test_cross_session_options_stay_ambient_notice(env):
    rendered = memorial.memorialize_output(
        "另一个执行会话有个建议\nOPTIONS: 去核实 | 先不管",
        "cross-session-sync",
    )
    assert rendered == ""
    state = memorial.list_memorials()[-1]
    assert state["attention"] == "notice"
    assert memorial.requires_decision(state) is False


def test_explicit_title_line_becomes_card_header(env):
    output = "TITLE: 发声候选已备好，挑一个\n三个候选：A、B、C，各配 open problem。"
    rendered = memorial.memorialize_output(output, "intention-check")
    card = json.loads(rendered)
    assert card["header"]["title"]["content"] == "📜 🎯 发声候选已备好，挑一个"
    body = card["elements"][0]["text"]["content"]
    assert body.startswith("ℹ️ 知道就行")
    assert "TITLE" not in body and "三个候选" in body


def test_short_first_line_promoted_to_title(env):
    output = "**周会冲突提醒**\n周四 9:00 的周会和心理咨询撞了，需要挪一个。"
    rendered = memorial.memorialize_output(output, "intention-check")
    card = json.loads(rendered)
    assert card["header"]["title"]["content"] == "📜 🎯 周会冲突提醒"
    # markdown heading was dropped from the body — no double-say
    assert "周会冲突提醒" not in card["elements"][0]["text"]["content"]


def test_title_only_output_still_makes_a_card(env):
    out = memorial.memorialize_output("TITLE: 今晚 EF 增长破千，值得看一眼",
                                      "intention-check")
    card = json.loads(out)
    assert card["header"]["title"]["content"] == "📜 🎯 今晚 EF 增长破千，值得看一眼"
    assert card["elements"][0]["text"]["content"] == (
        "ℹ️ 知道就行\n\n今晚 EF 增长破千，值得看一眼")


def test_overlong_explicit_title_clipped_but_body_keeps_full_line(env):
    long_title = "这是一个远超四十个字符上限的超长标题" * 3
    out = memorial.memorialize_output(f"TITLE: {long_title}\n正文在此",
                                      "intention-check")
    card = json.loads(out)
    assert card["header"]["title"]["content"] == f"📜 🎯 {long_title[:40]}"
    body = card["elements"][0]["text"]["content"]
    assert long_title in body and "正文在此" in body


def test_asymmetric_markup_first_line_keeps_clean_title_and_body(env):
    out = memorial.memorialize_output("【提醒】周四 9:00 周会和咨询撞了\n需要挪一个。",
                                      "intention-check")
    card = json.loads(out)
    assert card["header"]["title"]["content"] == "📜 🎯 【提醒】周四 9:00 周会和咨询撞了"
    # prose first line (not pure markup) stays in the body
    assert "【提醒】" in card["elements"][0]["text"]["content"]
    out = memorial.memorialize_output("**紧急**：磁盘只剩 3G\n清理一下下载目录。",
                                      "heartbeat")
    card = json.loads(out)
    assert card["header"]["title"]["content"] == "📜 🫀 紧急：磁盘只剩 3G"
    assert "**" not in card["header"]["title"]["content"]


def test_rotate_abort_then_retry_does_not_duplicate_archive(env, monkeypatch):
    import time as _t
    now = _t.time()
    _seed_ledger_card(env, "mem_old_dup", int(now - 60 * 86400))
    # first attempt: swap blows up AFTER verification → nothing archived
    real_replace = memorial.os.replace
    monkeypatch.setattr(memorial.os, "replace",
                        lambda *a, **kw: (_ for _ in ()).throw(OSError("boom")))
    assert memorial.rotate_ledger(now=now) == 0
    month = _t.strftime("%Y-%m", _t.localtime(now - 60 * 86400))
    assert not (env.dir / f"memorials.{month}.jsonl").exists()
    # retry succeeds; archive has exactly one copy of the group
    monkeypatch.setattr(memorial.os, "replace", real_replace)
    assert memorial.rotate_ledger(now=now) == 1
    archived = (env.dir / f"memorials.{month}.jsonl").read_text().splitlines()
    assert len([l for l in archived if '"ev": "create"' in l]) == 1


def test_prose_without_headline_keeps_generic_source_title(env):
    output = ("这是一段没有标题、首行也很长很长很长很长很长很长很长很长很长很长"
              "很长很长很长的正文\n第二行内容")
    rendered = memorial.memorialize_output(output, "intention-check")
    card = json.loads(rendered)
    # Fallback titles are Chinese since 2026-08-24 (the「Intent」codename
    # shipped on 10 cards in 14d).
    assert card["header"]["title"]["content"] == "📜 🎯 定时提醒"


def test_memorialize_output_does_not_double_wrap_memorial(env):
    mid, _ = memorial.create("mail", "邮件", "正文", preset="fyi", send=False)
    card = memorial.card_json(mid)
    # An existing memorial card passes through for Lark delivery — it is not
    # re-created, and the pass-through keeps the SAME memorial id.
    out = memorial.memorialize_output(card, "mail-triage")
    assert f'"id": "{mid}"' in out or f'"id":"{mid}"' in out
    assert memorial.get_memorial(mid)["attention"] == "notice"
    assert len([e for e in _ledger_events(env.dir) if e["ev"] == "create"]) == 1


def test_decision_on_sentinel_suppressed_body_returns_safe_card(env):
    mid, _ = memorial.create(
        "test", "内部静默残留", "分析草稿\nHEARTBEAT_OK",
        preset="decision", send=False,
    )
    assert memorial.card_json(mid) == ""

    payload = memorial.decide(mid, "approve")

    card = payload["card"]["data"]
    encoded = json.dumps(card, ensure_ascii=False)
    assert card["header"]["title"]["content"] == "Jarvis · 事项"
    assert "状态已更新" in encoded
    assert "HEARTBEAT_OK" not in encoded


def test_memorialize_output_suppresses_already_delivered_legacy_card(env):
    legacy = build_card("📡 EigenFlux", "同一条动态")
    first = memorial.memorialize_output(legacy, "eigenflux-feed-triage")
    assert first != ""  # curated signal renders for Lark
    mid = memorial.list_memorials()[-1]["id"]
    assert memorial.get_memorial(mid)["attention"] == "notice"
    # Once the transport recorded a delivery, the same content is suppressed —
    # not re-rendered into a second card.
    memorial._record_delivery(mid, "delivered")
    assert memorial.memorialize_output(legacy, "eigenflux-feed-triage") == ""


def test_duplicate_delivered_memorial_is_not_resent(env):
    first, sent = memorial.create(
        "x", "t", "b", preset="decision", review_at="lark")
    second, resent = memorial.create(
        "x", "t", "b", preset="decision", review_at="lark")

    assert sent is True and resent is True
    assert second == first
    assert len(env.cards) == 1


def test_explicit_dedup_key_ignores_reworded_pending_card(env):
    first, _ = memorial.create(
        "eigenflux-friends", "好友申请", "第一次风险说明",
        preset="decision", dedup_key="eigenflux-friend:123",
        review_at="lark")
    second, _ = memorial.create(
        "eigenflux-friends", "好友申请", "模型换了一种风险说明",
        preset="decision", dedup_key="eigenflux-friend:123",
        review_at="lark")

    assert second == first
    assert len(env.cards) == 1
    creates = [e for e in _ledger_events(env.dir) if e["ev"] == "create"]
    assert len(creates) == 1
    assert creates[0]["dedup_key"] == "eigenflux-friend:123"


def test_same_body_different_native_action_is_not_deduped(env):
    first = build_card("🎯 Intent", "做了吗？", buttons=[{"text": "做了", "value": {
        "action": "intent_close", "id": "int_1", "outcome": "done"}}])
    second = build_card("🎯 Intent", "做了吗？", buttons=[{"text": "做了", "value": {
        "action": "intent_close", "id": "int_2", "outcome": "done"}}])
    card1 = json.loads(memorial.adopt_card("intention-check", first))
    card2 = json.loads(memorial.adopt_card("intention-check", second))
    ids = [_actions(card)[-1]["value"]["id"]
           for card in (card1, card2)]
    assert ids[0] != ids[1]


def test_duplicate_failed_memorial_reuses_durable_queue_entry(env):
    env.send_ok = False
    first, sent = memorial.create(
        "x", "t", "b", preset="decision", review_at="lark")
    env.send_ok = True
    second, resent = memorial.create(
        "x", "t", "b", preset="decision", review_at="lark")

    assert sent is False and resent is True
    assert second == first
    assert len([e for e in _ledger_events(env.dir) if e["ev"] == "create"]) == 1
    assert memorial.get_memorial(first)["delivery_status"] == "retry_queued"
    from core.delivery import DeliveryPipeline
    assert len(DeliveryPipeline(env.dir).list(state="queued")) == 1
    assert not (env.dir / memorial.MEMORIAL_QUEUE_FILE).exists()


def test_quiet_hours_queue_records_delivery_without_direct_send(env, monkeypatch):
    monkeypatch.setattr(memorial, "_quiet_hours_now", lambda: True)
    mid, queued = memorial.create(
        "x", "t", "b", preset="decision", review_at="lark")

    assert queued is True
    assert env.cards == []
    assert memorial.get_memorial(mid)["delivery_status"] == "queued"
    from core.delivery import DeliveryPipeline
    rows = DeliveryPipeline(env.dir).list(state="queued")
    assert rows[0]["memorial_id"] == mid
    assert "card_json" in json.loads(rows[0]["payload"])
    assert not (env.dir / memorial.MEMORIAL_QUEUE_FILE).exists()
    assert not (env.dir / "night_queue.jsonl").exists()


# ── CLI ──────────────────────────────────────────────────────────────────


def test_cli_send_prints_id(env, capsys):
    rc = memorial.main(["send", "--source", "mail", "--title", "T",
                        "--body", "B", "--worked", "读完并分类邮件",
                        "--preset", "fyi"])
    assert rc == 0
    mid = capsys.readouterr().out.strip()
    assert mid.startswith("mem_")
    assert memorial.get_memorial(mid)["title"] == "T"


def test_cli_urgent_bypasses_quiet_hours(env, monkeypatch, capsys):
    monkeypatch.setattr(memorial, "_quiet_hours_now", lambda: True)
    rc = memorial.main(["send", "--source", "selfmon", "--title", "T",
                        "--body", "B", "--worked", "完成健康检查",
                        "--preset", "fyi", "--urgent"])
    assert rc == 0
    assert len(env.cards) == 1
    assert not (env.dir / memorial.MEMORIAL_QUEUE_FILE).exists()
    capsys.readouterr()


def test_cli_option_spec_parsing(env, capsys):
    rc = memorial.main([
        "send", "--source", "intent", "--title", "T", "--body", "B",
        "--worked", "核验触发与状态",
        "--option", "准=intent_close:id=xxx,outcome=done",
        "--option", "缓",
    ])
    assert rc == 0
    mid = capsys.readouterr().out.strip()
    opts = memorial.get_memorial(mid)["options"]
    assert opts[0] == {"key": "opt1", "label": "准",
                       "action": {"type": "intent_close",
                                  "params": {"id": "xxx", "outcome": "done"}}}
    assert opts[1] == {"key": "opt2", "label": "缓", "action": None}


def test_cli_send_failure_returns_1_but_prints_id(env, capsys):
    env.send_ok = False
    rc = memorial.main(["send", "--source", "mail", "--title", "T",
                        "--body", "B", "--worked", "读完并分类邮件",
                        "--preset", "fyi", "--urgent"])
    assert rc == 1
    assert capsys.readouterr().out.strip().startswith("mem_")


def test_cli_list_pending(env, capsys):
    m1, _ = memorial.create("mail", "一", "b", preset="fyi")
    m2, _ = memorial.create("mail", "二", "b", preset="fyi")
    memorial.decide(m1, "read")
    capsys.readouterr()  # drain

    rc = memorial.main(["list", "--pending"])
    assert rc == 0
    lines = [json.loads(x) for x in capsys.readouterr().out.splitlines() if x]
    assert [s["id"] for s in lines] == [m2]


def test_cli_bad_option_spec_is_usage_error(env, capsys):
    rc = memorial.main(["send", "--source", "x", "--title", "t",
                        "--body", "b", "--worked", "完成检查",
                        "--option", "  "])
    assert rc == 2
    assert "ERROR" in capsys.readouterr().err
    assert _ledger_events(env.dir) == []


def test_send_timeout_retries_and_never_claims_delivery(monkeypatch):
    calls = []

    def timeout(*args, **kwargs):
        calls.append((args, kwargs))
        raise memorial.subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(memorial.subprocess, "run", timeout)
    monkeypatch.setattr(memorial.time, "sleep", lambda _seconds: None)

    # _send returns "" on failure since REQ-118 (message_id on success) —
    # falsy is the delivery-claim contract, not the bool type.
    assert not memorial._send(["--user-id", "ou_test", "--markdown", "x"])
    assert len(calls) == 3


# ── mail triage integration (post script carrier swap) ──────────────────


def test_mail_post_prints_nonurgent_card_for_loop(tmp_path, monkeypatch, capsys):
    # The web notice stream is retired (Lark is the only delivery surface):
    # non-urgent pushed mail rides the CARD route, so the post hook must print
    # the card — carrying its memorial id — for heartbeat_loop to transport.
    import importlib.util
    root = Path(__file__).parent.parent
    monkeypatch.setenv("JARVIS_DIR", str(tmp_path))
    monkeypatch.setenv("MEMORY_DIR", str(tmp_path / "memory"))
    monkeypatch.setenv("JARVIS_EF_QUIET_OVERRIDE", "awake")
    monkeypatch.setattr(memorial, "JARVIS_DIR", tmp_path)
    spec = importlib.util.spec_from_file_location(
        "mail_triage_post_t", root / "tasks" / "mail_triage_post.py")
    post = importlib.util.module_from_spec(spec)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({
        "triage": [{"event_id": "a1", "decision": "push"}],
        "user_message": "📬 来自 X 的邮件", "urgent": False})))
    spec.loader.exec_module(post)
    assert post.main() == 0

    out = capsys.readouterr().out
    state = memorial.list_memorials()[-1]
    assert state["body"] == "📬 来自 X 的邮件"
    assert state["attention"] == "notice"
    assert state["id"] in out


# ── engagement accounting (v1.2 follow-up: memorial ↔ engagement_log) ────


def _engagement_rows(dirpath):
    p = dirpath / "engagement_log.jsonl"
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines()
            if l.strip()]


def test_direct_delivery_writes_engagement_sent_row(env):
    """A direct send (CLI / urgent) must leave the same "sent" row the
    heartbeat flush writes, so engagement-analyze stops seeing memorial
    sources as zero-output."""
    mid, ok = memorial.create(
        source="release", title="t", body="b", urgent=True)
    assert ok
    sent = [r for r in _engagement_rows(env.dir) if r["type"] == "sent"]
    assert len(sent) == 1
    assert sent[0]["source"] == "release"
    assert sent[0]["via"] == "memorial-direct"
    assert isinstance(sent[0]["epoch"], int)


def test_direct_delivery_sent_row_carries_message_ids(env):
    """engagement-analyze's delivery-ack attribution only counts sends with
    message_ids — without them every direct-sent card (all routines) read
    as "sent N, read 0" forever (2026-08-09 finding: 47 routine cards, zero
    attributable reads, while the id sat unused in result.message_id)."""
    memorial.create(source="routine:demo", title="t", body="b", urgent=True)
    sent = [r for r in _engagement_rows(env.dir) if r["type"] == "sent"]
    assert sent[0]["message_ids"] == ["om_test_fixture"]


def test_unparsed_send_placeholder_never_becomes_a_message_id(env, monkeypatch):
    """_send returns the literal "sent" when Lark's reply parses but carries
    no id; that placeholder must not pollute read-receipt joins."""
    monkeypatch.setattr(memorial, "_send_card", lambda *a, **k: "sent")
    memorial.create(source="release", title="t", body="b", urgent=True)
    sent = [r for r in _engagement_rows(env.dir) if r["type"] == "sent"]
    assert len(sent) == 1
    assert "message_ids" not in sent[0]


def test_failed_delivery_writes_no_sent_row(env):
    env.send_ok = False
    memorial.create(source="release", title="t", body="b")
    assert [r for r in _engagement_rows(env.dir) if r["type"] == "sent"] == []


def test_decide_writes_feedback_row(env):
    """批红 = engagement — same "feedback" shape the legacy buttons write."""
    mid, _ = memorial.create(source="mail", title="t", body="b")
    memorial.decide(mid, "read")
    fb = [r for r in _engagement_rows(env.dir) if r["type"] == "feedback"]
    assert len(fb) == 1
    assert fb[0]["source"] == "mail" and fb[0]["rating"] == "read"


def test_chat_writes_feedback_row(env):
    mid, _ = memorial.create(source="mail", title="t", body="b")
    memorial.chat(mid)
    fb = [r for r in _engagement_rows(env.dir) if r["type"] == "feedback"]
    assert len(fb) == 1
    assert fb[0]["rating"] == "chat"


def test_display_body_never_cuts_through_a_markdown_link():
    """A char-limit clip must not leave a dangling `[label](https://…` stub."""
    filler = "字" * 850
    link = "[官方公告](https://example.com/a-very-long-path-that-straddles-the-limit)"
    body = filler + " " + link
    out = memorial._display_body(body)
    assert memorial.CLIP_NOTICE in out
    # every link opener that survived the clip must still have its closer
    core_text = out.split(memorial.CLIP_NOTICE)[0]
    assert core_text.count("](") == core_text.count(")") or "](" not in core_text


# ── content-driven buttons (OPTIONS line + suggested replies) ────────────


def test_inline_options_become_buttons_and_leave_the_body(env):
    """A task-authored `OPTIONS:` line is the card's buttons, not its copy."""
    mid, _ = memorial.create(
        "intention-check", "NewsAPI 额度",
        "额度这周到底。\nOPTIONS: 加钱 | 限流到月底 | 让它自然停",
        authoring_protocol=True)

    st = memorial.get_memorial(mid)
    assert [o["label"] for o in st["options"]] == ["加钱", "限流到月底", "让它自然停"]
    assert all(o["reply"] for o in st["options"])
    assert "OPTIONS" not in st["body"] and st["body"] == "额度这周到底。"

    card = json.loads(memorial.card_json(mid))
    assert [a["text"]["content"] for a in _actions(card)] == [
        "加钱", "限流到月底", "让它自然停", "💬 聊聊这个", "🤔 看不懂"]


def test_inline_options_accept_chinese_label_and_fullwidth_separators(env):
    mid, _ = memorial.create(
        "heartbeat", "t", "正文\n选项：通过｜拒绝／先问清楚",
        authoring_protocol=True)
    assert [o["label"] for o in memorial.get_memorial(mid)["options"]] == [
        "通过", "拒绝", "先问清楚"]


def test_options_line_only_counts_when_trailing(env):
    """An OPTIONS mention mid-body is prose — it must not steal the buttons."""
    mid, _ = memorial.create("mail", "t", "OPTIONS: 这是正文里的一句\n后面还有话",
                             preset="fyi")
    st = memorial.get_memorial(mid)
    assert [o["key"] for o in st["options"]] == ["read", "watch"]
    assert "这是正文里的一句" in st["body"]


def test_inline_options_are_capped_and_clipped(env):
    labels = " | ".join(["选项" + str(i) for i in range(1, 8)])
    long_label = "这是一个非常非常冗长以至于手机上根本显示不下的按钮文案"
    mid, _ = memorial.create(
        "heartbeat", "t", f"正文\nOPTIONS: {long_label} | {labels}",
        authoring_protocol=True)
    opts = memorial.get_memorial(mid)["options"]
    assert len(opts) == memorial.MAX_INLINE_OPTIONS
    assert len(opts[0]["label"]) == memorial.MAX_OPTION_LABEL_CHARS


def test_reply_tap_is_injected_first_person_and_reads_back_as_speech(env):
    mid, _ = memorial.create("intention-check", "NewsAPI 额度",
                             "正文\nOPTIONS: 加钱 | 限流",
                             authoring_protocol=True)
    payload = memorial.decide(mid, "r1")

    assert payload["toast"]["type"] == "success"
    body = payload["card"]["data"]["elements"][0]["text"]["content"]
    assert "🗣 你回了：加钱" in body and "已批" not in body

    decision = next(json.loads(line) for line in
                    (env.dir / "jobs" / "pending_merge.jsonl").read_text().splitlines()
                    if "memorial-decision" in line)
    assert "点了推荐回复：「加钱」" in decision["summary"]
    assert "当作他刚亲口说了这句话" in decision["summary"]


def _seed_ledger_card(env, mid, epoch, decided=False):
    import time as _t
    ts = _t.strftime("%Y-%m-%d %H:%M", _t.localtime(epoch))
    events = [{"ev": "create", "id": mid, "ts": ts, "epoch": epoch,
               "source": "checkin", "title": "t", "body": "b",
               "options": [], "extra_buttons": [], "context": ""},
              {"ev": "delivery", "id": mid, "status": "delivered", "ts": ts}]
    if decided:
        events.append({"ev": "decide", "id": mid, "ts": ts,
                       "opt": "read", "label": "已阅"})
    with open(env.dir / "memorials.jsonl", "a", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")


def test_rotate_archives_old_groups_keeps_recent(env):
    import time as _t
    now = _t.time()
    _seed_ledger_card(env, "mem_old_1", int(now - 60 * 86400), decided=True)
    _seed_ledger_card(env, "mem_new_1", int(now - 2 * 86400))
    n = memorial.rotate_ledger(now=now)
    assert n == 1
    assert memorial.get_memorial("mem_old_1") is None
    st = memorial.get_memorial("mem_new_1")
    assert st is not None and st["title"] == "t"
    # the archived group is complete (create+delivery+decide) in a month file
    import time as _t2
    month = _t2.strftime("%Y-%m", _t2.localtime(now - 60 * 86400))
    archived = (env.dir / f"memorials.{month}.jsonl").read_text().splitlines()
    assert [json.loads(l)["ev"] for l in archived] == ["create", "delivery", "decide"]


def test_ledger_appends_and_rotation_share_one_flock(env):
    import fcntl, time as _t
    _seed_ledger_card(env, "mem_lock_1", int(_t.time() - 60 * 86400))
    ledger = env.dir / "memorials.jsonl"
    # while an external writer holds the lock, rotate_ledger must WAIT for
    # it (not race) — prove mutual exclusion via a non-blocking probe
    with memorial.ledger_lock(ledger):
        probe = open(ledger.parent / (ledger.name + ".lock"), "a")
        try:
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked_out = False
        except BlockingIOError:
            locked_out = True
        finally:
            probe.close()
        assert locked_out
    # lock released: both an append and a rotation proceed normally
    memorial._append_line(ledger, {"ev": "delivery", "id": "mem_lock_1",
                                   "status": "delivered", "ts": "x"})
    assert memorial.rotate_ledger() == 1


def test_rotate_noop_when_nothing_old(env):
    import time as _t
    _seed_ledger_card(env, "mem_new_2", int(_t.time() - 86400))
    assert memorial.rotate_ledger() == 0
    assert memorial.get_memorial("mem_new_2") is not None


def test_maybe_rotate_runs_once_per_month(env):
    import time as _t
    _seed_ledger_card(env, "mem_old_2", int(_t.time() - 60 * 86400))
    memorial._maybe_rotate()
    assert memorial.get_memorial("mem_old_2") is None
    # second old card in the same month: marker suppresses another pass
    _seed_ledger_card(env, "mem_old_3", int(_t.time() - 60 * 86400))
    memorial._maybe_rotate()
    assert memorial.get_memorial("mem_old_3") is not None


def test_intention_check_prose_without_authored_choices_is_a_notice(env):
    """A monitor observation is not a decision merely because of its source."""
    memorial.memorialize_output("这条 intent 到期了", source="intention-check")
    st = memorial.list_memorials()[-1]
    assert [o["key"] for o in st["options"]] == ["read", "watch"]
    assert st["attention"] == memorial.ATTENTION_NOTICE


def test_prose_cards_pick_up_an_inline_options_line(env):
    memorial.memorialize_output("额度到底了\nOPTIONS: 加钱 | 限流",
                                source="intention-check")
    st = memorial.list_memorials()[-1]
    assert [o["label"] for o in st["options"]] == ["加钱", "限流"]
    assert "OPTIONS" not in st["body"]
    assert st["attention"] == memorial.ATTENTION_DECISION


def test_exercise_week_feedback_buttons_do_not_create_a_decision(env):
    mid, _ = memorial.create(
        "exercise-week", "本周运动", "恢复得不错",
        options=[
            {"key": "ack", "label": "知道了", "reply": "知道了"},
            {"key": "more", "label": "多讲一点", "reply": "多讲一点"},
        ],
    )
    assert memorial.get_memorial(mid)["attention"] == memorial.ATTENTION_NOTICE


def test_cli_options_flag_builds_reply_buttons(env, capsys):
    rc = memorial.main(["send", "--source", "mail", "--title", "t",
                        "--body", "b", "--worked", "读完并分类邮件",
                        "--options", "加钱|限流"])
    assert rc == 0
    mid = capsys.readouterr().out.strip().splitlines()[0]
    opts = memorial.get_memorial(mid)["options"]
    assert [o["label"] for o in opts] == ["加钱", "限流"]
    assert all(o["reply"] for o in opts)


def test_suppressed_delivery_status_separates_budget_from_obsolete():
    """A cap drop still owes Pascal a mention; a stale one does not.

    2026-08-19: the wake-up backlog spent all nine budgeted slots by 13:26 and
    thirteen later cards were suppressed with `global_daily_cap`. Spelling
    that the same way as `recovery_incident_obsolete` is what let them vanish.
    """
    from core.delivery import BUDGET_CAP_REASONS

    for reason in BUDGET_CAP_REASONS:
        assert memorial.suppressed_delivery_status(reason) == "ledger_only"
    for reason in ("recovery_incident_obsolete", "recovery_item_resolved",
                   "expired_ttl", "ambient_ledger_only", "duplicate", ""):
        assert memorial.suppressed_delivery_status(reason) == "suppressed"


def test_ledger_only_is_an_accepted_delivery_status():
    """The cap-drop status must not fall outside the ledger's contract."""
    assert "ledger_only" in memorial.ACCEPTED_DELIVERY_STATUSES


# ── recent_verdicts + recurring-ask identity (2026-08-25 self-improve) ─────
# 博客稿 was asked 7 times in 6 days, 4 of them after Pascal had answered
# 「先都放着」, because every recurring intent is a fresh model call with no
# view of the ledger and no stable identity for its ask.

def test_recent_verdicts_lists_answered_and_pending_decisions(env):
    from core.memorial_verdicts import recent_verdicts
    parked, _ = memorial.create(
        "intention-check", "博客稿 三件待定", "还差三个判断",
        preset="decision", review_at="lark")
    assert memorial.lapse(parked, "先都放着")
    decided, _ = memorial.create(
        "eigenflux-publish", "广播待确认", "稿子在这",
        options=[{"key": "go", "label": "发"}, {"key": "no", "label": "不发"}],
        review_at="lark")
    assert memorial.decide(decided, "go")
    pending, _ = memorial.create(
        "intention-check", "周五饭局后闭环", "跟进了吗",
        preset="decision", review_at="lark")
    notice, _ = memorial.create("checkin", "早安", "今天晴")  # notice, not listed
    assert memorial.decide(notice, "read")

    rows = recent_verdicts()
    by_title = {r["title"]: r for r in rows}
    assert by_title["博客稿 三件待定"]["verdict"] == "先都放着"
    assert by_title["博客稿 三件待定"]["status"] == "lapsed"
    assert by_title["广播待确认"]["verdict"] == "发"
    assert by_title["周五饭局后闭环"]["status"] == "pending"
    assert "早安" not in by_title
    for r in rows:
        assert set(r) == {"title", "source", "status", "verdict", "ts"}


def test_recent_verdicts_window_and_limit(env):
    from core.memorial_verdicts import recent_verdicts
    old, _ = memorial.create(
        "intention-check", "上个月的事", "早过了",
        preset="decision", review_at="lark")
    assert memorial.lapse(old, "先都放着")
    fresh, _ = memorial.create(
        "intention-check", "今天的事", "还热着",
        preset="decision", review_at="lark")
    # Anchor on the ledger's own (fixture-pinned) clock, never the wall clock:
    # a test that mixes the two rots the day the fixture date drifts.
    from datetime import datetime as _dt
    ledger_now = _dt.strptime(memorial.now_local_str(), "%Y-%m-%d %H:%M").timestamp()
    # A clock 30 days ahead: the window (7 days) has passed both rows.
    assert recent_verdicts(now=ledger_now + 30 * 86400) == []
    # Limit keeps the newest rows.
    assert [r["title"] for r in recent_verdicts(now=ledger_now, limit=1)] == ["今天的事"]



# ── several bare ledger cards in one task output (2026-08-26) ──────────────

def test_multiple_bare_ledger_cards_all_render_under_receipt_gate(env):
    """mail-triage prints one card_json per surfaced email. 2026-08-25 18:24:
    six alerts in one run → six memorials ledgered ``not_sent``, zero
    delivered. Only the whole-output-is-one-JSON case was recognised; the
    rest was prose, and the work-receipt gate dropped the prose."""
    ids = []
    for title in ("EigenFlux PGC CI 失败", "Google 新通行密钥提醒"):
        mid, _ = memorial.create(
            source="mail", title=title, body="正文", attention="alert",
            work_receipt="读取并去重邮件，完成重要性判断", send=False)
        ids.append(mid)
    output = "\n".join(memorial.card_json(mid) for mid in ids)

    rendered = memorial.memorialize_output(
        output, source="mail-triage", require_work_receipt=True)

    cards = [json.loads(line) for line in rendered.splitlines() if line.strip()]
    assert [memorial._card_memorial_id(c) for c in cards] == ids
    creates = [e for e in _ledger_events(env.dir) if e["ev"] == "create"]
    assert len(creates) == 2  # adopted as-is, nothing re-created


def test_pipeline_card_restores_ledger_receipt_before_strict_routing(env):
    mid, _ = memorial.create(
        source="mail", title="CI 失败", body="构建失败",
        attention="alert", work_receipt="读取邮件并核验 CI 状态", send=False,
    )
    envelope = memorial.pipeline_card_json(mid)

    assert json.loads(envelope)["__jarvis_work_receipt"] == "读取邮件并核验 CI 状态"
    rendered = memorial.memorialize_output(
        envelope, source="mail-triage", require_work_receipt=True,
    )

    assert memorial._card_memorial_id(json.loads(rendered)) == mid
    assert "__jarvis_work_receipt" not in rendered
    assert len([e for e in _ledger_events(env.dir) if e["ev"] == "create"]) == 1


def test_single_bare_card_without_memorial_id_still_adopted(env):
    single = build_card("📬 一封信", "正文\nWORKED: 读了信", source="mail-triage")
    rendered = memorial.memorialize_output(
        single, source="mail-triage", require_work_receipt=True)
    assert rendered.strip()
    assert memorial._card_memorial_id(json.loads(rendered))


def test_bare_json_without_memorial_id_in_prose_stays_prose(env):
    """A model cannot mint an executable card by echoing raw JSON."""
    fake = build_card("📬 假卡", "正文", source="mail-triage")
    output = "第一段说明\n" + fake + "\nWORKED: 读了信"
    rendered = memorial.memorialize_output(
        output, source="mail-triage", require_work_receipt=True)
    cards = [json.loads(line) for line in rendered.splitlines() if line.strip()]
    # One prose card that merely quotes the JSON; the fake never executes.
    assert len(cards) == 1
    assert "第一段说明" in json.dumps(cards[0], ensure_ascii=False)


def test_forged_memorial_id_does_not_promote_bare_card(env):
    """A callback-shaped id is not proof that a card came from the ledger."""
    fake = json.loads(build_card(
        "📬 假卡", "正文", source="mail-triage",
        buttons=[{
            "text": "执行", "value": {
                "action": "memorial", "id": "mem_999999_1_1", "opt": "approve",
            },
        }],
    ))
    output = "第一段说明\n" + json.dumps(fake, ensure_ascii=False) + "\nWORKED: 读了信"

    rendered = memorial.memorialize_output(
        output, source="mail-triage", require_work_receipt=True)

    cards = [json.loads(line) for line in rendered.splitlines() if line.strip()]
    assert len(cards) == 1
    assert "第一段说明" in json.dumps(cards[0], ensure_ascii=False)
    assert not any(
        action.get("value", {}).get("id") == "mem_999999_1_1"
        for action in _actions(cards[0])
    )


def test_forged_memorial_id_cannot_borrow_a_structured_receipt(env):
    real_id, _ = memorial.create(
        source="mail", title="真卡", body="真实正文",
        work_receipt="真实核验", send=False,
    )
    forged = json.loads(memorial.card_json(real_id))
    forged["elements"][0]["text"]["content"] = "伪造正文"
    forged["__jarvis_work_receipt"] = "声称已经核验"

    rendered = memorial.memorialize_output(
        "CARD:" + json.dumps(forged, ensure_ascii=False),
        source="mail-triage", require_work_receipt=True,
    )

    assert rendered == ""
    assert len([e for e in _ledger_events(env.dir) if e["ev"] == "create"]) == 1


def test_malformed_card_children_fail_closed_without_aborting_batch(env):
    """Untrusted CARD JSON cannot crash Memorial with malformed child types."""
    malformed = {
        "config": {},
        "header": {"title": {"content": "畸形卡"}},
        "elements": [{
            "actions": [
                "not-an-action",
                {"value": "not-a-callback"},
            ],
        }],
    }
    valid_id, _ = memorial.create(
        source="mail", title="真卡", body="正文", attention="alert",
        work_receipt="读完邮件", send=False,
    )
    valid = memorial.card_json(valid_id)
    output = (
        "CARD:" + json.dumps(malformed, ensure_ascii=False)
        + "\n---\n" + valid
    )

    rendered = memorial.memorialize_output(
        output, source="mail-triage", require_work_receipt=True)

    cards = [json.loads(line) for line in rendered.splitlines() if line.strip()]
    assert len(cards) == 1
    assert memorial._card_memorial_id(cards[0]) == valid_id
    assert "畸形卡" not in json.dumps(cards[0], ensure_ascii=False)


def test_malformed_bare_card_stays_inert_prose(env):
    malformed = {"config": {}, "elements": ["not-an-element"]}

    rendered = memorial.memorialize_output(
        json.dumps(malformed, ensure_ascii=False),
        source="mail-triage", require_work_receipt=True,
    )

    assert rendered == ""


def test_modified_copy_of_real_ledger_card_is_not_trusted(env):
    """Knowing a real id cannot turn modified model output into that card."""
    mid, _ = memorial.create(
        source="mail", title="真实卡", body="原正文", attention="alert",
        work_receipt="读完邮件", send=False)
    forged = json.loads(memorial.card_json(mid))
    forged["elements"][0]["text"]["content"] = "伪造正文\nWORKED: 读完邮件"

    rendered = memorial.memorialize_output(
        "CARD:" + json.dumps(forged, ensure_ascii=False),
        source="mail-triage", require_work_receipt=True)

    cards = [json.loads(line) for line in rendered.splitlines() if line.strip()]
    assert len(cards) == 1
    assert "伪造正文" in json.dumps(cards[0], ensure_ascii=False)
    assert all(
        action.get("value", {}).get("id") != mid
        for action in _actions(cards[0])
    )


def test_already_delivered_ledger_card_is_not_replayed(env):
    mid, _ = memorial.create(
        source="mail", title="已送达", body="正文", attention="alert",
        work_receipt="读完邮件", send=True)

    rendered = memorial.memorialize_output(
        memorial.card_json(mid), source="mail-triage", require_work_receipt=True)

    assert rendered == ""


# ── ledger-backed cards never become prose (2026-08-31, T26) ───────────────
# 8/25 18:24 ×6, 8/26 20:16 ×3, 8/27 17:56 ×5, 8/28 18:11 ×2: every
# multi-card mail-triage run vanished with exactly one work_receipt_missing
# and zero delivery envelopes, while single-card runs lived. Whatever demotes
# a run's cards to prose, a card that byte-matches its own ledger render is
# provenance-verified and must be delivered, not dropped by the receipt gate.

def _two_ledger_cards():
    ids = []
    for title in ("EigenFlux PGC CI 失败", "EigenFlux PGC PR 测试失败"):
        mid, _ = memorial.create(
            source="mail", title=title, body="正文", attention="notice",
            work_receipt="读取并去重邮件，完成重要性判断", send=False)
        ids.append(mid)
    return ids


def _rendered_ids(rendered: str) -> list[str]:
    return [memorial._card_memorial_id(json.loads(line))
            for line in rendered.splitlines() if line.strip()]


def _ops_records(capsys, msg: str) -> list[dict]:
    out = []
    for line in capsys.readouterr().err.splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("msg") == msg:
            out.append(rec)
    return out


def test_ledger_cards_after_a_stray_prose_line_still_render(env, capsys):
    ids = _two_ledger_cards()
    output = "本轮 2 封邮件：\n" + "\n".join(memorial.card_json(m) for m in ids)

    rendered = memorial.memorialize_output(
        output, source="mail-triage", require_work_receipt=True)

    assert _rendered_ids(rendered) == ids
    # The stray line is still gated (and now says what shape it had).
    missing = _ops_records(capsys, "work_receipt_missing")
    assert len(missing) == 1
    assert missing[0]["line_count"] == 1
    assert missing[0]["json_lines"] == 0
    assert missing[0]["first_line_kind"] == "prose"


def test_ledger_cards_inside_an_unclosed_fence_still_render(env):
    ids = _two_ledger_cards()
    output = "```\n" + "\n".join(memorial.card_json(m) for m in ids)

    rendered = memorial.memorialize_output(
        output, source="mail-triage", require_work_receipt=True)

    assert _rendered_ids(rendered) == ids


def test_ledger_cards_behind_a_bad_envelope_are_not_swallowed(env):
    ids = _two_ledger_cards()
    output = ("散文一行\nCARD:{\"not\": \"a card\"}\n"
              + "\n".join(memorial.card_json(m) for m in ids))

    rendered = memorial.memorialize_output(
        output, source="mail-triage", require_work_receipt=True)

    assert _rendered_ids(rendered) == ids


def test_indented_ledger_card_is_rescued_from_prose(env, capsys):
    ids = _two_ledger_cards()
    output = "\n".join("    " + memorial.card_json(m) for m in ids)

    rendered = memorial.memorialize_output(
        output, source="mail-triage", require_work_receipt=True)

    assert _rendered_ids(rendered) == ids
    rescued = _ops_records(capsys, "ledger_card_rescued")
    assert rescued and rescued[0]["card_count"] == 2
    assert "正文" not in capsys.readouterr().err  # shape only, never content


def test_rescued_ledger_card_is_still_ledger_only_when_not_pushable(
        env, monkeypatch):
    ids = _two_ledger_cards()
    monkeypatch.setattr(memorial, "should_push_to_lark", lambda state: False)
    output = "前置散文\n" + "\n".join(memorial.card_json(m) for m in ids)

    rendered = memorial.memorialize_output(
        output, source="mail-triage", require_work_receipt=True)

    assert rendered == ""  # REQ-119: ledger-only stays ledger-only


def test_untrusted_card_after_prose_is_still_not_executable(env):
    """A card that does NOT byte-match its ledger render keeps the old rule:
    prose ahead of it makes it content, never a live callback."""
    ids = _two_ledger_cards()
    card = json.loads(memorial.card_json(ids[0]))
    card["header"]["title"]["content"] = "篡改过的标题"
    output = "散文一行\nCARD:" + json.dumps(card, ensure_ascii=False)

    rendered = memorial.memorialize_output(
        output, source="mail-triage", require_work_receipt=True)

    assert rendered == ""
