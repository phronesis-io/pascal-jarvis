#!/usr/bin/env python3
"""Post-hook: publish to EigenFlux via CLI if Claude decided to."""
import json
import os
import subprocess
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.safety import parse_json_response

LOG = open(os.environ.get("LOG_FILE", os.devnull), "a")
PATH_ENV = os.environ.get("PATH", "") + ":" + os.path.expanduser("~/.local/bin")
JARVIS_DIR = Path(os.environ.get("JARVIS_DIR", Path(__file__).resolve().parent.parent))


def main() -> int:
    raw = sys.stdin.read().strip()
    if not raw:
        return 0

    data = parse_json_response(raw)
    if data is None:
        print("[eigenflux-publish] JSON parse failed", file=LOG)
        return 0

    if not data.get("should_publish"):
        return 0

    content = data.get("content")
    notes = data.get("notes")
    if not content or not notes:
        print("[eigenflux-publish] missing content or notes", file=LOG)
        return 0

    btype = notes.get("type", "") if isinstance(notes, dict) else ""
    if btype not in ("supply", "demand", "insight"):
        print(f"[eigenflux-publish] dropping broadcast with unsupported type "
              f"{btype!r} — only supply/demand/insight allowed", file=LOG)
        return 0

    # Owner-facing one-line Chinese summary (SUMMARY_CN, drafted alongside
    # the English broadcast). Consumed only by the confirmation card below;
    # popped so the outbound broadcast notes stay byte-identical to before.
    summary_cn = ""
    if isinstance(notes, dict):
        summary_cn = " ".join(str(notes.pop("summary_cn", "") or "").split())

    notes_str = json.dumps(notes) if isinstance(notes, dict) else str(notes)
    url = data.get("source_url") or data.get("url", "")

    # Save pending broadcast for user confirmation (don't publish directly)
    pending_dir = JARVIS_DIR / "eigenflux" / "pending_publish"
    pending_dir.mkdir(parents=True, exist_ok=True)

    # Backlog gate, second layer (pre.sh is the first): while another
    # broadcast younger than 48h still awaits the user's 发/取消, drop this
    # draft instead of stacking one more card on the unanswered pile.
    now_epoch = time.time()
    from core.eigenflux_publish import reconcile_pending_drafts
    reconcile_pending_drafts(JARVIS_DIR, now=now_epoch)
    backlog = [f for f in pending_dir.glob("*.json")
               if now_epoch - f.stat().st_mtime <= 48 * 3600]
    if backlog:
        print(f"[eigenflux-publish] {len(backlog)} broadcast(s) already awaiting "
              f"approval — dropping new draft: {content[:80]}", file=sys.stderr)
        return 0

    pending_id = f"{int(time.time())}_{os.getpid()}"
    pending_file = pending_dir / f"{pending_id}.json"

    pending_data = {
        "id": pending_id,
        "content": content,
        "notes": notes,
        "url": url,
        "created_at": int(time.time()),
    }
    from core.safety import atomic_write
    atomic_write(pending_file, json.dumps(pending_data, ensure_ascii=False))

    # Send to user for confirmation via Lark. First line = Chinese and says
    # what this card wants (card-style contract: 第一句结论, no English
    # metadata labels — the old「**类型**: insight | **领域**: …」opener was
    # machine self-narration). The draft's summary leads when it is Chinese;
    # the broadcast body itself is English by design, so it is framed
    # honestly below the Chinese opener.
    summary = " ".join(str(
        notes.get("summary", "") if isinstance(notes, dict) else "").split())

    def _cjk(text: str) -> bool:
        return any("一" <= ch <= "鿿" for ch in text)

    if summary_cn and _cjk(summary_cn):
        first_line = f"想对外广播这条：{summary_cn}"
    elif summary and _cjk(summary):
        first_line = f"想对外广播这条：{summary}"
    else:
        first_line = "想对外发这条广播（英文原文如下）："
    preview = f"{first_line}\n\n{content}\n\n"
    # Render the source as a tappable link. The broadcast body often names a
    # source (e.g. "arXiv 2606.02859") without a clickable URL — surface it so
    # the user can actually open it from the card. (Never a bare URL.)
    src = url or (notes.get("source") if isinstance(notes, dict) else "") or ""
    if src.startswith("http"):
        preview += f"🔗 来源：[{src}]({src})\n\n"
    from core import memorial
    options = [
        {"key": "publish", "label": "发（确认广播）",
         "action": {"type": "eigenflux_publish",
                    "params": {"id": pending_id}}},
        {"key": "cancel", "label": "不发（取消）",
         "action": {"type": "eigenflux_cancel_publish",
                    "params": {"id": pending_id}}},
    ]
    mem_id, _ = memorial.create(
        source="eigenflux-publish", title="EigenFlux 广播待确认",
        work_receipt="完成广播草稿整理、来源绑定和发布参数校验",
        owner_need="authority",
        why_now="广播草稿已经准备完成，对外发布仍需本人授权",
        owner_action="确认发布，或取消这份广播草稿",
        silence_cost="不提示会让已经准备好的对外广播停在未授权状态",
        body=preview, options=options, send=False,
        context=f"pending_publish id={pending_id}",
        # This card carries 发/不发 buttons: it IS a decision. Explicit so the
        # engagement governor can never demote it to a「知道就行」banner while
        # the buttons still demand an answer (2026-08-24 audit).
        attention="decision",
    )
    # Make the draft and its approval card one lifecycle. The pre-hook uses
    # this link to file the card as 留中 when an unanswered draft expires.
    pending_data["memorial_id"] = mem_id
    atomic_write(pending_file, json.dumps(pending_data, ensure_ascii=False))
    print(memorial.pipeline_card_json(mem_id))
    print(f"[eigenflux-publish] Pending approval: {pending_id} — {content[:80]}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
