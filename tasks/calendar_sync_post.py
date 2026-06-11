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
    """Extract stable event identifiers as date|time|title.

    Normalises both the detailed (Day 0-6) and compact (Upcoming) sections to
    the same MM/DD|HH:MM|title key so that an event moving between sections as
    the rolling window advances does NOT produce a false cancel+add pair.
    """
    events = set()
    current_date_mmdd = ""  # "06/13" extracted from the section header

    for line in text.split("\n"):
        # Section headers: "Day 3 (2026-06-13 Saturday):" or "Today (2026-06-10 Wednesday):"
        date_m = re.search(r"\((\d{4})-(\d{2})-(\d{2})", line)
        if date_m:
            current_date_mmdd = f"{date_m.group(2)}/{date_m.group(3)}"
            continue

        # Compact upcoming line: "  06/13 Sat  14:00-15:00  Title ..."
        compact_m = re.match(r"\s+(\d{2}/\d{2})\s+\S+\s+(\d{1,2}:\d{2})-\d{1,2}:\d{2}\s+(.+)", line)
        if compact_m:
            mm_dd = compact_m.group(1)
            time = compact_m.group(2)
            title = _normalise_title(compact_m.group(3))
            events.add(f"{mm_dd}|{time}|{title}")
            continue

        # Detailed event line under a known date header: "  14:00-15:00  Title ..."
        if current_date_mmdd:
            detail_m = re.match(r"\s+(\d{1,2}:\d{2})-\d{1,2}:\d{2}\s+(.+)", line)
            if detail_m:
                time = detail_m.group(1)
                title = _normalise_title(detail_m.group(2))
                events.add(f"{current_date_mmdd}|{time}|{title}")

    return events


def _normalise_title(raw: str) -> str:
    """Strip location (@…) and parenthetical description, truncate to 30 chars."""
    t = re.sub(r"\s*@.+", "", raw)          # drop "@ 华山路…"
    t = re.sub(r"\s*[（(].*", "", t)         # drop "(description…"
    return t.strip()[:30]


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
            stored = json.loads(EVENTS_FILE.read_text())
            loaded = set(stored)
            # Migration guard: old format was "time|title" (2 parts).
            # New format is "MM/DD|time|title" (3 parts).
            # If old file uses the 2-part format, discard it to avoid a false
            # flood of "removed" + "added" on the first run after this fix.
            if loaded and any(e.count("|") < 2 for e in loaded):
                print("[calendar-sync] Old event format detected, resetting baseline", file=sys.stderr)
                loaded = set()
            old_events = loaded
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

    # Don't send notifications outside working hours (user is sleeping)
    from core.timeutil import now_local
    hour = now_local().hour
    if hour < 8 or hour >= 23:
        print(f"[calendar-sync] Change detected but outside working hours ({hour}:xx), silent", file=sys.stderr)
        return 0

    # Build a short natural-language notification (NOT the full schedule)
    def _event_label(e: str) -> str:
        """'06/13|14:00|复动肌骨 康复课' → '14:00 复动肌骨 康复课'"""
        parts = e.split("|")
        if len(parts) >= 3:
            return f"{parts[1]} {parts[2]}"
        return parts[-1]

    added_names = [_event_label(e) for e in list(added)[:3]]
    removed_names = [_event_label(e) for e in list(removed)[:3]]
    # Filter out empty names
    added_names = [n for n in added_names if n.strip()]
    removed_names = [n for n in removed_names if n.strip()]

    if not added_names and not removed_names:
        print(f"[calendar-sync] Cosmetic change only (title rewording), silent", file=sys.stderr)
        return 0

    parts = []
    if added_names:
        parts.append(f"新增: {', '.join(added_names)}")
    if removed_names:
        parts.append(f"取消: {', '.join(removed_names)}")

    if parts:
        msg = "日程变动 — " + "；".join(parts)
        print(build_card("📅 变动", msg, source="calendar-sync"))
        print(f"[calendar-sync] Notified: {msg}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
