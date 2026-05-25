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
from core.safety import looks_like_error
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
    cleaned = re.sub(r"^```json?\s*", "", raw)
    cleaned = re.sub(r"```\s*$", "", cleaned.strip())

    # Try to find JSON substring if direct parse fails
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find('{')
        end = cleaned.rfind('}')
        if start >= 0 and end > start:
            try:
                data = json.loads(cleaned[start:end + 1])
            except json.JSONDecodeError:
                data = None
        else:
            data = None
    if data is None:
        # Never emit raw JSON — extract human-readable text only
        text = re.sub(r'\{[^{}]*\}', '', raw).strip()
        if text and "http" in text:
            print(build_card("📺 推荐", text))
        return 0

    url = data.get("url", "")
    title = data.get("title", "")
    user_message = data.get("user_message", "")
    category = data.get("category", "")

    if not user_message and not url:
        return 0

    # Check for duplicate URL
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    entries: list[dict] = []
    if LOG_FILE.exists():
        for line in LOG_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue

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

    tmp = LOG_FILE.with_suffix(".jsonl.tmp")
    tmp.write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in entries) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, LOG_FILE)

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
        buttons.append({
            "text": "收藏",
            "value": {"action": "watchlater", "title": title, "url": url},
        })

    # Use build_card for header + body, then add note element manually
    card_str = build_card(header_text, user_message, buttons if buttons else None)
    if url:
        # Inject note element before closing the elements array
        card_obj = json.loads(card_str)
        card_obj["elements"].append({
            "tag": "note",
            "elements": [{"tag": "plain_text", "content": "回复'收藏'也可以稍后看"}],
        })
        card_str = json.dumps(card_obj, ensure_ascii=False)

    # Output card JSON as single line
    print(card_str)
    return 0


if __name__ == "__main__":
    sys.exit(main())
