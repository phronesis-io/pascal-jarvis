"""Tests for core.card — Lark card building and text extraction."""

import json

import pytest

import core.richview
from core.card import (
    _sections_to_markdown,
    _url_is_reachable,
    build_card,
    build_rich_card,
    extract_card_text,
    extract_readable_from_output,
    linkify_bare_urls,
    strip_internal_fields,
)


def _card_body(card_json: str) -> str:
    card = json.loads(card_json)
    for el in card["elements"]:
        if el.get("tag") == "div":
            return el["text"]["content"]
    return ""


def _card_buttons(card_json: str) -> list:
    card = json.loads(card_json)
    for el in card["elements"]:
        if el.get("tag") == "action":
            return el["actions"]
    return []


def test_linkify_bare_url():
    out = linkify_bare_urls("看这个 https://www.youtube.com/watch?v=SJYj57TUKjc 不错")
    assert "[🔗 youtube.com](https://www.youtube.com/watch?v=SJYj57TUKjc)" in out


def test_linkify_leaves_existing_markdown_links():
    src = "见 [原文](https://dev.to/x) 和 https://arxiv.org/abs/1"
    out = linkify_bare_urls(src)
    assert out.count("[原文](https://dev.to/x)") == 1  # untouched, not double-wrapped
    assert "[🔗 arxiv.org](https://arxiv.org/abs/1)" in out


def test_linkify_preserves_trailing_punctuation():
    out = linkify_bare_urls("链接：https://example.com。")
    assert "(https://example.com)" in out  # period not swallowed into URL
    assert out.rstrip().endswith("。")


def test_linkify_noop_without_url():
    assert linkify_bare_urls("没有链接的纯文本") == "没有链接的纯文本"


def test_build_card_linkifies_body():
    result = build_card("H", "watch https://youtu.be/abc now")
    card = json.loads(result)
    assert "[🔗 youtu.be](https://youtu.be/abc)" in card["elements"][0]["text"]["content"]


def test_build_card_basic():
    result = build_card("Test Header", "Hello world")
    card = json.loads(result)
    assert card["header"]["title"]["content"] == "Test Header"
    assert card["elements"][0]["text"]["content"] == "Hello world"


def test_build_card_preserves_overlong_body_for_memorial_adoption():
    body = "完整正文" * 2200
    card = json.loads(build_card("长文", body))

    assert "已截断" in card["elements"][0]["text"]["content"]
    assert card["__jarvis_full_body"] == body


def test_transport_sanitizer_removes_all_internal_card_fields():
    raw = json.dumps({
        "config": {"wide_screen_mode": True},
        "elements": [],
        "__jarvis_full_body": "private full body",
        "__jarvis_context": "private context",
        "__jarvis_work_receipt": "internal receipt",
    })

    card = json.loads(strip_internal_fields(raw))

    assert not any(key.startswith("__jarvis_") for key in card)
    assert card["config"]["wide_screen_mode"] is True


def test_build_card_with_buttons():
    buttons = [
        {"text": "Open", "url": "https://example.com"},
        {"text": "Save", "value": {"action": "save"}},
    ]
    result = build_card("Title", "Body", buttons)
    card = json.loads(result)
    actions = card["elements"][1]["actions"]
    assert len(actions) == 2
    assert actions[0]["url"] == "https://example.com"
    assert actions[1]["value"] == {"action": "save"}
    assert actions[0]["type"] == "primary"
    assert actions[1]["type"] == "default"


def test_build_card_with_phone_first_button_groups():
    result = build_card("Title", "Body", button_groups=[
        [{"text": "同意", "value": {"k": "yes"}},
         {"text": "不采纳", "value": {"k": "no"}}],
        [{"text": "聊聊这个", "type": "default", "value": {"k": "chat"}}],
    ])
    card = json.loads(result)
    rows = [e["actions"] for e in card["elements"] if e.get("tag") == "action"]
    assert [[a["text"]["content"] for a in row] for row in rows] == [
        ["同意", "不采纳"], ["聊聊这个"]]
    assert rows[0][0]["type"] == "primary"
    assert rows[1][0]["type"] == "default"


def test_build_card_rejects_buttons_and_groups_together():
    with pytest.raises(ValueError):
        build_card("T", "B", buttons=[{"text": "A"}],
                   button_groups=[[{"text": "B"}]])


def test_build_card_empty_body():
    result = build_card("Header Only", "")
    card = json.loads(result)
    # No div element for empty body
    assert not any(e.get("tag") == "div" for e in card["elements"])


def test_extract_card_text():
    card_json = json.dumps({
        "header": {"title": {"content": "Alert"}},
        "elements": [
            {"tag": "div", "text": {"content": "Something happened"}},
        ],
    })
    text = extract_card_text(card_json)
    assert "**Alert**" in text
    assert "Something happened" in text


def test_extract_card_text_invalid_json():
    assert extract_card_text("not json") == ""


