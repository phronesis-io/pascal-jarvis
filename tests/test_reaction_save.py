"""Tests for core/reaction_save.py against REAL captured lark-cli shapes.

The first (inline-bash) version of this extractor shipped dead-on-arrival:
its fixtures were imagined OpenAPI shapes, while lark-cli mget returns
content PRE-DECODED at top level. These fixtures mirror live captures from
the 2026-06-12 recheck (lark-cli 1.0.51).
"""

import json

from core.reaction_save import extract_saveable


def _mget(msg):
    return json.dumps({"data": {"messages": [msg]}}, ensure_ascii=False)


# Live shape 1: text message, content pre-decoded at TOP LEVEL (no body key)
def test_real_text_shape_with_url_saves():
    out = extract_saveable(_mget({
        "msg_type": "text",
        "sender": {"sender_type": "app", "id_type": "app_id"},
        "content": "📺 推荐 | 王德峰哲学课 值得一看\nhttps://example.com/video123",
    }))
    assert out is not None
    assert out["items"][0]["url"] == "https://example.com/video123"
    assert "王德峰" in out["title"]


# Live shape 2: interactive message, content is card pseudo-XML
def test_real_interactive_card_shape():
    out = extract_saveable(_mget({
        "msg_type": "interactive",
        "sender": {"sender_type": "app", "id_type": "app_id"},
        "content": '<card title="📚 推荐">\n深度好文，建议精读\nhttps://example.com/article 链接在此</card>',
    }))
    assert out is not None
    assert out["items"][0]["url"] == "https://example.com/article"
    assert out["title"] == "📚 推荐"  # from the card title attribute


# Legacy/raw OpenAPI shape: body.content JSON-wrapped — kept as fallback
def test_legacy_body_content_shape():
    out = extract_saveable(_mget({
        "sender": {"sender_type": "app"},
        "body": {"content": json.dumps({"text": "看这个 https://example.com/x"})},
    }))
    assert out is not None
    assert out["items"][0]["url"] == "https://example.com/x"


def test_user_message_never_saves():
    out = extract_saveable(_mget({
        "sender": {"sender_type": "user"},
        "content": "我的链接 https://evil.example.com",
    }))
    assert out is None


def test_no_url_no_save():
    assert extract_saveable(_mget({
        "sender": {"sender_type": "app"},
        "content": "早上好，今天有三个会",
    })) is None


def test_malformed_payloads_safe():
    assert extract_saveable("not json") is None
    assert extract_saveable(json.dumps({"data": {}})) is None
    assert extract_saveable(json.dumps({"data": {"messages": [{}]}})) is None


def test_multi_url_digest_saves_all_capped():
    text = "📡 今日 digest\n" + "\n".join(
        f"- 第{i}条 https://example.com/item{i}" for i in range(8))
    out = extract_saveable(_mget({
        "sender": {"sender_type": "app"},
        "content": text,
    }))
    assert out is not None
    assert len(out["items"]) == 5  # MAX_URLS cap
    urls = [i["url"] for i in out["items"]]
    assert len(set(urls)) == 5  # distinct


def test_duplicate_urls_deduped():
    out = extract_saveable(_mget({
        "sender": {"sender_type": "app"},
        "content": "同一条 https://example.com/a 再提一次 https://example.com/a",
    }))
    assert len(out["items"]) == 1


def test_title_never_contains_url():
    # First line IS the url-bearing line — title must strip the url so the
    # confirmation reply (which quotes the title) can't itself be saveable.
    out = extract_saveable(_mget({
        "sender": {"sender_type": "app"},
        "content": "推荐看这个 https://example.com/long/path 很不错",
    }))
    assert "http" not in out["title"]
    assert "推荐看这个" in out["title"]


def test_cjk_bracket_terminates_url():
    out = extract_saveable(_mget({
        "sender": {"sender_type": "app"},
        "content": "看「https://example.com/a」这篇",
    }))
    assert out["items"][0]["url"] == "https://example.com/a"
