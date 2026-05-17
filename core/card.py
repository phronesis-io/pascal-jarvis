"""Shared Lark interactive card builder.

All heartbeat task post-scripts use this to produce single-line card JSON
that bot.sh routes via lark_send_card().

Supports RichView integration: any card can include a "查看详情" button
that links to a full-page rendered view via core.richview.publish().
"""

import json


def build_card(header: str, body: str, buttons: list[dict] | None = None) -> str:
    """Build a Lark interactive card JSON string (single line).

    Args:
        header: Card header text (e.g. "📺 推荐 | Philosophy")
        body: Markdown body text
        buttons: Optional list of {"text": "label", "url": "https://..."} dicts

    Returns:
        Single-line JSON string starting with {"card":...}
    """
    elements = []
    if body:
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

    # Lark interactive message content is the card object directly (no {"card":} wrapper).
    # We prefix with {"card": for bot.sh detection, then lark_send_card strips it.
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
    return build_card(header=header, body=summary, buttons=buttons)
