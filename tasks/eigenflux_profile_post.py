#!/usr/bin/env python3
"""Post-hook: update EigenFlux profile via CLI if Claude decided to."""
import os
import subprocess
import sys
import traceback

sys.path.insert(0, str(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")))
from core.safety import parse_json_response

LOG = open(os.environ.get("LOG_FILE", os.devnull), "a")
PATH_ENV = os.environ.get("PATH", "") + ":" + os.path.expanduser("~/.local/bin")


def main() -> int:
    raw = sys.stdin.read().strip()
    if not raw or "HEARTBEAT_OK" in raw:
        return 0

    data = parse_json_response(raw)
    if data is None:
        print("[eigenflux-profile] JSON parse failed", file=LOG)
        return 0

    if not data.get("should_update"):
        return 0

    # Coerce to str: subprocess args must all be str, and the LLM occasionally
    # emits a non-string here. Strip so a whitespace-only value counts as absent.
    agent_name = str(data.get("agent_name") or "").strip()
    bio = str(data.get("bio") or "").strip()
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
            cmd, capture_output=True, text=True, timeout=60,
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
