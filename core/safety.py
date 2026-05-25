"""Shared safety helpers — detect error-looking Claude output before it reaches users."""

from __future__ import annotations

# Patterns that indicate Claude (or the underlying tooling) returned an error
# instead of a real answer. Post-scripts and the main bot reply path both
# check against these so we never send a traceback/auth error to the user.
#
# Matched at LINE START within the first 300 chars to avoid false positives
# when Claude legitimately discusses technical errors in running text
# (e.g. "the API Error was caused by...").
# Patterns checked at line start (startswith)
ERROR_PATTERNS: tuple[str, ...] = (
    "Not logged in",
    "Please run /login",
    "Invalid authentication",
    "API Error",
    "Traceback",
    "usage limit",
    "credit balance",
    "Connection error",
    "Failed to authenticate",
)

# Patterns checked anywhere in first 300 chars (substring match)
# These appear mid-line in JSON error responses
ERROR_SUBSTRINGS: tuple[str, ...] = (
    "authentication_error",
    "rate_limit",
    '"type":"error"',
)

# If the entire answer is shorter than this, treat it as non-substantive noise.
MIN_MEANINGFUL_LENGTH = 5


def looks_like_error(text: str) -> bool:
    """Return True if `text` looks like an error surface rather than real content.

    Only checks the first 300 chars. Patterns must appear at the start of
    a line (after optional whitespace) to avoid false positives on legitimate
    content that mentions error-related terms mid-sentence.
    """
    if not text or len(text.strip()) < MIN_MEANINGFUL_LENGTH:
        return True
    head = text[:300]
    # Check line-start patterns
    for line in head.splitlines():
        stripped = line.lstrip()
        if any(stripped.startswith(p) for p in ERROR_PATTERNS):
            return True
    # Check substring patterns (for errors embedded in JSON)
    if any(p in head for p in ERROR_SUBSTRINGS):
        return True
    return False


def sanitize_for_user(text: str, fallback: str = "") -> str:
    """Return `text` unchanged if safe to send, otherwise return `fallback`."""
    if looks_like_error(text):
        return fallback
    return text


def extract_json(raw: str) -> str:
    """Extract JSON object from Claude's response, handling code fences and trailing text.

    Claude often returns JSON wrapped in ```json...``` with optional trailing
    text after the closing ```. This function robustly extracts the JSON portion.

    Returns the cleaned JSON string, or the original if no JSON found.
    """
    import re
    # Strip code fence markers
    cleaned = re.sub(r'^```json?\s*', '', raw.strip())
    cleaned = re.sub(r'```\s*$', '', cleaned.strip())
    # If there's still a ``` in the middle (trailing text after code fence),
    # take only the part before it
    if '```' in cleaned:
        cleaned = cleaned[:cleaned.index('```')].strip()
    # Try to find a JSON object
    json_start = cleaned.find('{')
    json_end = cleaned.rfind('}')
    if json_start >= 0 and json_end > json_start:
        return cleaned[json_start:json_end + 1]
    return cleaned


def atomic_write(path, content: str, encoding: str = "utf-8"):
    """Write content to path atomically via tmp + rename.

    Prevents data corruption if the process is killed mid-write.
    Use for any file that is read by another process (heartbeat state,
    memory files, engagement logs, etc.).
    """
    import os
    from pathlib import Path
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(content, encoding=encoding)
    os.replace(tmp, p)
