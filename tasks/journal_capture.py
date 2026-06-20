#!/usr/bin/env python3
"""Capture Pascal's reply to a daily-reflect card into his 《Jarvis 日志》.

This closes the two-way loop of the PRD's 每日复盘 check-in: daily-reflect
appends Jarvis's reflection + question; when Pascal quote-replies to that card,
THIS captures HIS words ("我怎么看一些事") under the same day.

Invoked fire-and-forget from bot.sh's reply path (backgrounded, so it never
delays Jarvis's reply). Fully guarded — any failure is silent.

Env:
  JV_PARENT   the quoted message_id (the card being replied to)
  JV_REPLY    Pascal's raw reply text
  JARVIS_DIR  repo root (for engagement_log.jsonl + imports)

It only journals when the quoted card's source is `daily-reflect` — so ordinary
chat and other cards are never written to the journal.
"""

import json
import os
import sys

JARVIS_DIR = os.environ.get("JARVIS_DIR", "")
if JARVIS_DIR:
    sys.path.insert(0, JARVIS_DIR)

# Only replies to these card sources are journaled.
JOURNAL_SOURCES = {"daily-reflect"}


def _parent_is_reflect(parent_id: str) -> bool:
    """True iff parent_id belongs to a sent card whose source is journaled."""
    path = os.path.join(JARVIS_DIR, "engagement_log.jsonl")
    try:
        with open(path, encoding="utf-8") as f:
            for line in reversed(f.read().splitlines()):
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("type") != "sent":
                    continue
                if parent_id in (row.get("message_ids") or []):
                    return row.get("source") in JOURNAL_SOURCES
    except OSError:
        return False
    return False


def main() -> None:
    parent = (os.environ.get("JV_PARENT") or "").strip()
    reply = (os.environ.get("JV_REPLY") or "").strip()
    if not parent or not reply or not JARVIS_DIR:
        return
    if not _parent_is_reflect(parent):
        return
    try:
        from core.journal import append_entry
        # heading="" → nest under today's reflection as Pascal's own voice.
        append_entry(f"> 🗣 **你**：{reply}", heading="")
    except Exception:
        return


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
