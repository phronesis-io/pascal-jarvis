"""一张卡一件事 structural enforcement (REQ-117, 2026-07-21).

The 7/15 prompt contract (458ce63) only reached LLM-authored digests; the
7/21 日程变动 card merged three 改期 lines mechanically. These tests pin the
code-level backstop: core.card_split.split_matters plus its wiring into
memorialize_output (prose + adopted legacy cards). The split must stay
conservative — false splits are worse than occasional misses — so half the
tests here assert that single-matter bodies are NOT split.
"""

import json
from types import SimpleNamespace

import pytest

import core.memorial as memorial
from core.card import build_card
from core.card_split import MAX_SPLIT_CARDS, split_matters


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated JARVIS_DIR + mocked send channels (same as test_memorial)."""
    monkeypatch.setattr(memorial, "JARVIS_DIR", tmp_path)
    rec = SimpleNamespace(dir=tmp_path, cards=[], texts=[], send_ok=True)
    monkeypatch.setattr(memorial, "_send_card",
                        lambda cj, chat_id="": rec.cards.append((cj, chat_id)) or rec.send_ok)
    monkeypatch.setattr(memorial, "_send_text",
                        lambda t, chat_id="": rec.texts.append((t, chat_id)) or rec.send_ok)
    monkeypatch.setattr(memorial, "_resolve_user_id", lambda: "ou_test")
    monkeypatch.setattr(memorial, "_quiet_hours_now", lambda: False)
    return rec


THREE_RESCHEDULES = (
    "改期：交易下一步计划 — 7/21(周二) 14:00 → 7/21(周二) 14:30\n"
    "改期：聊下近期的一些用户反馈 — 7/21(周二) 15:00 → 7/21(周二) 15:30\n"
    "改期：白皮书 + Vic 讨论 — 7/21(周二) 16:00 → 7/21(周二) 16:30")


# ── split_matters: what splits ───────────────────────────────────────────


def test_change_line_list_splits_one_per_line():
    chunks = split_matters(THREE_RESCHEDULES)
    assert len(chunks) == 3
    assert all(c.count("改期") == 1 for c in chunks)


def test_mixed_change_verbs_split():
    body = "新增：7/22(周三) 15:00 评审会\n取消：7/23(周四) 10:00 瑜伽课"
    assert len(split_matters(body)) == 2


def test_overflow_counter_rides_last_chunk():
    body = THREE_RESCHEDULES + "\n…另有 2 项变动（详见日历）"
    chunks = split_matters(body)
    assert len(chunks) == 3
    assert "另有 2 项" in chunks[-1]


def test_bulleted_change_lines_split():
    body = "- 改期：A — 14:00 → 15:00\n- 取消：7/22 10:00 B"
    assert len(split_matters(body)) == 2


def test_multiple_bold_title_sections_split():
    body = ("**知会 · FlashRT 自动多GPU优化**\n号称 B200 上延迟降 70x。\n\n"
            "**知会 · agent 行为指纹**\n靠行为模式认 agent 身份。")
    chunks = split_matters(body)
    assert len(chunks) == 2
    assert chunks[0].startswith("**知会 · FlashRT")
    assert chunks[1].startswith("**知会 · agent")


def test_long_change_list_stays_one_card():
    """2026-08-07: 9 日程变动 cards in 24h drew 0 taps. Past
    MERGE_CHANGE_LIST_ABOVE a change list is calendar churn — one batch, one
    card — while 2–3 changes still split (test above)."""
    body = "\n".join(f"新增：7/2{i % 8}(周一) 1{i}:00 会议{i}" for i in range(10))
    chunks = split_matters(body)
    assert chunks == [body]  # merged, and nothing dropped


def test_bold_section_split_is_capped_against_card_storms():
    body = "\n\n".join(f"**知会 · 第{i}件**\n正文{i}" for i in range(10))
    chunks = split_matters(body)
    assert len(chunks) == MAX_SPLIT_CARDS
    # remainder is kept together, never dropped
    assert sum(c.count("知会") for c in chunks) == 10


# ── split_matters: what must NOT split (over-split protection) ───────────


def test_single_change_line_not_split():
    assert split_matters("改期：A — 14:00 → 15:00") == ["改期：A — 14:00 → 15:00"]


def test_change_lines_inside_prose_not_split():
    body = ("下午的日程有点挤，注意两点：\n"
            "改期：A — 14:00 → 15:00\n"
            "改期：B — 15:00 → 16:00\n"
            "建议提前 10 分钟收状态。")
    assert split_matters(body) == [body]


def test_multi_paragraph_single_matter_not_split():
    body = ("NewsAPI 额度这周会到底。\n\n"
            "按当前 burn 率，7/29 前后撞线；月底熄火结构上不可能。\n\n"
            "要不要现在加钱，还是限流到月底？")
    assert split_matters(body) == [body]


def test_generic_bold_sections_are_one_matter():
    body = ("**背景**\nNewsAPI 额度快到底了。\n\n"
            "**建议**\n限流到月底。")
    assert split_matters(body) == [body]


def test_bold_sections_with_preamble_not_split():
    body = ("一句话总结：两件事其实同根。\n\n"
            "**现象 A**\n细节。\n\n**现象 B**\n细节。")
    assert split_matters(body) == [body]


def test_heading_only_outline_not_split():
    assert split_matters("**第一件**\n**第二件**") == ["**第一件**\n**第二件**"]


def test_inline_bold_line_is_not_a_heading():
    body = "**codex** vs **workbuddy**\n搜索量对比。\n**结论** 略。"
    assert split_matters(body) == [body]


def test_empty_body_passthrough():
    assert split_matters("") == [""]


# ── memorialize_output wiring: adopted legacy cards ──────────────────────


def test_adopted_calendar_card_with_three_reschedules_becomes_three_cards(
        env, capsys):
    """THE 7/21 regression: the exact merged 日程变动 card must split."""
    legacy = build_card("📅 日程变动", THREE_RESCHEDULES, source="calendar-sync")
    rendered = memorial.memorialize_output(legacy, "calendar-sync")
    cards = [json.loads(line) for line in rendered.splitlines()]
    assert len(cards) == 3
    bodies = [c["elements"][0]["text"]["content"] for c in cards]
    assert all(b.count("改期") == 1 for b in bodies)
    assert all(c["header"]["title"]["content"] == "📜 📅 日程变动" for c in cards)
    # three distinct memorials in the ledger, each independently 批-able
    ids = {a["value"]["id"] for c in cards for el in c["elements"]
           if el.get("tag") == "action" for a in el["actions"]}
    assert len(ids) == 3
    # the split is audited
    assert '"msg": "card_split"' in capsys.readouterr().err


def test_adopted_card_with_native_action_button_never_splits(env):
    """A callback button binds to the whole card — replicating it would
    multiply the action, so button-carrying cards are exempt."""
    legacy = build_card("🎯 Intent", "改期：A — 14:00 → 15:00\n改期：B — 15:00 → 16:00",
                        buttons=[{"text": "做了", "value": {
                            "action": "intent_close", "id": "int_1",
                            "outcome": "done"}}])
    rendered = memorial.memorialize_output(legacy, "intention-check")
    assert len(rendered.splitlines()) == 1


def test_adopted_single_matter_card_unchanged(env):
    legacy = build_card("📅 日程变动", "新增：7/22(周三) 15:00 评审会")
    rendered = memorial.memorialize_output(legacy, "calendar-sync")
    assert len(rendered.splitlines()) == 1


# ── memorialize_output wiring: prose path ────────────────────────────────


def test_prose_multi_matter_body_splits_into_cards(env, capsys):
    rendered = memorial.memorialize_output(THREE_RESCHEDULES, "calendar-sync")
    cards = [json.loads(line) for line in rendered.splitlines()]
    assert len(cards) == 3
    assert '"msg": "card_split"' in capsys.readouterr().err
    assert len([s for s in memorial.list_memorials()]) == 3


def test_prose_single_matter_multi_paragraph_stays_one_web_notice(env):
    body = "一件事的第一段。\n\n同一件事的第二段，补充细节。"
    rendered = memorial.memorialize_output(body, "cross-session-sync")
    assert rendered == ""
    states = memorial.list_memorials()
    assert len(states) == 1
    assert "第一段" in states[0]["body"] and "第二段" in states[0]["body"]
    assert states[0]["attention"] == "notice"


def test_prose_with_authored_options_line_never_splits(env):
    """An OPTIONS line means the author designed ONE interactive ask."""
    body = ("改期：A — 14:00 → 15:00\n改期：B — 15:00 → 16:00\n"
            "OPTIONS: 都接受 | 帮我推掉B")
    rendered = memorial.memorialize_output(body, "calendar-sync")
    cards = [json.loads(line) for line in rendered.splitlines()]
    assert len(cards) == 1
    st = memorial.list_memorials()[-1]
    assert [o["label"] for o in st["options"]] == ["都接受", "帮我推掉B"]


def test_explicit_separator_events_still_one_notice_each(env):
    """The existing --- event boundary remains even for web-first sources."""
    rendered = memorial.memorialize_output("第一件进展\n---\n第二件进展",
                                           "cross-session-sync")
    assert rendered == ""
    assert [state["body"] for state in memorial.list_memorials()] == [
        "第一件进展", "第二件进展"]
