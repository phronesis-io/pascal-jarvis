"""Shared safety helpers — detect error-looking Claude output before it reaches users."""

from __future__ import annotations

# The heartbeat idle sentinel. When it appears ANYWHERE in a task's output,
# the model decided this cycle should stay silent — any surrounding text is
# leaked scratch work ("...nothing noteworthy.\n\nHEARTBEAT_OK", 2026-07-15
# phronesis card leak), never a message for the user.
IDLE_SENTINEL = "HEARTBEAT_OK"


def sentinel_present(text) -> bool:
    """True if the idle sentinel appears on any line of `text`.

    Deliberately a per-line substring match, NOT `strip() == sentinel`:
    exact-match checks in post-scripts are exactly how "prose + trailing
    HEARTBEAT_OK" leaked to the user as a card (2026-06-08 intent card,
    2026-07-15 phronesis card). The trade-off — a legitimate message that
    merely MENTIONS the token gets suppressed — fails silent, which is the
    safe direction; the sentinel is an internal protocol word that has no
    business in user-facing prose. Card builders (core/card.py) and the
    delivery router (core/heartbeat_loop._route_output) both enforce this,
    so no individual post-script can leak it again.
    """
    if not text:
        return False
    return any(IDLE_SENTINEL in line for line in str(text).splitlines())

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
    "You've hit your monthly spend limit",
    "You have hit your monthly spend limit",
    "monthly spend limit",
    "spend limit",
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

# Short no-op outputs that Claude/Claude Code can emit after a failed resume.
# These are not useful answers and previously showed up in conversation audit
# as user-visible empty turns. Keep this separate from ERROR_PATTERNS so normal
# longer debugging prose can still discuss the phrase.
NOOP_REPLY_NEEDLES: tuple[str, ...] = (
    "no response requested",
)

# Checked ONLY for proactive (heartbeat) messages, where these phrases in the
# first 300 chars are effectively always an error surface.
# 2026-06-10: a 403 auth failure rode out to the user 7 times in 12 hours as
# "**Intent** | Failed to authenticate. API Error: 403 Request not allowed" —
# the markdown header defeated the line-start check. They are NOT applied to
# interactive replies: when Pascal debugs this very bot, a legitimate answer
# may quote "API Error: 403" verbatim in its opening lines.
PROACTIVE_ERROR_SUBSTRINGS: tuple[str, ...] = (
    "Failed to authenticate. API Error",
    "API Error: 401",
    "API Error: 403",
    "API Error: 429",
    "API Error: 500",
    "API Error: 529",
    "Request not allowed",
    "You've hit your monthly spend limit",
    "You have hit your monthly spend limit",
    "monthly spend limit",
    "raise it at claude.ai/settings/usage",
)



def looks_like_error(text: str, proactive: bool = False) -> bool:
    """Return True if `text` looks like an error surface rather than real content.

    Only checks the first 300 chars. Patterns must appear at the start of
    a line (after optional whitespace) to avoid false positives on legitimate
    content that mentions error-related terms mid-sentence.

    proactive=True (heartbeat user messages) additionally applies the
    anywhere-in-head API-failure substrings — proactive notes never
    legitimately open with those, while interactive replies might quote them.
    """
    if not text or not text.strip():
        return True
    if _looks_like_noop_reply(text):
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
    if proactive and any(p in head for p in PROACTIVE_ERROR_SUBSTRINGS):
        return True
    return False


def _looks_like_noop_reply(text: str) -> bool:
    compact = " ".join(text.strip().split())
    if not compact or len(compact) > 200:
        return False
    lowered = compact.lower()
    stripped = lowered.strip(" .。!！?？")
    if stripped in NOOP_REPLY_NEEDLES:
        return True
    return (
        stripped.startswith("continue from where you left off")
        and any(needle in stripped for needle in NOOP_REPLY_NEEDLES)
    )


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
    import json
    import re
    cleaned = raw.strip()
    # A fenced block can appear ANYWHERE in the reply: models emit both
    # "```json ... ``` trailing prose" and "leading prose ... ```json ..."
    # (the 7/3 heartbeat outage: every envelope was preceded by analysis
    # prose, so cutting at the first ``` threw the envelope away and kept
    # the prose). Prefer the first ```json fence; fall back to any fence
    # that opens a JSON object.
    fenced = (re.search(r'```json\s*(.*?)(?:```|\Z)', cleaned, re.DOTALL)
              or re.search(r'```\s*(\{.*?)(?:```|\Z)', cleaned, re.DOTALL))
    if fenced:
        cleaned = fenced.group(1).strip()
    # Try to find a JSON object
    json_start = cleaned.find('{')
    json_end = cleaned.rfind('}')
    if json_start >= 0 and json_end > json_start:
        # Prefer the first complete JSON object. A model may append commentary
        # containing braces after the envelope; first-to-last slicing makes that
        # valid envelope unparseable. Do not scan forward to nested braces on a
        # failed decode: callers rely on malformed top-level JSON staying
        # malformed so system fields do not leak via partial objects.
        decoder = json.JSONDecoder()
        try:
            _, end = decoder.raw_decode(cleaned[json_start:])
            return cleaned[json_start:json_start + end]
        except json.JSONDecodeError:
            pass
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
