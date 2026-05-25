#!/usr/bin/env python3
"""Post-hook for Phronesis group monitor — format as Lark card and gate noise."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.card import build_card, build_rich_card
from core.safety import looks_like_error


def main() -> int:
    raw = sys.stdin.read().strip()
    if not raw or raw == "HEARTBEAT_OK":
        return 0
    if looks_like_error(raw):
        print("[counsel] skipping — looks like error output", file=sys.stderr)
        return 0

    # Output as rich card with full content in web view
    summary_lines = raw.strip().splitlines()[:4]
    summary = "\n".join(summary_lines)
    if len(raw.strip().splitlines()) > 4:
        summary += "\n..."
    print(build_rich_card(
        header="🏛️ Phronesis",
        summary=summary,
        sections=[{"type": "markdown", "content": raw}],
        meta={"source": "phronesis_monitor"},
    source="phronesis-monitor",
))
    return 0


if __name__ == "__main__":
    sys.exit(main())
