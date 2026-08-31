#!/usr/bin/env python3
"""Capturethe owner's reply to a daily-reflect card into his 《Jarvis 日志》.

This closes the two-way loop of the PRD's 每日复盘 check-in: daily-reflect
appends Jarvis's reflection + question; when the owner quote-replies to that card,
THIS captures HIS words ("我怎么看一些事") under the same day.

Invoked fire-and-forget from bot.sh's reply path (backgrounded, so it never
delays Jarvis's reply). Fully guarded — any failure is silent.

Env:
  JV_PARENT     the quoted message_id (the card being replied to); empty for
                direct (non-quote) messages
  JV_REPLY      the owner's raw reply text
  JARVIS_DIR    repo root (for engagement_log.jsonl + imports)
  JV_CHAT_TYPE  chat type of the incoming message (p2p/group), optional
  JV_MSG_TYPE   message type (text/image/...), optional
  JV_SENDER     sender open_id, optional
  JV_USER_ID    the owner's configured open_id, optional
  JV_JOURNAL_SHADOW_WINDOW_H  attribution window in hours (default 4)

It only journals when the quoted card's source is `daily-reflect` — so ordinary
chat and other cards are never written to the journal.

REQ-86 SHADOW extension (log-only): a direct (non-quote) p2p text from the owner
within N hours of the daily-reflect card SHOULD also count as a check-in
answer. Before enabling that write, this script only LOGS the attribution
decision — (ts, message head, reason, would_capture) — to
data/journal_capture_shadow.jsonl so the accuracy can be reviewed. The
direct-message path NEVER writes the journal.
"""

import json
import os
import sys
import time

JARVIS_DIR = os.environ.get("JARVIS_DIR", "")
if JARVIS_DIR:
    sys.path.insert(0, JARVIS_DIR)

# Only replies to these card sources are journaled.
JOURNAL_SOURCES = {"daily-reflect"}

# REQ-86 shadow: direct replies within this window of the daily-reflect card
# are attribution candidates.
SHADOW_WINDOW_HOURS = 4
SHADOW_FILE = "journal_capture_shadow.jsonl"  # under $JARVIS_DIR/data/


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


def _latest_reflect_sent_epoch():
    """Epoch of the most recent daily-reflect 'sent' row, or None."""
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
                if row.get("source") not in JOURNAL_SOURCES:
                    continue
                epoch = row.get("epoch")
                if isinstance(epoch, (int, float)):
                    return float(epoch)
                # Older rows may lack epoch; fall back to the local-ts string.
                try:
                    return time.mktime(
                        time.strptime(row.get("ts", ""), "%Y-%m-%d %H:%M"))
                except (ValueError, OverflowError):
                    return None
    except OSError:
        return None
    return None


def _shadow_log_direct(reply: str) -> None:
    """REQ-86 shadow (LOG-ONLY): decide whether a direct (non-quote) message
    would be attributed to today's daily-reflect check-in, and append the
    decision to data/journal_capture_shadow.jsonl. Never writes the journal,
    never sends anything. Fully guarded."""
    chat_type = (os.environ.get("JV_CHAT_TYPE") or "").strip()
    msg_type = (os.environ.get("JV_MSG_TYPE") or "").strip()
    sender = (os.environ.get("JV_SENDER") or "").strip()
    user_id = (os.environ.get("JV_USER_ID") or "").strip()

    # Not attribution candidates at all — skip logging entirely:
    # group chats, non-text payloads, and messages not from the owner.
    if chat_type and chat_type != "p2p":
        return
    if msg_type and msg_type != "text":
        return
    if user_id and sender and sender != user_id:
        return

    try:
        window_h = float(os.environ.get("JV_JOURNAL_SHADOW_WINDOW_H", "")
                         or SHADOW_WINDOW_HOURS)
    except ValueError:
        window_h = SHADOW_WINDOW_HOURS

    card_epoch = _latest_reflect_sent_epoch()
    if card_epoch is None:
        would, age_min, reason = False, None, "no daily-reflect sent record"
    else:
        age_min = int((time.time() - card_epoch) / 60)
        if 0 <= age_min <= window_h * 60:
            would = True
            reason = (f"direct p2p reply {age_min}min after daily-reflect "
                      f"card (window {window_h:g}h)")
        else:
            would = False
            reason = (f"last daily-reflect card is {age_min}min old — "
                      f"outside {window_h:g}h window")

    try:
        from core.timeutil import now_local_str
        ts = now_local_str("%Y-%m-%d %H:%M")
    except Exception:
        ts = time.strftime("%Y-%m-%d %H:%M")
    row = {
        "ts": ts,
        "msg": reply[:60],
        "reason": reason,
        "would_capture": would,
        "card_age_min": age_min,
    }
    out_dir = os.path.join(JARVIS_DIR, "data")
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, SHADOW_FILE), "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def main() -> None:
    parent = (os.environ.get("JV_PARENT") or "").strip()
    if parent == "null":
        parent = ""
    reply = (os.environ.get("JV_REPLY") or "").strip()
    if not reply or not JARVIS_DIR:
        return

    if not parent:
        # Direct (non-quote) message: REQ-86 shadow, log-only. No journal write.
        try:
            _shadow_log_direct(reply)
        except Exception:
            pass
        return

    # Quote-reply path (unchanged): journal only for daily-reflect cards.
    if not _parent_is_reflect(parent):
        return
    try:
        from core.journal import append_entry
        # heading="" → nest under today's reflection asthe owner's own voice.
        append_entry(f"> 🗣 **你**：{reply}", heading="")
    except Exception:
        return


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
