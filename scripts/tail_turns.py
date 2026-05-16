#!/usr/bin/env python3
"""
Recent turns buffer for Jarvis session handoff.

Reads the tail N user/assistant turns from the current session jsonl.
If the current session has fewer than MIN_TURNS (just rotated), backfills
from the previous session (counter-1) to cover the handoff gap.

Output: markdown-formatted "Recent Turns" block for sys_prompt injection.

Usage:
    python3 tail_turns.py <session_id> <counter> <conv_key> <limit>
"""
import json
import sys
import uuid
from pathlib import Path

SESSION_DIR = Path.home() / ".claude/projects/-Users-pascal-Desktop-jarvis"
NAMESPACE = uuid.UUID("a1b2c3d4-e5f6-7890-abcd-ef1234567890")

MIN_TURNS_FOR_STANDALONE = 5   # below this, backfill from previous session
MAX_MSG_CHARS = 400            # per-message truncation
MAX_TOTAL_CHARS = 6000         # ~2k tokens total budget


def read_tail_turns(sid: str, limit: int) -> list[tuple[str, str, str]]:
    """Read last `limit` user/assistant text turns from a session file.
    Returns list of (role, text, timestamp) tuples in chronological order.
    """
    path = SESSION_DIR / f"{sid}.jsonl"
    if not path.exists():
        return []

    turns = []
    for line in path.open(encoding="utf-8", errors="ignore"):
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if obj.get("type") not in ("user", "assistant"):
            continue
        msg = obj.get("message", {})
        role = msg.get("role", obj.get("type", ""))
        content = msg.get("content", "")
        text = ""
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            parts = []
            for c in content:
                if isinstance(c, dict) and c.get("type") == "text":
                    parts.append(c.get("text", ""))
            text = "\n".join(parts)
        text = text.strip()
        if not text:
            continue
        ts = obj.get("timestamp", "")[:16]
        turns.append((role, text, ts))
    return turns[-limit:]


def main() -> None:
    if len(sys.argv) < 5:
        print("Usage: tail_turns.py <session_id> <counter> <conv_key> <limit>",
              file=sys.stderr)
        sys.exit(1)

    sid = sys.argv[1]
    try:
        counter = int(sys.argv[2])
    except ValueError:
        counter = 0
    conv_key = sys.argv[3]
    try:
        limit = int(sys.argv[4])
    except ValueError:
        limit = 20

    turns = read_tail_turns(sid, limit)

    # If current session is too young (just rotated), backfill from previous session
    if len(turns) < MIN_TURNS_FOR_STANDALONE and counter > 1:
        prev_sid = str(uuid.uuid5(NAMESPACE, f"{conv_key}-{counter-1}"))
        prev_turns = read_tail_turns(prev_sid, limit - len(turns))
        turns = prev_turns + turns

    if not turns:
        return

    # Format with character budget (newest turns prioritized by building from the tail)
    formatted = []
    total = 0
    for role, text, ts in reversed(turns):
        snippet = text[:MAX_MSG_CHARS]
        if len(text) > MAX_MSG_CHARS:
            snippet += "…"
        line = f"[{ts}] **{role}**: {snippet}"
        if total + len(line) > MAX_TOTAL_CHARS:
            break
        formatted.append(line)
        total += len(line)
    formatted.reverse()

    print("## Recent Turns (原文缓冲)")
    print()
    print("这是你和 Pascal 最近的原文对话（已截断），如果看起来像被截断或跨 session 的，优先相信这份原文。")
    print()
    for line in formatted:
        print(line)
        print()


if __name__ == "__main__":
    main()
