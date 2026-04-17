#!/usr/bin/env python3
"""Post-hook: update EigenFlux profile via CLI if Claude decided to."""
import json
import os
import re
import subprocess
import sys
import traceback

LOG = open(os.environ.get("LOG_FILE", os.devnull), "a")
PATH_ENV = os.environ.get("PATH", "") + ":" + os.path.expanduser("~/.local/bin")


def main() -> int:
    raw = sys.stdin.read().strip()
    if not raw or "HEARTBEAT_OK" in raw:
        return 0

    raw = re.sub(r'^```json?\s*', '', raw)
    raw = re.sub(r'```\s*$', '', raw)

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"[eigenflux-profile] JSON parse failed: {e}", file=LOG)
        return 0

    if not data.get("should_update"):
        return 0

    agent_name = data.get("agent_name")
    bio = data.get("bio")
    if not agent_name and not bio:
        print("[eigenflux-profile] should_update=true but no fields", file=LOG)
        return 0

    cmd = ["eigenflux", "profile", "update", "-f", "json"]
    if agent_name:
        cmd.extend(["--name", agent_name])
    if bio:
        cmd.extend(["--bio", bio])

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            env={**os.environ, "PATH": PATH_ENV},
        )
        if result.returncode == 0:
            reason = data.get("reason", "")
            print(f"EigenFlux profile updated. {reason}".strip())
        else:
            print(f"[eigenflux-profile] CLI error: {result.stderr.strip()}", file=LOG)
    except Exception:
        print("[eigenflux-profile] raised:", file=LOG)
        traceback.print_exc(file=LOG)

    return 0


if __name__ == "__main__":
    sys.exit(main())
