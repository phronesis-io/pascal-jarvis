#!/usr/bin/env python3
"""Post-hook: send the morning anchor nudge (REQ-115) — ONE short line.

Stamps data/morning_anchor_state.json BEFORE emitting the card, so even if
two cycles raced through the pre-gate the second one dies here (at most once
per day, the stamp is the source of truth). No follow-up mechanism exists on
purpose: ignored = ignored (Pascal hates nagging).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.card import build_card
from core.lifelog import (morning_anchor_fired, morning_anchor_last_text,
                          morning_anchor_mark)
from core.safety import looks_like_error, parse_json_response, strip_task_framing

# One short line — hard-clip anything longer so a rambling model can't turn
# the anchor into a wall of text.
MAX_CHARS = 120


def _normalized(text: str) -> str:
    """Whitespace-insensitive comparison key for the anchor line."""
    return " ".join(str(text or "").split())


def main() -> int:
    raw = sys.stdin.read().strip()
    if not raw or "HEARTBEAT_OK" in raw:
        return 0
    if looks_like_error(raw):
        return 0

    message = raw
    parsed = parse_json_response(raw)
    if isinstance(parsed, dict):
        message = str(parsed.get("user_message") or parsed.get("response") or "").strip()
    message = strip_task_framing(message)
    if not message:
        return 0

    # ONE line, short.
    message = message.splitlines()[0].strip()[:MAX_CHARS]
    if not message:
        return 0

    # Dedup backstop: stamp first, then everything else — a crash after the
    # stamp loses one nudge (fine), any later stamping point would reopen
    # the double-send window the stamp exists to close. Yesterday's line is
    # captured before the stamp overwrites it (REQ-121 comparison below).
    if morning_anchor_fired():
        print("[morning-anchor] already sent today — dropping duplicate",
              file=sys.stderr)
        return 0
    previous_line = morning_anchor_last_text()
    morning_anchor_mark(text=message)

    # Deterministic footer, not part of the model's one-line contract:
    # ledger-only cards (ambient exhaust that never reaches Feishu, REQ-119)
    # get their one batched shot at being seen here (style contract
    # 攒批≥5条晨匣提一行).
    try:
        from core.presence import morning_digest_line
        digest = morning_digest_line()
    except Exception as exc:
        print(f"[morning-anchor] presence digest failed: {exc}",
              file=sys.stderr)
        digest = ""

    # REQ-121: a line substantively identical to yesterday's carries no new
    # information — skip the resend (the window is already stamped). The
    # digest footer overrides the skip: its counts/titles are fresh content,
    # and it has no other surface (dropping it would silence the ledger-only
    # bin).
    if not digest and _normalized(message) == _normalized(previous_line):
        print("[morning-anchor] same line as yesterday, no digest — "
              "skipping resend", file=sys.stderr)
        return 0

    if digest:
        message = f"{message}\n{digest}"

    print(build_card("🌅 晨间锚点", message, source="morning-anchor"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
