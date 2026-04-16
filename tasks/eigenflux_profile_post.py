#!/usr/bin/env python3
"""Post-hook: update EigenFlux profile if Claude decided to."""
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
    if not raw or "HEARTBEAT_OK" in raw:
        return 0

    raw = re.sub(r'^```json?\s*', '', raw)
    raw = re.sub(r'```\s*$', '', raw)

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"[eigenflux-profile] JSON parse failed: {e}", file=sys.stderr)
        return 0

    if not data.get("should_update"):
        return 0

    agent_name = data.get("agent_name")
    bio = data.get("bio")
    if not agent_name and not bio:
        print("[eigenflux-profile] should_update=true but no fields to update", file=sys.stderr)
        return 0

    client = EigenFluxClient(str(JARVIS_DIR / "eigenflux"))
    try:
        result = client.update_profile(agent_name=agent_name, bio=bio)
    except Exception:
        print("[eigenflux-profile] update_profile raised:", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return 0

    if result.get("code") == 0:
        reason = data.get("reason", "")
        print(f"EigenFlux profile updated. {reason}".strip())
    else:
        print(f"[eigenflux-profile] API err: code={result.get('code')} msg={result.get('msg')}",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
