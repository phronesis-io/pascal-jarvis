#!/usr/bin/env python3
"""Post-hook for eigenflux-friends: handle friend request actions and notify user.

Stdin: Claude's response (JSON with actions and user_message).
Stdout: user_message (forwarded to Pascal via Lark) or empty if nothing to send.
"""

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.card import build_card
from core.safety import looks_like_error


def main() -> int:
    message = sys.stdin.read().strip()
    if not message or message == "HEARTBEAT_OK":
        return 0
    if looks_like_error(message):
        print("[eigenflux-friends] skipping — looks like error output", file=sys.stderr)
        return 0

    try:
        data = json.loads(message)
    except json.JSONDecodeError:
        print(message)
        return 0

    actions = data.get("actions", [])
    user_message = data.get("user_message", "")

    # Execute accept/reject actions
    for action in actions:
        request_id = action.get("request_id", "")
        decision = action.get("decision", "")  # accept or reject
        remark = action.get("remark", "")
        if not request_id or not decision:
            continue

        cmd = [
            "eigenflux", "relation", "handle",
            "--request-id", str(request_id),
            "--action", decision,
        ]
        if remark:
            cmd.extend(["--remark", remark])
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=30
            )
            if result.returncode != 0:
                print(
                    f"[eigenflux-friends] handle failed for {request_id}: {result.stderr.strip()}",
                    file=sys.stderr,
                )
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            print(f"[eigenflux-friends] handle error: {e}", file=sys.stderr)

    # Output user_message as Lark card
    if user_message:
        print(build_card("📡 EigenFlux · 好友申请", user_message))

    return 0


if __name__ == "__main__":
    sys.exit(main())
