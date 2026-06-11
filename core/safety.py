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
    # 2026-06-10: a 403 auth failure rode out to the user 7 times in 12 hours
    # as "**Intent** | Failed to authenticate. API Error: 403 Request not
    # allowed" — the markdown header defeated the line-start check. These
    # exact API-failure phrases are specific enough that a false positive in
    # the first 300 chars of real content is effectively impossible.
    "Failed to authenticate. API Error",
    "API Error: 401",
    "API Error: 403",
    "API Error: 429",
    "API Error: 500",
    "API Error: 529",
    "Request not allowed",
)



def looks_like_error(text: str) -> bool:
    """Return True if `text` looks like an error surface rather than real content.

    Only checks the first 300 chars. Patterns must appear at the start of
    a line (after optional whitespace) to avoid false positives on legitimate
    content that mentions error-related terms mid-sentence.
    """
    if not text or not text.strip():
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


def parse_json_response(raw: str):
    """Extract and parse a JSON *object* from a Claude response.

    This folds together the boilerplate every heartbeat post-hook was
    duplicating: extract_json() to peel off code fences / preamble, then
    json.loads(). Returns the parsed dict, or None if the payload is empty,
    isn't valid JSON, or doesn't parse to an object.

    Never raises. On a None return, callers decide what to do with the raw
    string — suppress it, forward it as plain text, or recover a human message
    from *broken* JSON via salvage_field()/salvage_task_ids() (see
    task_triage_post / weekly_review_post). Centralizing the happy path here is
    what keeps "never dump raw JSON to the user" a single decision instead of
    one reimplemented per file.
    """
    import json
    if not raw or not raw.strip():
        return None
    try:
        result = json.loads(extract_json(raw))
    except (json.JSONDecodeError, ValueError):
        return None
    return result if isinstance(result, dict) else None


def summarize(text: str, max_lines: int = 4) -> str:
    """First `max_lines` lines of `text`, with a trailing "..." if truncated.

    The canonical card-summary truncation shared by every rich-card post-hook
    (phronesis, eigenflux, weekly_review, daily_plan, daily_reflect, ...).
    """
    lines = text.strip().splitlines()
    summary = "\n".join(lines[:max_lines])
    if len(lines) > max_lines:
        summary += "\n..."
    return summary


def salvage_field(raw: str, field: str = "user_message"):
    """Best-effort extract a string field's value from broken JSON.

    Models sometimes emit invalid JSON by using unescaped ASCII double-quotes
    inside a string value (common in Chinese, e.g. `不是"放下"`), which makes
    `json.loads` fail. Rather than dumping the raw JSON object to the user
    (leaking system fields like auto_decay), recover just the human-facing
    value by anchoring on the next JSON key or the closing brace.

    Returns the value string, or None if the field isn't present.
    """
    import re
    m = re.search(
        r'"' + re.escape(field) + r'"\s*:\s*"(.*?)"\s*(?:,\s*"\w+"\s*:|\}\s*$)',
        raw,
        re.DOTALL,
    )
    if m:
        return m.group(1).strip()
    return None


def salvage_task_ids(raw: str):
    """Extract all task_id values from broken JSON so decay still applies."""
    import re
    return re.findall(r'"task_id"\s*:\s*"([^"]+)"', raw)


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
