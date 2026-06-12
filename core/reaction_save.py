"""Extract saveable links from a reacted-on message (one-tap watch-later).

bot.sh pipes `lark-cli im +messages-mget` stdout here; we print a JSON
result or nothing. Lives in core/ (not inline bash-python) so the logic is
covered by tests with REAL captured fixtures — the first inline version
shipped dead-on-arrival because its imagined fixtures didn't match what
lark-cli actually emits (2026-06-12 recheck finding).

Real lark-cli (1.0.51) mget shapes, captured live:
- text message:        top-level "content": "大休特休"        (pre-decoded, no body key)
- interactive message: top-level "content": '<card title="⏰ 空档">\\n...'
- legacy/raw OpenAPI:  "body": {"content": "{\\"text\\": ...}"} (kept as fallback)
"""

from __future__ import annotations

import json
import re
import sys

MAX_URLS = 5
# Exclusions: whitespace, closing brackets/quotes (ASCII + CJK), CJK
# punctuation, markdown emphasis tails.
_URL_RE = re.compile(r"https?://[^\s\)\]\}>\"'，。；、」』＞＊*]+")
_CARD_TITLE_RE = re.compile(r'<card title="([^"]*)"')
_TITLE_STRIP_RE = re.compile(r"[#*\[\]`>|<{}\"']")


def _message_text(msg: dict) -> str:
    """Normalize the message content to plain text across known shapes."""
    body = msg.get("body", {}).get("content", msg.get("content", "")) or ""
    if not isinstance(body, str):
        return json.dumps(body, ensure_ascii=False)
    try:
        inner = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        inner = None
    if isinstance(inner, dict):
        if isinstance(inner.get("text"), str):
            return inner["text"]
        # rich text (post): concatenate text runs
        runs = []
        for block in (inner.get("content") or []):
            for piece in (block or []):
                if isinstance(piece, dict):
                    runs.append(piece.get("text", "") or piece.get("href", ""))
        if runs:
            return " ".join(r for r in runs if r)
        return json.dumps(inner, ensure_ascii=False)
    # Pre-decoded plain text / card pseudo-XML — the NORMAL lark-cli case
    return body


def _pick_title(text: str, fallback: str) -> str:
    m = _CARD_TITLE_RE.search(text)
    if m and m.group(1).strip():
        return m.group(1).strip()[:80]
    for line in text.splitlines():
        # titles must never contain a URL: the confirmation reply quotes the
        # title, and a URL in it would make the confirmation itself saveable
        # (reaction loop)
        line = _URL_RE.sub("", line)
        line = _TITLE_STRIP_RE.sub("", line).strip()
        if line:
            return line[:80]
    return fallback[:60]


def extract_saveable(mget_stdout: str) -> dict | None:
    """Return {"title": str, "items": [{"title","url"}...]} or None.

    Only fires for OUR OWN (bot-sent) messages that contain at least one URL.
    """
    try:
        d = json.loads(mget_stdout)
    except (json.JSONDecodeError, ValueError):
        return None
    msgs = (d.get("data") or {}).get("messages") or d.get("messages") or []
    if not msgs:
        return None
    m = msgs[0] or {}
    sender = m.get("sender") or {}
    stype = sender.get("sender_type") or sender.get("type") or ""
    if stype not in ("app", "bot"):
        return None  # never save from someone else's message

    text = _message_text(m)
    urls = []
    for u in _URL_RE.findall(text):
        u = u.rstrip(".,;:!?")
        if u not in urls:
            urls.append(u)
    if not urls:
        return None

    title = _pick_title(text, urls[0])
    items = [{"title": title if i == 0 else f"{title} ({i + 1})", "url": u}
             for i, u in enumerate(urls[:MAX_URLS])]
    return {"title": title, "items": items}


if __name__ == "__main__":
    result = extract_saveable(sys.stdin.read())
    if result:
        print(json.dumps(result, ensure_ascii=False))
