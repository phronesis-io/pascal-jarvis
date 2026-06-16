"""Tests for core.safety — error pattern detection."""

from core.safety import (
    ERROR_PATTERNS,
    ERROR_SUBSTRINGS,
    extract_json,
    looks_like_error,
    parse_json_response,
    salvage_field,
    salvage_task_ids,
    sanitize_for_user,
    summarize,
)


def test_empty_is_error():
    assert looks_like_error("") is True
    assert looks_like_error("   ") is True


def test_whitespace_only_is_error():
    assert looks_like_error("   ") is True
    assert looks_like_error("\n\t") is True


def test_short_replies_are_safe():
    """Short legitimate replies must NOT be blocked."""
    assert looks_like_error("OK") is False
    assert looks_like_error("好的") is False
    assert looks_like_error("记住了。") is False


def test_normal_text_is_safe():
    assert looks_like_error("Here's what I found in your portfolio today...") is False


def test_traceback_is_error():
    assert looks_like_error("Traceback (most recent call last):\n ...") is True


def test_auth_error_detected():
    assert looks_like_error("Not logged in. Please run /login.") is True


def test_each_pattern_triggers_at_line_start():
    # looks_like_error now requires the pattern at the start of a line
    # (after optional whitespace) to avoid false positives on legitimate
    # content that mentions error terms mid-sentence.
    for p in ERROR_PATTERNS:
        assert looks_like_error(f"{p}\nmore detail follows" + "x" * 20) is True, f"missed line-start: {p}"
        assert looks_like_error(f"  {p} indented") is True, f"missed indented: {p}"


def test_mid_line_mention_is_safe():
    # The user can legitimately discuss errors — these should NOT trigger.
    assert looks_like_error("The API Error was caused by a network blip" + "x" * 50) is False
    assert looks_like_error("I encountered Traceback handling in my code" + "x" * 50) is False


def test_json_auth_error_caught():
    """Regression: API errors embedded in JSON were leaking to users.
    e.g. 'Failed to authenticate. API Error: 401 {"type":"error",...}'
    """
    err = ('Failed to authenticate. API Error: 401 '
           '{"type":"error","error":{"type":"authentication_error",'
           '"message":"Invalid authentication credentials"}}')
    assert looks_like_error(err) is True


def test_json_error_type_caught():
    """JSON responses with "type":"error" should be caught."""
    err = '{"type":"error","error":{"type":"rate_limit","message":"too fast"}}'
    assert looks_like_error(err) is True


def test_claude_spend_limit_caught_even_under_card_header():
    text = "**🏛️ Phronesis**\n\nYou've hit your monthly spend limit · raise it at claude.ai/settings/usage"
    assert looks_like_error(text, proactive=True) is True


def test_substring_patterns_in_json():
    for p in ERROR_SUBSTRINGS:
        text = f'some prefix {p} and more text' + "x" * 50
        assert looks_like_error(text) is True, f"missed substring: {p}"


def test_extract_json_code_fence():
    raw = '```json\n{"key": "value"}\n```'
    assert extract_json(raw) == '{"key": "value"}'


def test_extract_json_trailing_text():
    """Regression: Claude returns JSON in code fence + trailing explanation."""
    raw = '```json\n{"user_message": "test"}\n```\n\nHere is the explanation.'
    result = extract_json(raw)
    import json
    data = json.loads(result)
    assert data["user_message"] == "test"


def test_extract_json_trailing_text_with_braces():
    raw = '{"key": "value"}\n\nNote: use {"example": "not part of payload"}'
    result = extract_json(raw)
    import json
    assert json.loads(result) == {"key": "value"}


def test_extract_json_no_fence():
    raw = '{"plain": "json"}'
    assert extract_json(raw) == '{"plain": "json"}'


def test_extract_json_preamble_text():
    raw = 'Here is the result:\n{"key": "value"}\nDone.'
    result = extract_json(raw)
    import json
    assert json.loads(result)["key"] == "value"


def test_sanitize_returns_fallback_for_errors():
    assert sanitize_for_user("Traceback\n" + "x" * 20, fallback="fallback") == "fallback"


