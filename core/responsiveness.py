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
  - The instant "Typing" reaction fires at dispatch (bot.sh). That reaction IS
    the sign of life while opus thinks — there is NO textual "received,
    thinking" ack. (Pascal 2026-06-20: the 💭 ack was redundant with the typing
    indicator and felt annoying. Removed.)
  - First activity poll is FAST (POLL_FIRST_S); subsequent polls settle to
    POLL_STEADY_S to avoid spam.
  - Each poll, while claude is still running, decide_action picks ONE of:
      'narrate' — new tool calls appeared → 🔧 narrate what it's doing
      'none'    — nothing new to say (pure thinking stays silent; the Typing
                  reaction already shows it's alive)
  - A call still running past PROMOTE_AFTER_S becomes a background job.

CLI (consumed by bot.sh):
    python3 -m core.responsiveness env                  # shell-evalable consts
    python3 -m core.responsiveness decide <tools>       # narrate|none
    python3 -m core.responsiveness poll <index>         # seconds for poll N
"""

from __future__ import annotations

import sys

# Tunables (single source of truth — bot.sh reads these via `env`).
POLL_FIRST_S = 6      # first poll: fast, so feedback lands within ~6s
POLL_STEADY_S = 20    # subsequent polls: settle to avoid message spam
PROMOTE_AFTER_S = 120  # a call past this becomes a background job


def poll_interval(poll_index: int) -> int:
    """Seconds to wait before poll N (0-indexed): first fast, then steady."""
    return POLL_FIRST_S if poll_index <= 0 else POLL_STEADY_S


def decide_action(has_new_tools: bool) -> str:
    """Pick the feedback for this poll: 'narrate' | 'none'.

    Tool narration is the only textual sign of life we emit; pure thinking
    stays silent because the instant Typing reaction already covers it.
    """
    return "narrate" if has_new_tools else "none"


def _as_bool(s: str) -> bool:
    return str(s).strip() not in ("", "0", "false", "False", "no")


def main(argv: list[str]) -> int:
    cmd = argv[0] if argv else "env"
    if cmd == "env":
        # Shell-evalable; bot.sh does: eval "$(python3 -m core.responsiveness env)"
        print(f"JV_POLL_FIRST={POLL_FIRST_S}")
        print(f"JV_POLL_STEADY={POLL_STEADY_S}")
        print(f"JV_PROMOTE_AFTER={PROMOTE_AFTER_S}")
        return 0
    if cmd == "decide":
        has_tools = _as_bool(argv[1]) if len(argv) > 1 else False
        print(decide_action(has_tools))
        return 0
    if cmd == "poll":
        idx = int(argv[1]) if len(argv) > 1 and argv[1].lstrip("-").isdigit() else 0
        print(poll_interval(idx))
        return 0
    print(f"unknown command: {cmd} (env|decide|poll)", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
