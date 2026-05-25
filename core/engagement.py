"""Engagement tracking — measures if users respond to heartbeat messages.

Classifies user responses by gap from last heartbeat send:
  - engaged: <10 min
  - late_reply: 10-30 min
  - ignored: >30 min (within 1h window)

Used by bot.sh message listener to record engagement per source.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path


def record_response(log_path: str | Path, content_head: str = "") -> bool:
    """Record a user response in the engagement log.

    Reads the last 'sent' entry, computes gap, classifies reaction, appends.
    Returns True if a response was recorded, False if skipped.
    """
    log_path = Path(log_path)
    if not log_path.exists():
        return False

    # Find last 'sent' entry
    last_sent = None
    for line in log_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
            if entry.get("type") == "sent":
                last_sent = entry
        except (json.JSONDecodeError, TypeError):
            continue

    if not last_sent:
        return False

    sent_epoch = last_sent.get("epoch", 0)
    now_epoch = int(time.time())
    gap_seconds = now_epoch - sent_epoch

    # Only track if sent within last 60 minutes
    if gap_seconds > 3600:
        return False

    # Classify
    if gap_seconds <= 600:
        reaction = "engaged"
    elif gap_seconds <= 1800:
        reaction = "late_reply"
    else:
        reaction = "ignored"

    entry = {
        "ts": time.strftime("%Y-%m-%d %H:%M"),
        "source": last_sent.get("source", "unknown"),
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
