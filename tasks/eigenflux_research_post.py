#!/usr/bin/env python3
"""Post-hook: process research decisions, clear queue, output actionable items as card."""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.card import build_card, build_rich_card
from core.safety import parse_json_response, summarize

JARVIS_DIR = Path(os.environ.get("JARVIS_DIR", Path(__file__).resolve().parent.parent))
RESEARCH_QUEUE = JARVIS_DIR / "eigenflux" / "needs_research.jsonl"

LOG = open(os.environ.get("LOG_FILE", os.devnull), "a")


def main() -> int:
    raw = sys.stdin.read().strip()
    if not raw or "HEARTBEAT_OK" in raw:
        return 0

    data = parse_json_response(raw)
    if data is None:
        print("[eigenflux-research] JSON parse failed", file=LOG)
        return 0

    decisions = data.get("decisions", [])

    # Track which items were processed
    processed_ids = set()
    for d in decisions:
        processed_ids.add(str(d.get("item_id", "")))

    # Remove processed items from queue
    if processed_ids and RESEARCH_QUEUE.exists():
        remaining = []
        for line in RESEARCH_QUEUE.read_text(encoding="utf-8").strip().split("\n"):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
                if str(entry.get("item_id", "")) not in processed_ids:
                    remaining.append(line)
            except json.JSONDecodeError:
                remaining.append(line)

        if remaining:
            RESEARCH_QUEUE.write_text("\n".join(remaining) + "\n", encoding="utf-8")
        else:
            RESEARCH_QUEUE.write_text("", encoding="utf-8")

        processed = len(processed_ids)
        print(f"[eigenflux-research] {processed} items processed, {len(remaining)} remaining", file=LOG)

    # Output user message as card (only for items decided as "push")
    msg = str(data.get("user_message", "")).strip()
    if msg:
        print(build_rich_card(
            header="📡 EigenFlux 深度",
            summary=summarize(msg),
            sections=[{"type": "markdown", "content": msg}],
            meta={"source": "eigenflux_research"},
        source="eigenflux-research",
    ))
    return 0


if __name__ == "__main__":
    sys.exit(main())
