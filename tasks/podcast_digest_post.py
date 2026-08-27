#!/usr/bin/env python3
"""Post-hook: daily podcast digest card.

The task writes a Feishu doc itself (that is where the real summary lives —
a phone cannot read a local .md), then hands this hook the doc URL plus a
short teaser. This hook does three things and nothing else:

  1. Marks the episode seen, so tomorrow moves on to the next one.
  2. Stamps the day, so the 07:00-10:00 window fires at most one card.
  3. Emits ONE 知会级 card: a pointer + why it is worth his 5 minutes.

It is a notice, not a decision — he does not have to approve anything, so it
must not carry 批红 options (REQ: 信封与正文角色对账).
"""

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import podcasts
from core.safety import (atomic_write, looks_like_error, parse_json_response,
                         strip_task_framing)

MAX_BODY_CHARS = 400
STAMP = Path(podcasts.DATA_DIR) / "podcast_digest_day.txt"

OPTIONS = [
    {"key": "ack", "label": "知道了", "action": None},
    {"key": "more", "label": "这类的多来点", "action": None, "reply": True},
    {"key": "less", "label": "这个节目别推了", "action": None, "reply": True},
]


def _stamp_today() -> None:
    STAMP.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(STAMP, date.today().isoformat())


def main() -> int:
    raw = sys.stdin.read().strip()
    if not raw or "HEARTBEAT_OK" in raw or looks_like_error(raw):
        return 0

    parsed = parse_json_response(raw)
    if not isinstance(parsed, dict):
        return 0

    body = strip_task_framing(
        str(parsed.get("user_message") or "").strip())[:MAX_BODY_CHARS].strip()
    doc_url = str(parsed.get("doc_url") or "").strip()
    video_id = str(parsed.get("video_id") or "").strip()
    title = str(parsed.get("title") or "今天这期播客").strip()[:20]

    # No doc means the digest itself does not exist; a card pointing nowhere is
    # worse than no card, and the episode stays unseen so tomorrow retries it.
    if not body or not doc_url:
        print("[podcast-digest] missing body or doc_url — no card",
              file=sys.stderr)
        return 0

    if video_id:
        podcasts.mark(video_id, doc_url)
    _stamp_today()

    if doc_url not in body:
        body = f"{body}\n\n[全文摘要]({doc_url})"

    try:
        from core import memorial
        mem_id, _ = memorial.create(
            source="podcast-digest", title=title, body=body,
            work_receipt=(f"已下载并通读该期官方字幕全文，摘要写入飞书文档 {doc_url}"),
            options=OPTIONS, authoring_protocol=True, send=False,
            attention="notice",
            context="每日播客摘要：他 2026-08-27 亲口要的「每天搞一点，我简单看看，别让我错过」。知会级，不需要他拍板。",
        )
        print(memorial.card_json(mem_id))
    except Exception as exc:  # memorial must not be a single point of failure
        print(f"[podcast-digest] memorial failed, plain card: {exc}",
              file=sys.stderr)
        from core.card import build_card
        print(build_card(
            f"🎧 {title}", body, source="podcast-digest",
            work_receipt="已下载并通读该期官方字幕全文并写成飞书摘要",
        ))
    return 0


if __name__ == "__main__":
    sys.exit(main())
