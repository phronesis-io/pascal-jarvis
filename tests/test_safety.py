"""Tests for core.safety — error pattern detection."""

from core.safety import ERROR_PATTERNS, looks_like_error, sanitize_for_user


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


def test_each_pattern_triggers():
    for p in ERROR_PATTERNS:
        assert looks_like_error(f"prefix {p} suffix" * 2) is True, f"missed: {p}"


def test_sanitize_returns_fallback_for_errors():
    assert sanitize_for_user("Traceback\n" + "x" * 20, fallback="fallback") == "fallback"


def test_sanitize_returns_original_for_safe():
    safe = "Real reply from Claude."
    assert sanitize_for_user(safe) == safe
