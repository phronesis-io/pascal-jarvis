#!/usr/bin/env python3
"""Post-hook: write calendar snapshot to hot/calendar_today.md.

Design principle: SILENT by default.
- Always update the memory file (keeps context fresh for main conversation).
- NEVER send the full schedule dump to the user as a card.
- Only notify the user when there's a STRUCTURAL change worth their attention:
  new events added, events cancelled, or time conflicts detected.
- When notifying, use ONE natural sentence — not a table.

UX first principle: the user's calendar is always available in memory for
the main conversation to reference. The heartbeat's job is NOT to repeat
what the user already knows, but to flag what changed or needs action.
"""
import hashlib
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.card import build_card
from core.safety import looks_like_error
from core.timeutil import now_local_str

MEMORY_DIR = Path(os.environ.get("MEMORY_DIR",
    Path.home() / ".jarvis" / "memory"))
CALENDAR_FILE = MEMORY_DIR / "hot" / "calendar_today.md"
HASH_FILE = MEMORY_DIR / "system" / ".calendar_hash"
EVENTS_FILE = MEMORY_DIR / "system" / ".calendar_events.json"
RAW_CACHE = MEMORY_DIR / "system" / ".calendar_raw_output.txt"


def extract_events(text: str) -> set[str]:
    """Extract a set of event identifiers (date + time + title keywords).

    Used to detect structural changes (new/removed events) vs cosmetic changes
    (rewording, time-until countdown, etc.)
    """
    events = set()
    # Match lines with times: "HH:MM something" or "**HH:MM** something"
    for m in re.finditer(r"(\d{1,2}:\d{2})\s+(.+?)(?:\n|$)", text):
        time = m.group(1)
        title = re.sub(r"[（(].*?[）)]", "", m.group(2)).strip()[:30]
        events.add(f"{time}|{title}")
    # Also match "周X" day headers with events
    for m in re.finditer(r"(周[一二三四五六日])\s*.*?(\d{1,2}:\d{2})", text):
        events.add(f"{m.group(1)}|{m.group(2)}")
    return events


def detect_changes(old_events: set[str], new_events: set[str]) -> tuple[set, set]:
    """Return (added, removed) event sets."""
    added = new_events - old_events
    removed = old_events - new_events
    return added, removed


def main() -> int:
    raw = sys.stdin.read().strip()
    if not raw or "HEARTBEAT_OK" in raw:
        return 0
    if looks_like_error(raw):
        print("[calendar-sync] skipping — output looks like error", file=sys.stderr)
        return 0

    ts = now_local_str("%Y-%m-%d %H:%M")
    CALENDAR_FILE.parent.mkdir(parents=True, exist_ok=True)

    # Event ID mapping is saved directly by the pre-script to $JARVIS_DIR/calendar_event_mapping.json
    # (used by calendar_write.sh for write-back)

    # Use raw pre-script output (actual schedule data) for memory file,
    # NOT Claude's response which may be just a summary.
    # The pre-script saves its output to RAW_CACHE via tee.
    schedule_data = ""
    if RAW_CACHE.exists():
        schedule_data = RAW_CACHE.read_text().strip()

    # Fallback to Claude's output if raw cache missing (shouldn't happen)
    if not schedule_data:
        schedule_data = raw

    # Always update the memory file silently with FULL schedule data (atomic — read by main session)
    from core.safety import atomic_write
    atomic_write(CALENDAR_FILE,
        f"---\nname: 今日日程\ndescription: Lark 日历自动同步，含今天和明天的日程\n"
        f"type: reference\n---\n\n# Calendar (synced {ts})\n\n{schedule_data}\n"
    )
    print(f"[calendar-sync] Updated memory silently at {ts}", file=sys.stderr)

    # Detect structural changes (new/removed events)
    EVENTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    new_events = extract_events(schedule_data)
    old_events: set[str] = set()
    if EVENTS_FILE.exists():
        try:
            old_events = set(json.loads(EVENTS_FILE.read_text()))
        except (json.JSONDecodeError, TypeError):
            pass

    # Save current events for next comparison (atomic)
    atomic_write(EVENTS_FILE, json.dumps(list(new_events), ensure_ascii=False))

    added, removed = detect_changes(old_events, new_events)

    # Only notify user if there are meaningful structural changes
    if not added and not removed:
        print(f"[calendar-sync] No structural change, silent", file=sys.stderr)
        return 0

    # First sync ever — don't spam the full schedule
    if not old_events and new_events:
        print(f"[calendar-sync] Initial sync, silent (no old baseline)", file=sys.stderr)
        return 0

    # Build a short natural-language notification (NOT the full schedule)
    parts = []
    if added:
        # Summarize added events briefly
        added_names = [e.split("|", 1)[1] if "|" in e else e for e in list(added)[:3]]
        parts.append(f"新增: {', '.join(added_names)}")
    if removed:
        removed_names = [e.split("|", 1)[1] if "|" in e else e for e in list(removed)[:3]]
        parts.append(f"取消: {', '.join(removed_names)}")

    if parts:
        msg = "日程变动 — " + "；".join(parts)
        print(build_card("📅 变动", msg))
        print(f"[calendar-sync] Notified: {msg}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
