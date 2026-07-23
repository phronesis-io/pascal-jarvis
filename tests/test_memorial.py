"""Tests for core.memorial — the 奏折 (memorial) card framework.

Covers: create / ledger fold / decide idempotence / action execution via
ActionProcessor / chat injection into bot.sh's pending-merge channel /
CLI parsing / preset expansion / card JSON structure (button value
round-trip). All lark-cli sends are mocked — nothing real is sent.
"""

import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import core.memorial as memorial
from core.card import build_card


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
    # Most tests care about chat semantics, not thread scheduling. Keep them
    # deterministic; the dedicated async test below covers non-blocking send.
    monkeypatch.setattr(memorial, "_send_opener_async",
                        lambda text, chat_id: memorial._deliver_opener(text, chat_id))
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


def test_create_writes_ledger_sends_card_and_mirrors_outbox(env):
    mid, sent = memorial.create("mail", "测试标题", "正文内容", preset="decision")

    assert mid.startswith("mem_")
    assert sent is True
    events = _ledger_events(env.dir)
    # "sent" = REQ-118 thread-lookup event (delivered card's Lark message_id)
    assert [e["ev"] for e in events] == ["create", "delivery", "sent"]
    ev = events[0]
    assert ev["ev"] == "create" and ev["id"] == mid
    assert ev["source"] == "mail" and ev["title"] == "测试标题"
    assert [o["key"] for o in ev["options"]] == ["approve", "defer", "reject"]
    assert events[1]["status"] == "delivered"
    # direct send mirrored into the outbox so the main session knows
    outbox = (env.dir / "heartbeat_outbox.jsonl").read_text()
    assert "测试标题" in outbox and '"source": "memorial"' in outbox


def test_create_card_structure_and_button_value_round_trip(env):
    mid, _ = memorial.create("mail", "标题", "正文", preset="decision")
    card = json.loads(env.cards[0][0])

    assert card["header"]["title"]["content"] == "📜 📬 标题"
    assert card["elements"][0]["text"]["content"] == "正文"
    rows = _action_rows(card)
    assert [len(row) for row in rows] == [3, 1]  # choices, then full-row Chat
    actions = _actions(card)
    assert len(actions) == 4
    assert actions[0]["type"] == "primary"
    assert actions[-1]["text"]["content"] == "💬 聊聊这个"
    for a, opt in zip(actions, ("approve", "defer", "reject", "chat")):
        # the value dict must round-trip exactly as the sidecar will see it
        v = json.loads(json.dumps(a["value"]))
        assert v == {"action": "memorial", "id": mid, "opt": opt}


def test_create_defaults_to_fyi_preset(env):
    mid, _ = memorial.create("selfmon", "t", "b")
    st = memorial.get_memorial(mid)
    assert [o["label"] for o in st["options"]] == ["已阅", "标为重点"]


def test_card_compacts_long_body_but_ledger_keeps_full_context(env):
    body = "\n".join(f"第{i}行 " + "细节" * 80 for i in range(12))
    mid, _ = memorial.create("mail", "长邮件", body, preset="fyi")
    card_body = json.loads(env.cards[0][0])["elements"][0]["text"]["content"]
    assert len(card_body) < len(body)
    assert "完整背景可点「聊聊这个」" in card_body
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
    mid, sent = memorial.create("mail", "t", "b", preset="fyi")
    assert sent is False
    assert memorial.get_memorial(mid)["status"] == "pending"
    assert memorial.get_memorial(mid)["delivery_status"] == "retry_queued"
    # no outbox mirror for an unsent card
    assert not (env.dir / "heartbeat_outbox.jsonl").exists()
    # Exact card is queued for automatic retry; buttons are not flattened.
    queued = [json.loads(line) for line in
              (env.dir / memorial.MEMORIAL_QUEUE_FILE).read_text().splitlines()]
    assert queued[0]["memorial_id"] == mid
    assert json.loads(queued[0]["card_json"])["elements"][1]["tag"] == "action"
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
    assert [a["text"]["content"] for a in _actions(card)] == ["💬 聊聊这个"]
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
    assert len(entry["summary"]) <= 1500

    # 3. ledger event + replacement card keeps remaining options, drops 聊聊
    # ("sent" = REQ-118 thread-lookup event appended after delivery)
    assert [e["ev"] for e in _ledger_events(env.dir)] == [
        "create", "delivery", "sent", "chat"
    ]
    card = payload["card"]["data"]
    body = card["elements"][0]["text"]["content"]
    assert "💬 聊天中" in body
    labels = [a["text"]["content"] for a in _actions(card)]
    assert labels == ["已阅", "标为重点"]  # no 聊聊这个 button


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
        ["已阅", "标为重点"], ["阅读原文"], ["💬 聊聊这个"]]
    actions = _actions(adopted)
    assert next(a for a in actions if a["text"]["content"] == "阅读原文")["url"] == "https://example.com/a"


