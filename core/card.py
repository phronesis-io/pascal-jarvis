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

# Hosts a Lark client (phone/desktop) cannot open — a richview link on one of
# these is dead, so we render the full content inline instead.
_LOCAL_HOSTS = {"127.0.0.1", "localhost", "0.0.0.0", "::1", ""}
# Lark interactive card content cap is ~8000 chars; stay under it for JSON
# overhead and the header.
_CARD_BODY_LIMIT = 7000


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


def _url_is_reachable(url: str) -> bool:
    """True if a Lark client could open this URL (i.e. not a localhost view)."""
    if not url:
        return False
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    return host not in _LOCAL_HOSTS


def _sections_to_markdown(sections: list[dict] | None) -> str:
    """Flatten richview sections into one markdown string for inline display.

    Used when the richview link is unreachable from the user's Lark client, so
    the full content is shown in the card itself instead of behind a dead link.
    """
    parts: list[str] = []
    for sec in sections or []:
        stype = sec.get("type", "markdown")
        if stype == "kv":
            items = sec.get("items", {}) or {}
            parts.append("\n".join(f"**{k}**：{v}" for k, v in items.items()))
        elif stype == "timeline":
            events = sec.get("events", []) or []
            parts.append("\n".join(
                f"`{e.get('time', '')}` {e.get('text', '')}" for e in events))
        elif stype == "table":
            lines = []
            headers = sec.get("headers", []) or []
            if headers:
                lines.append(" | ".join(str(h) for h in headers))
            lines.extend(" | ".join(str(c) for c in row)
                         for row in sec.get("rows", []) or [])
            parts.append("\n".join(lines))
        elif stype == "code":
            parts.append(f"```{sec.get('language', '')}\n{sec.get('content', '')}\n```")
        else:  # markdown / heading / unknown
            content = sec.get("content", "")
            if content:
                parts.append(content)
    return "\n\n".join(p for p in parts if p)


def build_rich_card(
    header: str,
    summary: str,
    sections: list[dict],
    meta: dict | None = None,
    button_text: str = "查看完整内容",
    extra_buttons: list[dict] | None = None,
    source: str = "",
) -> str:
    """Build a Lark card carrying full rich content.

    When the RichView page is publicly reachable, the card shows a short
    summary and links to the full interactive page. When it is served from
    localhost (the link a Lark client can't open), the full content is rendered
    inline instead — so nothing is hidden behind a dead link.

    Args:
        header: Card header text
        summary: Brief markdown body shown when the full view is reachable
        sections: Full content sections passed to richview.publish()
        meta: Optional metadata for the view
        button_text: Label for the "view details" button
        extra_buttons: Additional buttons to show alongside the view link

    Returns:
        Single-line card JSON string
    """
    from core.richview import publish

    url = publish(title=header, sections=sections, meta=meta)

    if _url_is_reachable(url):
        buttons = [{"text": button_text, "url": url}]
        if extra_buttons:
            buttons.extend(extra_buttons)
        return build_card(header=header, body=summary, buttons=buttons, source=source)

    # Localhost / unreachable view: render the full content inline so the user
    # can read everything in the card, and drop the dead "查看完整内容" link.
    body = _sections_to_markdown(sections) or summary
    if len(body) > _CARD_BODY_LIMIT:
        body = body[:_CARD_BODY_LIMIT].rstrip() + "\n\n…（内容较长，已截断）"
    return build_card(
        header=header,
        body=body,
        buttons=list(extra_buttons) if extra_buttons else None,
        source=source,
    )


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
