#!/usr/bin/env python3
"""Post-hook for Phronesis group monitor — format as Lark card and gate noise."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "repos" / "pascal-jarvis"))
from core.card import build_card
from core.safety import looks_like_error


def main() -> int:
    raw = sys.stdin.read().strip()
    if not raw or raw == "HEARTBEAT_OK":
        return 0
    if looks_like_error(raw):
        print("[counsel] skipping — looks like error output", file=sys.stderr)
        return 0

    # Output as card
    print(build_card("🏛️ Phronesis", raw))
    return 0


if __name__ == "__main__":
    sys.exit(main())
