"""Engagement tracking — measures if users respond to heartbeat messages.

Classifies user responses by gap from last heartbeat send:
  - engaged: <10 min
  - late_reply: 10-30 min
  - ignored: >30 min (within 1h window)

Used by bot.sh message listener to record engagement per source.
"""

from __future__ import annotations

import fcntl
import json
import os
import sys
import time
from pathlib import Path


def record_response(log_path: str | Path, content_head: str = "") -> bool:
    """Record a user response in the engagement log.

    Reads the last 'sent' entry, computes gap, classifies reaction, appends.
    Returns True if a response was recorded, False if skipped.

    The whole read-scan-append runs under an exclusive flock (red-team fix):
    bot.sh spawns this backgrounded once per inbound message with no `wait`,
    so two rapid replies used to race — both read responded_already=False
    before either appended and BOTH credited the source (the 107% over-count
    this very function exists to kill). The lock also serializes against
    _trim_file's in-place truncate on the same file.
    """
    log_path = Path(log_path)
    if not log_path.exists():
        return False

    lock_fh = open(log_path, "a")
    try:
        fcntl.flock(lock_fh, fcntl.LOCK_EX)
        return _record_response_locked(log_path, content_head)
    finally:
        fcntl.flock(lock_fh, fcntl.LOCK_UN)
        lock_fh.close()


def _record_response_locked(log_path: Path, content_head: str) -> bool:
    # Find the last 'sent' entry AND whether a 'response' was already recorded
    # AFTER it (REQ-63). The old code credited EVERY reply within 60min to the
    # last sent source with no cap — so a multi-message conversation after one
    # proactive card billed every message to that source (calendar-sync showed
    # 27 sent / 29 responses = 107%, with '凯瑞老师'/背痛 misattributed). Now:
    # only the FIRST reply credits the source; later messages are 'conversation'.
    last_sent = None
    responded_already = False
    for line in log_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        t = entry.get("type")
        if t == "sent":
            last_sent = entry
            responded_already = False  # a new send resets the "first reply" gate
        elif t == "response" and last_sent is not None:
            responded_already = True

    if not last_sent:
        return False

    sent_epoch = last_sent.get("epoch", 0)
    now_epoch = int(time.time())
    gap_seconds = now_epoch - sent_epoch

    # Only track if sent within last 60 minutes
    if gap_seconds > 3600:
        return False

    # Classify timing
    if gap_seconds <= 600:
        reaction = "engaged"
    elif gap_seconds <= 1800:
        reaction = "late_reply"
    else:
        reaction = "ignored"

    is_quote_reply = content_head.strip().startswith("[Replying to:")
    if responded_already and not is_quote_reply:
        # The proactive card was already answered; this is a follow-on
        # conversation message. Log it for volume but DON'T re-credit the
        # source (that's the 107% bug). A quote-reply is exempt — it's an
        # explicit, attributable reply to a specific card.
        source = "conversation"
        reaction = "conversation"
    else:
        source = last_sent.get("source", "unknown")

    entry = {
        "ts": time.strftime("%Y-%m-%d %H:%M"),
        "source": source,
        "type": "response",
        "reaction": reaction,
        "gap_seconds": gap_seconds,
        "content_head": content_head[:80] if content_head else "",
    }
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return True


if __name__ == "__main__":
    # CLI: python3 -m core.engagement "message content"
    jarvis_dir = os.environ.get("JARVIS_DIR", ".")
    log_file = Path(jarvis_dir) / "engagement_log.jsonl"
    content = sys.argv[1] if len(sys.argv) > 1 else ""
    record_response(log_file, content)