def test_extract_readable_from_output():
    card = json.dumps({
        "config": {"wide_screen_mode": True},
        "header": {"title": {"content": "Check-in"}},
        "elements": [{"tag": "div", "text": {"content": "How are you?"}}],
    })
    output = f"CARD:{card}\nsome plain text"
    result = extract_readable_from_output(output)
    assert "Check-in" in result
    assert "How are you?" in result
    assert "some plain text" in result
    assert "config" not in result  # raw JSON should not leak


def test_extract_readable_blocks_raw_json():
    output = '{"internal": "data"}\nvisible text'
    result = extract_readable_from_output(output)
    assert "internal" not in result
    assert "visible text" in result


def test_url_is_reachable():
    assert _url_is_reachable("https://views.example.com/view/abc")
    assert not _url_is_reachable("http://127.0.0.1:3456/view/abc")
    assert not _url_is_reachable("http://localhost:3456/view/abc")
    assert not _url_is_reachable("")


def test_sections_to_markdown_flattens_kv_and_markdown():
    md = _sections_to_markdown([
        {"type": "markdown", "content": "今天的计划"},
        {"type": "kv", "items": {"模式 1": "回避", "模式 2": "紧绷"}},
    ])
    assert "今天的计划" in md
    assert "**模式 1**：回避" in md
    assert "**模式 2**：紧绷" in md


def test_rich_card_localhost_renders_full_content_inline(monkeypatch):
    # Localhost view is unreachable from Lark → full content must be in the card,
    # not hidden behind a dead "查看完整内容" link.
    monkeypatch.setattr(core.richview, "publish",
                        lambda **kw: "http://127.0.0.1:3456/view/deadbeef")
    full = "完整的今日计划：康复处方、臀肌激活、周会准备，全都在这里。"
    result = build_rich_card(
        header="🌅 今日",
        summary="完整的今日…",  # truncated summary that used to be all he saw
        sections=[{"type": "markdown", "content": full}],
        source="daily-plan",
    )
    assert full in _card_body(result)  # full content inline
    # No dead richview link button
    assert not any("查看完整内容" in b["text"]["content"] for b in _card_buttons(result))


def test_rich_card_public_url_keeps_summary_and_link(monkeypatch):
    monkeypatch.setattr(core.richview, "publish",
                        lambda **kw: "https://views.example.com/view/abc")
    result = build_rich_card(
        header="🌅 今日",
        summary="简短摘要",
        sections=[{"type": "markdown", "content": "完整内容"}],
    )
    assert _card_body(result) == "简短摘要"
    btns = _card_buttons(result)
    assert btns[0]["text"]["content"] == "查看完整内容"
    assert btns[0]["url"] == "https://views.example.com/view/abc"


def test_rich_card_preserves_extra_buttons_inline(monkeypatch):
    monkeypatch.setattr(core.richview, "publish",
                        lambda **kw: "http://127.0.0.1:3456/view/x")
    result = build_rich_card(
        header="📺 推荐",
        summary="s",
        sections=[{"type": "markdown", "content": "body"}],
        extra_buttons=[{"text": "收藏", "value": {"action": "save"}}],
    )
    btns = _card_buttons(result)
    assert any(b["text"]["content"] == "收藏" for b in btns)


def test_rich_card_truncates_overlong_content(monkeypatch):
    monkeypatch.setattr(core.richview, "publish",
                        lambda **kw: "http://127.0.0.1:3456/view/x")
    long_body = "字" * 9000
    result = build_rich_card(
        header="周报", summary="s",
        sections=[{"type": "markdown", "content": long_body}],
    )
    body = _card_body(result)
    assert len(body) < 9000
    assert "已截断" in body


# ── idle-sentinel gate (2026-07-15 phronesis leak) ───────────────────


def test_build_card_suppresses_trailing_sentinel():
    # The exact leak shape: prose + blank line + trailing HEARTBEAT_OK.
    leaked = ("Just team members chatting about seating and air "
              "conditioning—nothing noteworthy.\n\nHEARTBEAT_OK")
    assert build_card("🏛️ Phronesis", leaked) == ""


def test_build_card_suppresses_sentinel_anywhere():
    assert build_card("h", "HEARTBEAT_OK") == ""
    assert build_card("h", "line one\nmid HEARTBEAT_OK text\nline three") == ""
    assert build_card("HEARTBEAT_OK", "body") == ""
    # clean content still builds
    assert build_card("h", "正常内容").startswith('{"config":')


def test_build_rich_card_suppresses_sentinel(monkeypatch):
    monkeypatch.setattr(core.richview, "publish",
                        lambda **kw: "http://127.0.0.1:3456/view/x")
    leaked = "analysis text\n\nHEARTBEAT_OK"
    assert build_rich_card(
        header="🏛️ Phronesis", summary="ok summary",
        sections=[{"type": "markdown", "content": leaked}]) == ""
    assert build_rich_card(
        header="🏛️ Phronesis", summary=leaked,
        sections=[{"type": "markdown", "content": "fine"}]) == ""
