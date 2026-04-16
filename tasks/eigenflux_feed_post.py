#!/usr/bin/env python3
"""Post-hook: submit feedback to EigenFlux, output user message to Lark."""
import json
import os
import re
import sys
import traceback
from pathlib import Path

JARVIS_DIR = Path(os.environ.get("JARVIS_DIR",
                                 Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(JARVIS_DIR))

from plugins.eigenflux.client import EigenFluxClient


def main() -> int:
    raw = sys.stdin.read().strip()
    if not raw:
        return 0

    raw = re.sub(r'^```json?\s*', '', raw)
    raw = re.sub(r'```\s*$', '', raw)

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"[eigenflux-feed] JSON parse failed: {e}", file=sys.stderr)
        print(f"[eigenflux-feed] raw head: {raw[:200]!r}", file=sys.stderr)
        return 0

    # Submit feedback scores
    fb = data.get("feedback", [])
    if fb:
        client = EigenFluxClient(str(JARVIS_DIR / "eigenflux"))
        items = []
        for i in fb:
            try:
                items.append({"item_id": int(i["item_id"]), "score": int(i["score"])})
            except (ValueError, KeyError, TypeError) as e:
                print(f"[eigenflux-feed] bad feedback entry {i!r}: {e}", file=sys.stderr)
        if items:
            try:
                resp = client.submit_feedback(items)
                if resp.get("code") != 0:
                    print(f"[eigenflux-feed] feedback API err: code={resp.get('code')} msg={resp.get('msg')}",
                          file=sys.stderr)
                else:
                    print(f"[eigenflux-feed] {len(items)} items scored", file=sys.stderr)
            except Exception:
                print("[eigenflux-feed] submit_feedback raised:", file=sys.stderr)
                traceback.print_exc(file=sys.stderr)

    # Output user message (this becomes the Lark reply)
    msg = str(data.get("user_message", "")).strip()
    if msg:
        print(msg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