def test_sanitize_returns_original_for_safe():
    safe = "Real reply from Claude."
    assert sanitize_for_user(safe) == safe


# --- salvage from broken JSON (unescaped inner quotes) -----------------------

# The real-world failure: model put bare ASCII quotes inside the value, so
# json.loads raises and the old code dumped the whole object to the user.
BROKEN = (
    '{\n"user_message": "三个 PGC 项可以归档了——不是"放下"，是"已经做完了"。",\n'
    '"auto_decay": [\n'
    '{"task_id": "t_20260531_001", "reason": "上线完成"},\n'
    '{"task_id": "t_20260531_002", "reason": "分支已并 main"}\n]\n}'
)


def test_broken_json_is_unparseable():
    import json
    try:
        json.loads(extract_json(BROKEN))
        assert False, "expected JSONDecodeError"
    except json.JSONDecodeError:
        pass


def test_salvage_field_recovers_message_with_inner_quotes():
    msg = salvage_field(BROKEN, "user_message")
    assert msg.startswith("三个 PGC")
    assert '"放下"' in msg  # inner quotes preserved
    assert "已经做完了" in msg
    assert "auto_decay" not in msg  # stops before the next key


def test_salvage_task_ids_recovers_all_ids():
    assert salvage_task_ids(BROKEN) == ["t_20260531_001", "t_20260531_002"]


def test_salvage_field_missing_returns_none():
    assert salvage_field('{"other": "x"}', "user_message") is None


def test_salvage_field_handles_value_at_end():
    raw = '{"user_message": "结束语带"引号"。"}'
    assert salvage_field(raw, "user_message") == '结束语带"引号"。'


# --- parse_json_response (the shared extract+loads boundary) ------------------


def test_parse_json_response_plain_object():
    assert parse_json_response('{"user_message": "hi"}') == {"user_message": "hi"}


def test_parse_json_response_code_fence_and_trailing():
    raw = '```json\n{"a": 1}\n```\n\nHere is why.'
    assert parse_json_response(raw) == {"a": 1}


def test_parse_json_response_preamble():
    assert parse_json_response('Sure!\n{"a": 1}\nDone.') == {"a": 1}


def test_parse_json_response_ignores_braced_trailer():
    raw = '```json\n{"tasks": {"a": "ok"}, "user_message": ""}\n```\nNote: {"debug": true}'
    assert parse_json_response(raw) == {"tasks": {"a": "ok"}, "user_message": ""}


def test_parse_json_response_empty_is_none():
    assert parse_json_response("") is None
    assert parse_json_response("   \n ") is None


def test_parse_json_response_broken_is_none():
    # Unescaped inner quotes — callers fall back to salvage_field on None.
    assert parse_json_response(BROKEN) is None


def test_parse_json_response_non_object_is_none():
    # A bare list/string/number is valid JSON but not the expected envelope;
    # returning None keeps callers' `.get(...)` from blowing up on a non-dict.
    assert parse_json_response('[1, 2, 3]') is None
    assert parse_json_response('"just a string"') is None
    assert parse_json_response('42') is None


def test_parse_json_response_plain_text_is_none():
    assert parse_json_response("好的，已经记下了。") is None


# --- summarize (the shared card-summary truncation) --------------------------


def test_summarize_short_text_unchanged():
    assert summarize("line1\nline2") == "line1\nline2"


def test_summarize_exactly_max_lines_no_ellipsis():
    text = "\n".join(f"l{i}" for i in range(4))
    assert summarize(text) == text
    assert not summarize(text).endswith("...")


def test_summarize_truncates_with_ellipsis():
    text = "\n".join(f"l{i}" for i in range(10))
    result = summarize(text)
    assert result == "l0\nl1\nl2\nl3\n..."


def test_summarize_respects_max_lines_arg():
    text = "\n".join(f"l{i}" for i in range(5))
    assert summarize(text, max_lines=2) == "l0\nl1\n..."


def test_summarize_strips_surrounding_whitespace():
    assert summarize("\n\n  hello  \n\n") == "hello"
