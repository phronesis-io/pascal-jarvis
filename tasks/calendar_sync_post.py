#!/usr/bin/env python3
"""Post-hook: write calendar snapshot to hot/calendar_today.md.

Design principle: SILENT by default.
- Always update the memory file (keeps context fresh for main conversation).
- NEVER send the full schedule dump to the user as a card.
- Only notify the user when there's a STRUCTURAL change worth their attention:
  new events added, events cancelled, or time conflicts detected.
- When notifying, use ONE natural sentence — not a table.
- One card per change (一张卡一件事, REQ-117) while the batch is small
  (≤3 changes); a longer batch of churn ships as ONE card (2026-08-07).

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
from core.card_split import split_matters
from core.safety import is_idle_reply, looks_like_error
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
    t = re.sub(r"\s*@.+", "", raw)          # drop "@ 某某路…"
    t = re.sub(r"\s*[（(].*", "", t)         # drop "(description…"
    return t.strip()[:30]


def detect_changes(old_events: set[str], new_events: set[str]) -> tuple[set, set]:
    """Return (added, removed) event sets."""
    added = new_events - old_events
    removed = old_events - new_events
    return added, removed


def _date_label(mmdd: str) -> str:
    """'07/15' → '7/15(周三)' — weekday is best-effort."""
    if not mmdd:
        return ""
    try:
        from core.timeutil import now_local
        m, d = (int(x) for x in mmdd.split("/"))
        today = now_local().date()
        wd = "一二三四五六日"[_nearby_date(m, d, today).weekday()]
        return f"{m}/{d}(周{wd})"
    except (ValueError, IndexError):
        return mmdd


def _nearby_date(month: int, day: int, today):
    """Resolve MM/DD to the nearest valid date around today.

    Calendar snapshots span both sides of New Year and stale snapshots may
    retain last month's events. A simple ``month < current month`` rule turns
    every elapsed July event into July next year when run in August.
    """
    from datetime import date

    candidates = []
    for year in (today.year - 1, today.year, today.year + 1):
        try:
            candidates.append(date(year, month, day))
        except ValueError:
            continue
    if not candidates:
        raise ValueError("invalid month/day")
    return min(candidates, key=lambda value: abs((value - today).days))


def _is_past(mmdd: str) -> bool:
    """True when 'MM/DD' resolves to a date strictly before today.

    An event that merely slid out of the rolling 30-day window is not a
    cancellation — it already happened. Before this guard, a resync after a
    quiet stretch reported every elapsed event as 取消 (2026-08-19: two cards
    at once, '取消：8/17 羽毛球' and '取消：8/18 白皮书 + Vic 讨论', for
    thingsthe owner had already done — "也不叫取消吧，做完了？"). A same-day
    removal IS a real cancellation and is deliberately kept.
    """
    if not mmdd:
        return False
    try:
        from core.timeutil import now_local
        m, d = (int(x) for x in mmdd.split("/"))
        today = now_local().date()
        return _nearby_date(m, d, today) < today
    except (ValueError, IndexError):
        return False


def format_change_lines(added: set, removed: set, cap: int = 5) -> list[str]:
    """Human-complete card body for a calendar change (the owner's 2026-07-14
    complaint — the old card said just "新增: 15:00 X" with no date):
    every line carries the DATE and weekday, a same-title add+remove pair is
    presented as ONE 改期 line (old→new) instead of a confusing add/cancel
    pair, and overflow beyond the display cap is counted, never silently
    dropped."""
    def _split(e: str) -> tuple[str, str, str]:
        parts = e.split("|")
        if len(parts) >= 3:
            return parts[0], parts[1], parts[2]
        return "", "", parts[-1]

    added_ev = [t for t in (_split(e) for e in sorted(added)) if t[2].strip()]
    removed_ev = [t for t in (_split(e) for e in sorted(removed)) if t[2].strip()]
    if not added_ev and not removed_ev:
        return []

    moved = []
    removed_by_title = {t[2]: t for t in removed_ev}
    still_added = []
    for a in added_ev:
        old = removed_by_title.pop(a[2], None)
        if old is not None:
            moved.append((old, a))
        else:
            still_added.append(a)
    still_removed = [t for t in removed_ev if t[2] in removed_by_title]

    # Drop anything dated before today (see _is_past). A reschedule whose new
    # slot is still ahead stays, even if its old slot has already elapsed.
    moved = [(o, n) for o, n in moved if not _is_past(n[0])]
    still_added = [t for t in still_added if not _is_past(t[0])]
    still_removed = [t for t in still_removed if not _is_past(t[0])]
    if not moved and not still_added and not still_removed:
        return []

    lines = []
    for old, new in moved[:cap]:
        when_old = f"{_date_label(old[0])} {old[1]}".strip()
        when_new = f"{_date_label(new[0])} {new[1]}".strip()
        lines.append(f"改期：{new[2]} — {when_old} → {when_new}")
    for d, t, title in still_added[:cap]:
        lines.append(f"新增：{_date_label(d)} {t} {title}".replace("  ", " "))
    for d, t, title in still_removed[:cap]:
        lines.append(f"取消：{_date_label(d)} {t} {title}".replace("  ", " "))
    overflow = (max(0, len(moved) - cap) + max(0, len(still_added) - cap)
                + max(0, len(still_removed) - cap))
    if overflow:
        lines.append(f"…另有 {overflow} 项变动（详见日历）")
    return lines


def _within_days(mmdd: str, days: int) -> bool:
    """True when 'MM/DD' resolves to today .. today+days (inclusive)."""
    if not mmdd:
        return False
    try:
        from core.timeutil import now_local
        m, d = (int(x) for x in mmdd.split("/"))
        today = now_local().date()
        delta = (_nearby_date(m, d, today) - today).days
        return 0 <= delta <= days
    except (ValueError, IndexError):
        return False


_CHANGE_DATE_RE = re.compile(r"(\d{1,2}/\d{1,2})\(")


def change_attention(body: str, near_days: int = 2) -> str:
    """The attention class one calendar-change card deserves.

    A 取消 or 改期 that lands within the next two days can cost the owner a
    trip or a slot — that is the only calendar change worth an alert. A 新增
    is almost always his own hand on the calendar echoed back (18 alert cards
    in 14 days by 2026-08-31, every one 「新增」); it is a notice that waits
    for the morning digest.
    """
    for line in str(body or "").splitlines():
        head = line.strip()
        if not head.startswith(("取消：", "改期：")):
            continue
        dates = _CHANGE_DATE_RE.findall(head)
        target = dates[-1] if dates else ""
        if target and _within_days(target, near_days):
            return "alert"
    return "notice"


def change_card_bodies(lines: list[str]) -> list[str]:
    """Card bodies for a batch of calendar changes.

    Delegates to core.card_split so this hook and the memorial backstop apply
    ONE rule: 2–3 changes are 一张卡一件事 (the owner's 7/10 decree, REQ-117 —
    on 7/21 he explicitly wanted three cards for three shifted meetings); a
    longer list stays on one card (2026-08-07, after 9 日程变动 cards in a day
    drew 0 taps). The …另有 N 项 overflow counter is never a matter of its
    own — it rides on the last card instead of buzzing a context-free stub.
    """
    body = "\n".join(lines).strip()
    if not body:
        return []
    return split_matters(body)


def main() -> int:
    raw = sys.stdin.read().strip()
    if is_idle_reply(raw):
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

    lines = format_change_lines(added, removed)
    if not lines:
        print(f"[calendar-sync] Cosmetic change only (title rewording), silent", file=sys.stderr)
        return 0

    # One card per change (一张卡一件事, REQ-117) — never merge several
    # events' changes into one card. Each build_card line is adopted into
    # its own memorial downstream (core.memorial.memorialize_output).
    bodies = change_card_bodies(lines)
    for body in bodies:
        print(build_card(
            "📅 日程变动", body, source="calendar-sync",
            work_receipt="读取日历快照、完成变更比对和重复事件过滤",
            attention=change_attention(body),
        ))
    print(f"[calendar-sync] Notified {len(bodies)} card(s): "
          f"{'; '.join(lines)!r}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
