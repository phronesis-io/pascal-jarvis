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

    notes_str = json.dumps(notes) if isinstance(notes, dict) else str(notes)
    url = data.get("source_url") or data.get("url", "")

    # Save pending broadcast for user confirmation (don't publish directly)
    pending_dir = JARVIS_DIR / "eigenflux" / "pending_publish"
    pending_dir.mkdir(parents=True, exist_ok=True)
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

    # Send to user for confirmation via Lark
    summary = notes.get("summary", "") if isinstance(notes, dict) else ""
    btype = notes.get("type", "info") if isinstance(notes, dict) else "info"
    domains = notes.get("domains", []) if isinstance(notes, dict) else []
    domain_str = ", ".join(domains) if domains else ""

    preview = f"**类型**: {btype}"
    if domain_str:
        preview += f" | **领域**: {domain_str}"
    preview += f"\n\n{content}\n\n"
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
        body=preview, options=options, send=False,
        context=f"pending_publish id={pending_id}",
    )
    print(memorial.card_json(mem_id))
    print(f"[eigenflux-publish] Pending approval: {pending_id} — {content[:80]}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
