"""Shared Lark interactive card builder.

All heartbeat task post-scripts use this to produce single-line card JSON
that bot.sh routes via lark_send_card().
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
