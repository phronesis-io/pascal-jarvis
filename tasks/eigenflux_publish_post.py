#!/usr/bin/env python3
"""Post-hook: publish to EigenFlux if Claude decided to. Errors are logged
to stderr (which bot.sh pipes to jarvis.log) — never silently swallowed.
"""
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
        print(f"[eigenflux-publish] JSON parse failed: {e}", file=sys.stderr)
        print(f"[eigenflux-publish] raw head: {raw[:200]!r}", file=sys.stderr)
        return 0

    if not data.get("should_publish"):
        return 0

    content = data.get("content")
    notes = data.get("notes")
    if not content or not notes:
        print(f"[eigenflux-publish] missing content/notes in response", file=sys.stderr)
        return 0

    client = EigenFluxClient(str(JARVIS_DIR / "eigenflux"))
    try:
        resp = client.publish(content, notes)
    except Exception:
        print("[eigenflux-publish] publish raised exception:", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return 0

    if resp.get("code") == 0:
        print("[eigenflux-publish] published successfully", file=sys.stderr)
    else:
        print(f"[eigenflux-publish] API returned error: code={resp.get('code')} msg={resp.get('msg')}",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
