#!/usr/bin/env python3
"""Post-hook for Phronesis group monitor — format as Lark card and gate noise.

Every surfaced card is also appended to data/phronesis_flagged.jsonl — the
monitor's cross-cycle memory. The pre-hook injects the last 24h of these
into the next cycle's prompt so a follow-up ("聊空调") to a serious flag
("工业味+多人头晕", 2026-07-15) is read as a continuation, not isolated
small talk that contradicts the monitor's own judgement 20 minutes earlier.
"""

import json
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.card import build_card, build_rich_card
from core.safety import looks_like_error, sentinel_present, summarize


def main() -> int:
    raw = sys.stdin.read().strip()
    # sentinel_present, not `== "HEARTBEAT_OK"`: the exact match let
    # "...nothing noteworthy.\n\nHEARTBEAT_OK" through as a card (2026-07-15).
    if not raw or sentinel_present(raw):
        return 0
    if looks_like_error(raw):
        print("[counsel] skipping — looks like error output", file=sys.stderr)
        return 0

    # Strip Claude assessment labels that sometimes precede the actual content
    lines = raw.strip().splitlines()
    noise_words = {"noteworthy", "important", "relevant", "actionable", "fyi",
                   "not noteworthy", "routine", "skip"}
    while lines and lines[0].strip().lower() in noise_words:
        lines.pop(0)
    raw = "\n".join(lines)
    if not raw.strip():
        return 0

    # Output as rich card with full content in web view
    card = build_rich_card(
        header="🏛️ Phronesis",
        summary=summarize(raw),
        sections=[{"type": "markdown", "content": raw}],
        meta={"source": "phronesis_monitor"},
        source="phronesis-monitor",
    )
    if not card:  # sentinel gate in the card builder chose silence
        return 0
    print(card)
    _record_flagged(raw)
    return 0


FLAGGED_LEDGER_MAX_LINES = 200


def _record_flagged(text: str):
    """Append the surfaced summary to the cross-cycle flagged ledger."""
    jarvis_dir = Path(os.environ.get("JARVIS_DIR")
                      or Path(__file__).resolve().parent.parent)
    path = jarvis_dir / "data" / "phronesis_flagged.jsonl"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        entry = {"ts": datetime.now().astimezone().isoformat(timespec="seconds"),
                 "summary": " ".join(text.split())[:300]}
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        if len(lines) > FLAGGED_LEDGER_MAX_LINES:
            tmp = path.with_suffix(".tmp")
            tmp.write_text("".join(lines[-FLAGGED_LEDGER_MAX_LINES:]),
                           encoding="utf-8")
            os.replace(tmp, path)
    except OSError as e:
        print(f"[phronesis] flagged ledger append failed: {e}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