def test_adopt_action_card_preserves_native_choice_and_adds_chat_only(env):
    legacy = build_card(
        "🎯 Intent", "这件事做了吗？",
        buttons=[{"text": "做了", "value": {
            "action": "intent_close", "id": "int_1", "outcome": "done"}}])
    adopted = json.loads(memorial.adopt_card("intention-check", legacy))
    actions = _actions(adopted)
    assert [a["text"]["content"] for a in actions] == ["做了", "💬 聊聊这个"]
    assert actions[0]["value"]["action"] == "intent_close"


def test_memorialize_output_makes_one_card_per_prose_event(env):
    output = "跨 Session 有一件进展\n---\n另一件独立进展"
    rendered = memorial.memorialize_output(output, "cross-session-sync")
    cards = [json.loads(line) for line in rendered.splitlines()]
    assert len(cards) == 2
    bodies = [c["elements"][0]["text"]["content"] for c in cards]
    assert bodies == ["跨 Session 有一件进展", "另一件独立进展"]
    assert all(c["header"]["title"]["content"].startswith("📜 🧠") for c in cards)


def test_explicit_title_line_becomes_card_header(env):
    output = "TITLE: 发声候选已备好，挑一个\n三个候选：A、B、C，各配 open problem。"
    rendered = memorial.memorialize_output(output, "intention-check")
    card = json.loads(rendered)
    assert card["header"]["title"]["content"] == "📜 🎯 发声候选已备好，挑一个"
    body = card["elements"][0]["text"]["content"]
    assert "TITLE" not in body and body.startswith("三个候选")


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
    assert card["elements"][0]["text"]["content"] == "今晚 EF 增长破千，值得看一眼"


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
    assert card["header"]["title"]["content"] == "📜 🎯 Intent"


def test_memorialize_output_does_not_double_wrap_memorial(env):
    mid, _ = memorial.create("mail", "邮件", "正文", preset="fyi", send=False)
    card = memorial.card_json(mid)
    assert memorial.memorialize_output(card, "mail-triage") == card
    assert len([e for e in _ledger_events(env.dir) if e["ev"] == "create"]) == 1


def test_memorialize_output_suppresses_already_delivered_legacy_card(env):
    legacy = build_card("📡 EigenFlux", "同一条动态")
    first = memorial.memorialize_output(legacy, "eigenflux-feed-triage")
    mid = _actions(json.loads(first))[-1]["value"]["id"]
    memorial._record_delivery(mid, "delivered")
    assert memorial.memorialize_output(legacy, "eigenflux-feed-triage") == ""


def test_duplicate_delivered_memorial_is_not_resent(env):
    first, sent = memorial.create("x", "t", "b", preset="fyi")
    second, resent = memorial.create("x", "t", "b", preset="fyi")

    assert sent is True and resent is True
    assert second == first
    assert len(env.cards) == 1


def test_explicit_dedup_key_ignores_reworded_pending_card(env):
    first, _ = memorial.create(
        "eigenflux-friends", "好友申请", "第一次风险说明",
        preset="decision", dedup_key="eigenflux-friend:123")
    second, _ = memorial.create(
        "eigenflux-friends", "好友申请", "模型换了一种风险说明",
        preset="decision", dedup_key="eigenflux-friend:123")

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
    first, sent = memorial.create("x", "t", "b", preset="fyi")
    env.send_ok = True
    second, resent = memorial.create("x", "t", "b", preset="fyi")

    assert sent is False and resent is True
    assert second == first
    assert len([e for e in _ledger_events(env.dir) if e["ev"] == "create"]) == 1
    assert memorial.get_memorial(first)["delivery_status"] == "retry_queued"
    assert len((env.dir / memorial.MEMORIAL_QUEUE_FILE).read_text().splitlines()) == 1


def test_quiet_hours_queue_records_delivery_without_direct_send(env, monkeypatch):
    monkeypatch.setattr(memorial, "_quiet_hours_now", lambda: True)
    mid, queued = memorial.create("x", "t", "b", preset="fyi")

    assert queued is True
    assert env.cards == []
    assert memorial.get_memorial(mid)["delivery_status"] == "queued"
    queued_text = (env.dir / memorial.MEMORIAL_QUEUE_FILE).read_text()
    assert mid in queued_text and '"card_json"' in queued_text
    assert not (env.dir / "night_queue.jsonl").exists()


# ── CLI ──────────────────────────────────────────────────────────────────


def test_cli_send_prints_id(env, capsys):
    rc = memorial.main(["send", "--source", "mail", "--title", "T",
                        "--body", "B", "--preset", "fyi"])
    assert rc == 0
    mid = capsys.readouterr().out.strip()
    assert mid.startswith("mem_")
    assert memorial.get_memorial(mid)["title"] == "T"


