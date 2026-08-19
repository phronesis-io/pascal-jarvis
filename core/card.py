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
# markdown link or autolink). Allows balanced parentheses inside the URL
# (Wikipedia-style) and matches after CJK characters without a space.
# URL chars: ASCII printable minus whitespace, <>, [], and CJK ranges.
_URL_CHAR = r"[^\s<>\[\]⺀-鿿豈-﫿︰-﹏]"
_BARE_URL_RE = re.compile(
    r"(?<![(\]<])"
    r"(?<!\]\()"
    rf"https?://{_URL_CHAR}*(?:\({_URL_CHAR}*\){_URL_CHAR}*)*"
)
# Trailing punctuation that shouldn't be swallowed into the link target.
_TRAIL_PUNCT = ".,;:!?，。、）)"

# Hosts a Lark client (phone/desktop) cannot open — a richview link on one of
# these is dead, so we render the full content inline instead.
_LOCAL_HOSTS = {"127.0.0.1", "localhost", "0.0.0.0", "::1", ""}
# Lark interactive card content cap is ~8000 chars; stay under it for JSON
# overhead and the header.
_CARD_BODY_LIMIT = 7000
# Lark truncates long button captions on phone; keep them short everywhere.
_MAX_BUTTON_TEXT = 14
# Lark header display truncates at ~40 chars on mobile; the assembled header
# includes emoji prefix (📜 🎯 title), so allow enough for prefix + content.
_MAX_HEADER_CHARS = 60


def _safe_truncate(text: str, limit: int, suffix: str = "\n\n…（已截断）") -> str:
    """Truncate *text* to *limit* chars without breaking markdown links.

    A naive cut can land inside ``[label](url)``, leaving the card with
    visible broken syntax. This finds a safe boundary by backing up past
    any open markdown link at the cut point.
    """
    if len(text) <= limit:
        return text
    budget = limit - len(suffix)
    cut = text[:budget]
    # If we landed inside a markdown link, back up to before its opening [.
    last_open = cut.rfind("[")
    if last_open != -1:
        # Check if this [ is still unclosed (no matching ] after it, or
        # ] exists but the ](url) is incomplete).
        after_open = cut[last_open:]
        close_bracket = after_open.find("]")
        if close_bracket == -1:
            cut = cut[:last_open].rstrip()
        elif after_open.find(")", close_bracket) == -1:
            cut = cut[:last_open].rstrip()
    return cut.rstrip() + suffix


def linkify_bare_urls(text: str) -> str:
    """Convert bare URLs to markdown links so Lark renders them tappable.

    Feishu post/lark_md only makes [text](url) tappable; a bare URL shows as
    plain text the user has to copy-paste. URLs already in markdown-link form
    are left untouched. Label is the URL's host so the tap target reads well.
    """
    if not isinstance(text, str) or not text or "http" not in text:
        return str(text) if text is not None else ""

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
               source: str = "",
               button_groups: list[list[dict]] | None = None,
               context: str = "",
               work_receipt: str = "") -> str:
    """Build a Lark interactive card JSON string (single line).

    Args:
        header: Card header text (e.g. "📺 推荐 | Philosophy")
        body: Markdown body text
        buttons: Optional list of {"text": "label", "url": "https://..."} dicts
        source: Task source name (e.g. "checkin") — if set, adds feedback buttons
        button_groups: Optional rows of buttons. Use this for mobile cards where
            choices, source links, and a conversation affordance should not be
            squeezed into one four-or-five-button row. Mutually exclusive with
            ``buttons``.
        context: Structured context carried through to the memorial this card
            becomes, via the ``__jarvis_context`` marker that
            ``core.memorial.adopt_card`` pops (same convention as
            ``__jarvis_source``). A task post-hook owns only stdout, so this is
            its only channel for attaching machine-readable state — e.g.
            checkin's KIND, which per-kind learning depends on.
        work_receipt: Concrete preparation completed before producing this
            card. The heartbeat adoption gate removes this internal marker,
            persists the receipt, and renders it as user-visible evidence.

    Returns:
        Single-line JSON string starting with {"config":...}, or "" when the
        content carries the heartbeat idle sentinel — HEARTBEAT_OK anywhere
        in a card means the model chose silence and the text around it is
        leaked scratch work (2026-07-15 phronesis leak); the empty string is
        ignored by every routing path.
    """
    from core.safety import sentinel_present
    if sentinel_present(header) or sentinel_present(body):
        return ""
    header = str(header or "").strip()
    body = str(body or "").strip()
    if header and len(header) > _MAX_HEADER_CHARS:
        header = header[:_MAX_HEADER_CHARS]
    elements = []
    if body:
        body = linkify_bare_urls(body)
        if len(body) > _CARD_BODY_LIMIT:
            body = _safe_truncate(body, _CARD_BODY_LIMIT)
        elements.append({"tag": "div", "text": {"content": body, "tag": "lark_md"}})
    if buttons and button_groups:
        raise ValueError("use buttons or button_groups, not both")
    groups = button_groups if button_groups is not None else ([buttons] if buttons else [])
    for group_index, group in enumerate(groups):
        if not group:
            continue
        actions = []
        for i, btn in enumerate(group):
            btn_text = str(btn.get("text") or "").strip()
            if not btn_text:
                continue
            if len(btn_text) > _MAX_BUTTON_TEXT:
                btn_text = btn_text[:_MAX_BUTTON_TEXT]
            action = {
                "tag": "button",
                "text": {"content": btn_text, "tag": "plain_text"},
                "type": btn.get(
                    "type", "primary" if group_index == 0 and i == 0 else "default"),
            }
            if "url" in btn:
                action["url"] = btn["url"]
            if "value" in btn:
                val = btn["value"]
                if not isinstance(val, dict):
                    val = {"v": val}
                action["value"] = val
            actions.append(action)
        if actions:
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
    if context:
        card["__jarvis_context"] = str(context)
    if work_receipt:
        card["__jarvis_work_receipt"] = " ".join(
            str(work_receipt).split()
        )
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
    work_receipt: str = "",
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
        Single-line card JSON string, or "" when any content part carries the
        heartbeat idle sentinel (see build_card) — checked here too so the
        scratch work is never even published to a RichView page.
    """
    from core.safety import sentinel_present
    if (sentinel_present(header) or sentinel_present(summary)
            or any(sentinel_present(s.get("content", "")) for s in sections or []
                   if isinstance(s, dict))):
        return ""
    from core.richview import publish

    url = publish(title=header, sections=sections, meta=meta)

    if _url_is_reachable(url):
        buttons = [{"text": button_text, "url": url}]
        if extra_buttons:
            buttons.extend(extra_buttons)
        return build_card(
            header=header, body=summary, buttons=buttons, source=source,
            work_receipt=work_receipt,
        )

    # Localhost / unreachable view: render the full content inline so the user
    # can read everything in the card, and drop the dead "查看完整内容" link.
    body = _sections_to_markdown(sections) or summary
    if len(body) > _CARD_BODY_LIMIT:
        body = _safe_truncate(body, _CARD_BODY_LIMIT, "\n\n…（内容较长，已截断）")
    return build_card(
        header=header,
        body=body,
        buttons=list(extra_buttons) if extra_buttons else None,
        source=source,
        work_receipt=work_receipt,
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
