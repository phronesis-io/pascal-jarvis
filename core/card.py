"""Shared Lark interactive card builder.

All heartbeat task post-scripts use this to produce single-line card JSON
that bot.sh routes via lark_send_card().

Supports RichView integration: any card can include a "查看详情" button
that links to a full-page rendered view via core.richview.publish().
"""

import json
import re
from urllib.parse import urlparse

# Markdown link already in [text](url) form — leave these untouched.
_MD_LINK_RE = re.compile(r"\[[^\]]*\]\(https?://[^\s)]+\)")
# A bare URL not preceded by "(", "]" or "<" (i.e. not already part of a
# markdown link or autolink). Stops at whitespace and bracket chars.
_BARE_URL_RE = re.compile(r"(?<![(\[<])\bhttps?://[^\s<>()\[\]]+")
# Trailing punctuation that shouldn't be swallowed into the link target.
_TRAIL_PUNCT = ".,;:!?，。、）)"


def linkify_bare_urls(text: str) -> str:
    """Convert bare URLs to markdown links so Lark renders them tappable.

    Feishu post/lark_md only makes [text](url) tappable; a bare URL shows as
    plain text the user has to copy-paste. URLs already in markdown-link form
    are left untouched. Label is the URL's host so the tap target reads well.
    """
    if not text or "http" not in text:
        return text

    # Mask existing markdown links so we don't double-wrap them.
    saved: list[str] = []

    def _mask(m: re.Match) -> str:
        saved.append(m.group(0))
        return f"\x00{len(saved) - 1}\x00"

    masked = _MD_LINK_RE.sub(_mask, text)

    def _link(m: re.Match) -> str:
        url = m.group(0)
        trail = ""
        while url and url[-1] in _TRAIL_PUNCT:
            trail = url[-1] + trail
            url = url[:-1]
        try:
            host = urlparse(url).netloc.replace("www.", "") or "链接"
        except ValueError:
            host = "链接"
        return f"[🔗 {host}]({url}){trail}"

    linked = _BARE_URL_RE.sub(_link, masked)
    return re.sub(r"\x00(\d+)\x00", lambda m: saved[int(m.group(1))], linked)


def build_card(header: str, body: str, buttons: list[dict] | None = None,
               source: str = "") -> str:
    """Build a Lark interactive card JSON string (single line).

    Args:
        header: Card header text (e.g. "📺 推荐 | Philosophy")
        body: Markdown body text
        buttons: Optional list of {"text": "label", "url": "https://..."} dicts
        source: Task source name (e.g. "checkin") — if set, adds feedback buttons

    Returns:
        Single-line JSON string starting with {"config":...}
    """
    elements = []
    if body:
        body = linkify_bare_urls(body)
        elements.append({"tag": "div", "text": {"content": body, "tag": "lark_md"}})
    if buttons:
        actions = []
        for i, btn in enumerate(buttons):
            action = {
                "tag": "button",
                "text": {"content": btn["text"], "tag": "plain_text"},
                "type": "primary" if i == 0 else "default",
            }
            if "url" in btn:
                action["url"] = btn["url"]
            if "value" in btn:
                action["value"] = btn["value"]
            actions.append(action)
        elements.append({"tag": "action", "actions": actions})

    # Feedback note — Lark card interactive buttons don't work with WebSocket
    # subscription (no HTTP callback URL). Instead, show a subtle note.
    # User engagement is tracked via reply timing (engagement_log.jsonl).
    # In the future, if an HTTP callback is set up, buttons can be restored.

    card = {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"content": header, "tag": "plain_text"}},
        "elements": elements,
    }
    return json.dumps(card, ensure_ascii=False)


def build_rich_card(
    header: str,
    summary: str,
    sections: list[dict],
    meta: dict | None = None,
    button_text: str = "查看完整内容",
    extra_buttons: list[dict] | None = None,
    source: str = "",
) -> str:
    """Build a Lark card with an auto-generated RichView link.

    This is the primary way to send rich content: the card shows a summary,
    and a button links to the full interactive page.

    Args:
        header: Card header text
        summary: Brief markdown body shown in the card itself
        sections: Full content sections passed to richview.publish()
        meta: Optional metadata for the view
        button_text: Label for the "view details" button
        extra_buttons: Additional buttons to show alongside the view link

    Returns:
        Single-line card JSON string
    """
    from core.richview import publish

    url = publish(title=header, sections=sections, meta=meta)
    buttons = [{"text": button_text, "url": url}]
    if extra_buttons:
        buttons.extend(extra_buttons)
    return build_card(header=header, body=summary, buttons=buttons, source=source)


def extract_card_text(card_json: str) -> str:
    """Extract human-readable text from a Lark card JSON string.

    Used for outbox entries so the main session Claude sees readable content
    instead of raw JSON.
    """
    try:
        card = json.loads(card_json)
        header = card.get("header", {}).get("title", {}).get("content", "")
        parts = [f"**{header}**"] if header else []
        for el in card.get("elements", []):
            text = el.get("text", {}).get("content", "")
            if text:
                parts.append(text)
        return "\n\n".join(parts)
    except (json.JSONDecodeError, TypeError):
        return ""


def extract_readable_from_output(raw_output: str) -> str:
    """Extract readable text from mixed heartbeat output (cards + plain text).

    Handles CARD: prefixed lines, raw card JSON, and plain text.
    Blocks raw JSON from leaking into the outbox.
    """
    parts = []
    for line in raw_output.split("\n"):
        stripped = line.strip()
        card_json = None
        if stripped.startswith("CARD:"):
            card_json = stripped[5:]
        elif stripped.startswith('{"config":'):
            card_json = stripped
        if card_json:
            text = extract_card_text(card_json)
            if text:
                parts.append(text)
        elif stripped:
            # Block raw JSON from entering outbox
            try:
                json.loads(stripped)
                continue  # valid JSON — skip
            except (json.JSONDecodeError, ValueError):
                parts.append(stripped)
    return "\n\n".join(parts)
