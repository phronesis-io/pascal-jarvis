"""Shared safety helpers — detect error-looking Claude output before it reaches users."""

from __future__ import annotations

# Patterns that indicate Claude (or the underlying tooling) returned an error
# instead of a real answer. Post-scripts and the main bot reply path both
# check against these so we never send a traceback/auth error to the user.
ERROR_PATTERNS: tuple[str, ...] = (
    "Not logged in",
    "Please run /login",
    "Invalid authentication",
    "API Error",
    "authentication_error",
    "rate_limit",
    "Traceback",
    "usage limit",
    "credit balance",
    "Connection error",
)

# If the entire answer is shorter than this, treat it as non-substantive noise.
MIN_MEANINGFUL_LENGTH = 5


def looks_like_error(text: str) -> bool:
    """Return True if `text` looks like an error surface rather than real content."""
    if not text or len(text.strip()) < MIN_MEANINGFUL_LENGTH:
        return True
    return any(p in text for p in ERROR_PATTERNS)


def sanitize_for_user(text: str, fallback: str = "") -> str:
    """Return `text` unchanged if safe to send, otherwise return `fallback`."""
    if looks_like_error(text):
        return fallback
    return text
