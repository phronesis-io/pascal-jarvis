"""Perceived-responsiveness policy for the message activity stream (REQ-59).

The investigation on 2026-06-15 (real-log latency breakdown) found the wait
after a user message is the MODEL, not the pipeline: Event→claude spawn is
p50 1s, but claude(opus) generation is p50 103s / p90 414s (8/10 >60s). Pascal
chose to keep opus (quality) and instead make the WAIT feel alive. This module
is the single, TESTED source of truth for that feedback policy — bot.sh's
activity-stream subshell mirrors/consumes it (the bash equivalent was
previously untested inline code, the heartbeat_loop migration precedent: move
logic to Python where it can be tested).

Policy:
  - The instant "Typing" reaction fires at dispatch.
  - If work is still running after ACK_AFTER_S, send one short, natural
    progress line. Do not expose tools, providers, retries, IDs or logs.
  - A call still running past PROMOTE_AFTER_S becomes a background job and the
    conversation is released with one plain-language notice.

CLI (consumed by bot.sh):
    python3 -m core.responsiveness env                  # shell-evalable consts
    python3 -m core.responsiveness decide <elapsed> <ack_sent>  # ack|none
    python3 -m core.responsiveness poll <index>         # seconds for poll N
"""

from __future__ import annotations

import sys

# Tunables (single source of truth — bot.sh reads these via `env`).
POLL_FIRST_S = 10
POLL_STEADY_S = 10
ACK_AFTER_S = 20
PROMOTE_AFTER_S = 90
PROGRESS_ACK = "我还在处理，查清楚后马上告诉你。"


def poll_interval(poll_index: int) -> int:
    """Seconds to wait before poll N (0-indexed): first fast, then steady."""
    return POLL_FIRST_S if poll_index <= 0 else POLL_STEADY_S


def decide_action(elapsed_s: int, ack_sent: bool = False) -> str:
    """Return one user-visible action for an in-flight reply."""
    return "ack" if elapsed_s >= ACK_AFTER_S and not ack_sent else "none"


def _as_bool(s: str) -> bool:
    return str(s).strip() not in ("", "0", "false", "False", "no")


def main(argv: list[str]) -> int:
    cmd = argv[0] if argv else "env"
    if cmd == "env":
        # Shell-evalable; bot.sh does: eval "$(python3 -m core.responsiveness env)"
        print(f"JV_POLL_FIRST={POLL_FIRST_S}")
        print(f"JV_POLL_STEADY={POLL_STEADY_S}")
        print(f"JV_ACK_AFTER={ACK_AFTER_S}")
        print(f"JV_PROMOTE_AFTER={PROMOTE_AFTER_S}")
        print(f"JV_PROGRESS_ACK='{PROGRESS_ACK}'")
        return 0
    if cmd == "decide":
        elapsed = int(argv[1]) if len(argv) > 1 and argv[1].isdigit() else 0
        ack_sent = _as_bool(argv[2]) if len(argv) > 2 else False
        print(decide_action(elapsed, ack_sent))
        return 0
    if cmd == "poll":
        idx = int(argv[1]) if len(argv) > 1 and argv[1].lstrip("-").isdigit() else 0
        print(poll_interval(idx))
        return 0
    print(f"unknown command: {cmd} (env|decide|poll)", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
