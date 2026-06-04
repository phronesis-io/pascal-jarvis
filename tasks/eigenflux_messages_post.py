#!/usr/bin/env python3
"""Post-hook for eigenflux-messages: send replies back to EigenFlux.

Stdin: Claude's response (JSON with reply_actions and user_message).
Stdout: user_message (forwarded to Pascal via Lark) or empty if nothing to send.
"""

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.card import build_card
from core.safety import looks_like_error, parse_json_response


def _auto_reply_pm_enabled() -> bool:
    """Honor EigenFlux's `auto_reply_pm` switch before sending any autonomous PM
    reply. The switch is console-controllable and synced down to the local config
    after every feed poll. Per the upstream agent contract, only an explicit
    `false` suppresses auto-reply; unset/unknown/`true` all default to ON.

    This is the single choke point where Jarvis replies to a PM *sender* (the
    real-time stream path only surfaces messages to Pascal), so gating here is
    sufficient to make the whole bot honor the switch.
    """
    try:
        r = subprocess.run(
            ["eigenflux", "config", "get", "--key", "auto_reply_pm"],
            capture_output=True, text=True, timeout=10,
        )
        return "false" not in (r.stdout or "").lower()
    except Exception:
        return True  # fail open: never silently swallow replies on a probe error


def main() -> int:
    message = sys.stdin.read().strip()
    if not message or message == "HEARTBEAT_OK":
        return 0
    if looks_like_error(message):
        print("[eigenflux-messages] skipping — looks like error output", file=sys.stderr)
        return 0

    data = parse_json_response(message)
    if data is None:
        # Never emit raw JSON — only pass through human-readable text
        import re
        text = re.sub(r'\{[^{}]*\}', '', message).strip()
        if text:
            print(text)
        return 0

    reply_actions = data.get("reply_actions", [])
    user_message = data.get("user_message", "")

    # Honor the auto_reply_pm switch: when off, do NOT reply to the sender —
    # surface to Pascal and let him decide. Annotate so he knows replies were held.
    if reply_actions and not _auto_reply_pm_enabled():
        held = len(reply_actions)
        reply_actions = []
        note = f"（EigenFlux auto_reply_pm 已关闭：{held} 条自动回复已暂停，需要回复请告诉我。）"
        user_message = f"{user_message}\n\n{note}".strip() if user_message else note
        print(f"[eigenflux-messages] auto_reply_pm=false — held {held} repl(y/ies)", file=sys.stderr)

    # Send each reply via eigenflux CLI
    for action in reply_actions:
        receiver_id = action.get("receiver_id", "")
        content = action.get("content", "")
        if not receiver_id or not content:
            continue

        cmd = [
            "eigenflux", "msg", "send",
            "--content", content,
            "--receiver-id", receiver_id,
        ]
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=30
            )
            if result.returncode != 0:
                print(
                    f"[eigenflux-messages] send failed for {receiver_id}: {result.stderr.strip()}",
                    file=sys.stderr,
                )
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            print(f"[eigenflux-messages] send error: {e}", file=sys.stderr)

    # Output user_message as Lark card
    if user_message:
        print(build_card("📡 EigenFlux · 消息", user_message, source="eigenflux-messages"))

    return 0


if __name__ == "__main__":
    sys.exit(main())
