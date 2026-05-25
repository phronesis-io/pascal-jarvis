"""Tests for core.safety — error pattern detection."""

from core.safety import ERROR_PATTERNS, ERROR_SUBSTRINGS, looks_like_error, sanitize_for_user


def test_empty_is_error():
    assert looks_like_error("") is True
    assert looks_like_error("   ") is True


def test_too_short_is_error():
    assert looks_like_error("hi") is True  # < MIN_MEANINGFUL_LENGTH


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


def test_substring_patterns_in_json():
    for p in ERROR_SUBSTRINGS:
        text = f'some prefix {p} and more text' + "x" * 50
        assert looks_like_error(text) is True, f"missed substring: {p}"


def test_sanitize_returns_fallback_for_errors():
    assert sanitize_for_user("Traceback\n" + "x" * 20, fallback="fallback") == "fallback"


def test_sanitize_returns_original_for_safe():
    safe = "Real reply from Claude."
    assert sanitize_for_user(safe) == safe
