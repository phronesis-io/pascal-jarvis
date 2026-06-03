"""Tests for core.card — Lark card building and text extraction."""

import json

from core.card import build_card, extract_card_text, extract_readable_from_output, linkify_bare_urls


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
