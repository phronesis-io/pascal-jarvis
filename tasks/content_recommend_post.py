#!/usr/bin/env python3
"""Post-hook: log content recommendations and prevent repeats.

Stdin: Claude's JSON response with recommendation.
Stdout: user-facing message with title + link.
"""

import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.card import build_card
from core.safety import looks_like_error, parse_json_response
from core.jsonl import read_jsonl, write_jsonl
from core.timeutil import now_local_str

MEMORY_DIR = Path(os.environ.get("MEMORY_DIR", Path.home() / ".jarvis" / "memory"))
LOG_FILE = MEMORY_DIR / "system" / "content_recommend_log.jsonl"
MAX_ENTRIES = 60  # ~1 month at 2/day


def main() -> int:
    raw = sys.stdin.read().strip()
    if not raw or raw == "HEARTBEAT_OK":
        return 0
    if looks_like_error(raw):
        print("[content-recommend] skipping — looks like error", file=sys.stderr)
        return 0

    # Parse Claude's response — expect JSON with url, title, user_message
    data = parse_json_response(raw)
    if data is None:
        # Claude returned plain text instead of JSON. Extract URL if present
        # and build a card with a clickable button.
        text = re.sub(r'\{[^{}]*\}', '', raw).strip()
        url_match = re.search(r'https?://\S+', text)
        if text and url_match:
            url = url_match.group(0)
            body = text.replace(url, "").strip()
            buttons = [{"text": "去看看", "url": url}]
            print(build_card(
                "📺 推荐", body, buttons, source="content-recommend",
                work_receipt="完成内容筛选、链接提取和历史推荐去重",
            ))
        elif text:
            print(build_card(
                "📺 推荐", text, source="content-recommend",
                work_receipt="完成内容筛选和历史推荐去重",
            ))
        return 0

    url = data.get("url", "")
    title = data.get("title", "")
    user_message = data.get("user_message", "")
    category = data.get("category", "")

    if not user_message and not url:
        return 0

    # Check for duplicate URL
    entries = read_jsonl(LOG_FILE)

    # Block if same URL was already recommended
    if url:
        existing_urls = {e.get("url", "") for e in entries}
        if url in existing_urls:
            print(f"[content-recommend] BLOCKED — already recommended: {url}", file=sys.stderr)
            return 0

    # Log the recommendation
    entries.append({
        "ts": now_local_str("%Y-%m-%d %H:%M"),
        "title": title,
        "url": url,
        "category": category,
    })
    entries = entries[-MAX_ENTRIES:]
    write_jsonl(LOG_FILE, entries)

    # Build interactive card for Lark
    header_text = "📺 推荐"
    if category:
        header_text += f" | {category}"
    duration = data.get("duration", "")
    if duration:
        header_text += f" ({duration})"

    buttons = []
    if url:
        buttons.append({"text": "去看看", "url": url})
        # Callback button RE-ENABLED 2026-06-12: the app's callback config is
        # published and the single-connection sidecar (lark_event_sidecar.py,
        # JARVIS_EVENT_BACKEND=sidecar) ACKs card.action.trigger inline.
        # (lark-cli ≤1.0.52 still can't consume it — the sidecar is required.)
        buttons.append({
            "text": "收藏",
            "value": {"action": "watchlater", "title": title, "url": url},
        })

    # Use build_card for header + body, then add note element manually
    card_str = build_card(
        header_text, user_message, buttons if buttons else None,
        source="content-recommend",
        work_receipt="完成内容筛选、链接校验和历史推荐去重",
    )
    if url:
        # Inject note element before closing the elements array
        card_obj = json.loads(card_str)
        card_obj["elements"].append({
            "tag": "note",
            "elements": [{"tag": "plain_text",
                          "content": "点任意表情👍即可收藏稍后看；回复'收藏'也行"}],
        })
        card_str = json.dumps(card_obj, ensure_ascii=False)

    # Output card JSON as single line
    print(card_str)
    return 0


if __name__ == "__main__":
    sys.exit(main())
