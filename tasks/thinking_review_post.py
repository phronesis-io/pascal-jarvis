#!/usr/bin/env python3
"""Post-hook: extract user_message from thinking-review JSON response."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.card import build_rich_card
from core.safety import looks_like_error, parse_json_response


def main() -> int:
    raw = sys.stdin.read().strip()
    if not raw or raw == "HEARTBEAT_OK":
        return 0
    if looks_like_error(raw):
        return 0

    data = parse_json_response(raw)
    if data is None:
        # If Claude returned plain text assessment, don't forward it
        # (it's diagnostic text like "All question files are only 3 days old")
        return 0
    message = data.get("user_message", "").strip()

    if not message:
        return 0

    print(build_rich_card(
        header="🤔 思考回顾",
        summary=message[:200],
        sections=[{"type": "markdown", "content": message}],
        source="thinking-review",
        work_receipt="回看近期思考记录、完成主题聚类和后续价值判断",
    ))
    return 0


if __name__ == "__main__":
    sys.exit(main())
