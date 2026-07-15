#!/usr/bin/env python3
"""Post-hook for `metrics-digest` — render probe records as Lark cards.

Watermark handshake with the pre-hook: .digest_pending.json (staged by pre)
is promoted to .digest_watermark.json only on a well-formed Claude reply —
including HEARTBEAT_OK (a valid "nothing card-worthy"). On error-shaped or
unparsable output the pending file is left alone, so the next cycle re-emits
the same records instead of silently losing the day's digest.
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.card import build_card
from core.safety import looks_like_error, parse_json_response


def _metrics_dir() -> Path:
    jd = os.environ.get("JARVIS_DIR") or str(Path(__file__).resolve().parent.parent)
    return Path(jd) / "data" / "metrics"


def _promote_watermark():
    mdir = _metrics_dir()
    pending = mdir / ".digest_pending.json"
    if pending.exists():
        try:
            os.replace(pending, mdir / ".digest_watermark.json")
        except OSError as e:
            print(f"[metrics_digest] watermark promote failed: {e}", file=sys.stderr)


def main() -> int:
    raw = sys.stdin.read().strip()
    if not raw:
        return 0
    if raw == "HEARTBEAT_OK":
        _promote_watermark()
        return 0
    if looks_like_error(raw):
        print("[metrics_digest] skipping — looks like error output; "
              "records will re-emit next cycle", file=sys.stderr)
        return 0

    parsed = parse_json_response(raw)
    if parsed is None:
        print("[metrics_digest] no JSON envelope; records will re-emit "
              "next cycle", file=sys.stderr)
        return 0

    cards = parsed.get("cards")
    for card in cards if isinstance(cards, list) else []:
        if not isinstance(card, dict):
            continue
        header = str(card.get("header") or "").strip()
        body = str(card.get("body") or "").strip()
        if header and body:
            print(build_card(header, body, source="metrics-digest"))
    _promote_watermark()
    return 0


if __name__ == "__main__":
    sys.exit(main())
