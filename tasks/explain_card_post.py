#!/usr/bin/env python3
"""Post-hook: deliver the plain-language retelling and settle the request.

Stdin: the model's retelling. Printing it is delivery — heartbeat routes the
task's output to the user like any other user-facing task. The queue entry is
only dropped AFTER we have output to show; a model that came back empty
leaves the claim to be retaken (EXPLAIN_RETAKE_S), so a tap can never be
silently swallowed — an unanswered 「看不懂」 is a dead end.
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import memorial
from core.safety import looks_like_error, strip_task_framing


def main() -> int:
    text = strip_task_framing(sys.stdin.read().strip())
    if not text or "HEARTBEAT_OK" in text or looks_like_error(text):
        return 0
    m = re.search(r"\[explain (mem_[a-z0-9_]+)\]", text)
    mid = m.group(1) if m else ""
    if mid:
        text = text.replace(m.group(0), "").strip()
        memorial.explain_complete(mid)
    else:
        # No id echoed — still deliver, settle the oldest claimed request so
        # the queue cannot wedge on a model that dropped the marker.
        from core.jsonl import read_jsonl
        claimed = [r for r in read_jsonl(memorial._explain_queue_path())
                   if int(r.get("taken_at") or 0)]
        if claimed:
            memorial.explain_complete(str(claimed[0].get("memorial_id")))
    print(f"🤔 你说看不懂的那张，大白话重讲：\n\n{text}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
