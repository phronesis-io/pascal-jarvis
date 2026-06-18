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
  - The instant "Typing" reaction already fires at dispatch (bot.sh).
  - First activity poll is FAST (POLL_FIRST_S) so the first textual feedback
    appears within ~6s instead of ~20s; subsequent polls settle to
    POLL_STEADY_S to avoid spam.
  - Each poll, while claude is still running, decide_action picks ONE of:
      'narrate' — new tool calls appeared → 🔧 narrate what it's doing
      'thinking' — no tools yet AND no ack sent → 💭 one-time "received,
                   thinking" so a pure-thinking opus run isn't dead silence
      'none'    — nothing new to say
  - Fast replies (< POLL_FIRST_S) never reach a poll (the loop breaks when the
    claude process exits first), so they get only the instant reaction — no
    redundant ack.
  - A call still running past PROMOTE_AFTER_S becomes a background job.

CLI (consumed by bot.sh):
    python3 -m core.responsiveness env                  # shell-evalable consts
    python3 -m core.responsiveness decide <tools> <sent>  # narrate|thinking|none
    python3 -m core.responsiveness ack-text             # the 💭 ack string
"""

from __future__ import annotations

import shlex
import sys

# Tunables (single source of truth — bot.sh reads these via `env`).
POLL_FIRST_S = 6      # first poll: fast, so feedback lands within ~6s
POLL_STEADY_S = 20    # subsequent polls: settle to avoid message spam
PROMOTE_AFTER_S = 120  # a call past this becomes a background job

# The one-time textual ack when opus is generating with no tool calls. Keep it
# short and natural: recent interaction audits showed extra parenthetical
# explanation made the bot feel noisy instead of reassuring.
THINKING_ACK = "💭 收到了，正在想。"


def poll_interval(poll_index: int) -> int:
    """Seconds to wait before poll N (0-indexed): first fast, then steady."""
    return POLL_FIRST_S if poll_index <= 0 else POLL_STEADY_S


def decide_action(has_new_tools: bool, thinking_sent: bool) -> str:
    """Pick the feedback for this poll: 'narrate' | 'thinking' | 'none'.

    Tool narration is the strongest sign of life and always wins; a one-time
    thinking ack fills the gap only when nothing has been said yet; otherwise
    stay quiet (no spam).
    """
    if has_new_tools:
        return "narrate"
    if not thinking_sent:
        return "thinking"
    return "none"


def ack_text() -> str:
    return THINKING_ACK


def _as_bool(s: str) -> bool:
    return str(s).strip() not in ("", "0", "false", "False", "no")


def main(argv: list[str]) -> int:
    cmd = argv[0] if argv else "env"
    if cmd == "env":
        # Shell-evalable; bot.sh does: eval "$(python3 -m core.responsiveness env)"
        print(f"JV_POLL_FIRST={POLL_FIRST_S}")
        print(f"JV_POLL_STEADY={POLL_STEADY_S}")
        print(f"JV_PROMOTE_AFTER={PROMOTE_AFTER_S}")
        print(f"JV_THINKING_ACK={shlex.quote(THINKING_ACK)}")
        return 0
    if cmd == "decide":
        has_tools = _as_bool(argv[1]) if len(argv) > 1 else False
        sent = _as_bool(argv[2]) if len(argv) > 2 else False
        print(decide_action(has_tools, sent))
        return 0
    if cmd == "ack-text":
        print(ack_text())
        return 0
    if cmd == "poll":
        idx = int(argv[1]) if len(argv) > 1 and argv[1].lstrip("-").isdigit() else 0
        print(poll_interval(idx))
        return 0
    print(f"unknown command: {cmd} (env|decide|ack-text|poll)", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