def test_cli_urgent_bypasses_quiet_hours(env, monkeypatch, capsys):
    monkeypatch.setattr(memorial, "_quiet_hours_now", lambda: True)
    rc = memorial.main(["send", "--source", "selfmon", "--title", "T",
                        "--body", "B", "--preset", "fyi", "--urgent"])
    assert rc == 0
    assert len(env.cards) == 1
    assert not (env.dir / memorial.MEMORIAL_QUEUE_FILE).exists()
    capsys.readouterr()


def test_cli_option_spec_parsing(env, capsys):
    rc = memorial.main([
        "send", "--source", "intent", "--title", "T", "--body", "B",
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
                        "--body", "B", "--preset", "fyi"])
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
                        "--body", "b", "--option", "  "])
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


def test_mail_post_emits_memorial_card(tmp_path, monkeypatch, capsys):
    """mail_triage_post push path now goes through memorial.create; the
    quiet-hours / triaged bookkeeping is exercised by test_mail_triage.py —
    here we only pin the carrier swap at the module level."""
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

    card = json.loads(capsys.readouterr().out)
    assert card["header"]["title"]["content"] == "📜 📬 邮件"
    assert "来自 X 的邮件" in card["elements"][0]["text"]["content"]
    labels = [a["text"]["content"] for a in _actions(card)]
    assert labels == ["已阅", "标为重点", "💬 聊聊这个"]


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
    mid, ok = memorial.create(source="release", title="t", body="b")
    assert ok
    sent = [r for r in _engagement_rows(env.dir) if r["type"] == "sent"]
    assert len(sent) == 1
    assert sent[0]["source"] == "release"
    assert sent[0]["via"] == "memorial-direct"
    assert isinstance(sent[0]["epoch"], int)


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
    assert "…完整背景可点「聊聊这个」" in out
    # every link opener that survived the clip must still have its closer
    core_text = out.split("…完整背景可点")[0]
    assert core_text.count("](") == core_text.count(")") or "](" not in core_text


# ── content-driven buttons (OPTIONS line + suggested replies) ────────────


def test_inline_options_become_buttons_and_leave_the_body(env):
    """A task-authored `OPTIONS:` line is the card's buttons, not its copy."""
    mid, _ = memorial.create(
        "intention-check", "NewsAPI 额度",
        "额度这周到底。\nOPTIONS: 加钱 | 限流到月底 | 让它自然停")

    st = memorial.get_memorial(mid)
    assert [o["label"] for o in st["options"]] == ["加钱", "限流到月底", "让它自然停"]
    assert all(o["reply"] for o in st["options"])
    assert "OPTIONS" not in st["body"] and st["body"] == "额度这周到底。"

    card = json.loads(env.cards[0][0])
    assert [a["text"]["content"] for a in _actions(card)] == [
        "加钱", "限流到月底", "让它自然停", "💬 聊聊这个"]


def test_inline_options_accept_chinese_label_and_fullwidth_separators(env):
    mid, _ = memorial.create("mail", "t", "正文\n选项：通过｜拒绝／先问清楚")
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
    mid, _ = memorial.create("mail", "t", f"正文\nOPTIONS: {long_label} | {labels}")
    opts = memorial.get_memorial(mid)["options"]
    assert len(opts) == memorial.MAX_INLINE_OPTIONS
    assert len(opts[0]["label"]) == memorial.MAX_OPTION_LABEL_CHARS


def test_reply_tap_is_injected_first_person_and_reads_back_as_speech(env):
    mid, _ = memorial.create("intention-check", "NewsAPI 额度",
                             "正文\nOPTIONS: 加钱 | 限流")
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


def test_prose_cards_use_the_sources_natural_preset(env):
    """A follow-up source must not fall back to「已阅」."""
    memorial.memorialize_output("这条 intent 到期了", source="intention-check")
    st = memorial.list_memorials()[-1]
    assert [o["key"] for o in st["options"]] == ["done", "later", "stop"]


def test_prose_cards_pick_up_an_inline_options_line(env):
    memorial.memorialize_output("额度到底了\nOPTIONS: 加钱 | 限流",
                                source="intention-check")
    st = memorial.list_memorials()[-1]
    assert [o["label"] for o in st["options"]] == ["加钱", "限流"]
    assert "OPTIONS" not in st["body"]


def test_cli_options_flag_builds_reply_buttons(env, capsys):
    rc = memorial.main(["send", "--source", "mail", "--title", "t",
                        "--body", "b", "--options", "加钱|限流"])
    assert rc == 0
    mid = capsys.readouterr().out.strip().splitlines()[0]
    opts = memorial.get_memorial(mid)["options"]
    assert [o["label"] for o in opts] == ["加钱", "限流"]
    assert all(o["reply"] for o in opts)
