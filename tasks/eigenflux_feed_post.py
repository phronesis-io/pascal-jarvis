#!/usr/bin/env python3
"""Post-hook: submit feedback to EigenFlux via CLI, output user message as Lark card."""
import json
import os
import subprocess
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.card import build_card, build_rich_card
from core.safety import parse_json_response, summarize

LOG = open(os.environ.get("LOG_FILE", os.devnull), "a")
PATH = os.environ.get("PATH", "") + ":" + os.path.expanduser("~/.local/bin")


def run_eigenflux(*args: str, stdin_data: str | None = None) -> dict:
    result = subprocess.run(
        ["eigenflux", *args, "-f", "json"],
        capture_output=True, text=True,
        env={**os.environ, "PATH": PATH},
        input=stdin_data,
    )
    if result.returncode != 0:
        print(f"[eigenflux-feed] CLI error: {result.stderr.strip()}", file=LOG)
        return {}
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return {}


def main() -> int:
    raw = sys.stdin.read().strip()
    if not raw:
        return 0

    data = parse_json_response(raw)
    if data is None:
        print("[eigenflux-feed] JSON parse failed", file=LOG)
        return 0

    # Submit feedback scores via CLI
    fb = data.get("feedback", [])
    if fb:
        items = []
        for i in fb:
            try:
                items.append({"item_id": int(i["item_id"]), "score": int(i["score"])})
            except (ValueError, KeyError, TypeError) as e:
                print(f"[eigenflux-feed] bad feedback entry {i!r}: {e}", file=LOG)
        if items:
            try:
                resp = run_eigenflux("feed", "feedback", "--items", json.dumps(items))
                print(f"[eigenflux-feed] {len(items)} items scored", file=LOG)
            except Exception:
                print("[eigenflux-feed] feedback submission failed:", file=LOG)
                traceback.print_exc(file=LOG)

    # Queue items flagged for deep research
    research_queue = Path(os.environ.get("JARVIS_DIR", Path(__file__).resolve().parent.parent)) / "eigenflux" / "needs_research.jsonl"
    research_queue.parent.mkdir(parents=True, exist_ok=True)
    queued = 0
    for item in fb:
        if item.get("needs_research") and item.get("action") == "hold" and int(item.get("score", 0)) >= 1:
            from datetime import datetime, timezone
            entry = {
                "item_id": str(item["item_id"]),
                "queued_at": datetime.now(timezone.utc).isoformat(),
                "score": int(item["score"]),
                "reason": item.get("reason", ""),
            }
            with open(research_queue, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            queued += 1
    if queued:
        print(f"[eigenflux-feed] {queued} items queued for research", file=LOG)

    # Output user message as Lark card with richview
    msg = str(data.get("user_message", "")).strip()
    if msg:
        print(build_rich_card(
            header="📡 EigenFlux",
            summary=summarize(msg),
            sections=[{"type": "markdown", "content": msg}],
            meta={"source": "eigenflux_feed"},
        source="eigenflux-feed",
    ))
    return 0


if __name__ == "__main__":
    sys.exit(main())
