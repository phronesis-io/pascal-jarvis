"""Lifelog — diet logging + exercise aggregation + morning-anchor state
(REQ-114 饮食记录结构化 / REQ-115 晨间锚点 / REQ-116 运动周汇总).

Pure data layer, no advice: append-only JSONL logs under the gitignored
``data/`` directory plus 7-day aggregations over them.

Multi-user principle (feedback-personal-data-is-config): anything
person-specific — the morning anchor items, the weekly exercise goal — lives
in gitignored ``data/*_personal.txt`` files with neutral defaults in code.
Exercise keywords are generic activity words, so they stay a module-level
list for now.

Files (all under data/, covered by the root ``/data/`` gitignore rule):
    diet_log.jsonl              {ts, meal, items, source, note}
    exercise_log.jsonl          {ts, activity, source, note}
    morning_anchor_state.json   {"date": "YYYY-MM-DD", "ts": ...} — daily dedup
    exercise_week_state.json    {"week": "YYYY-Wnn", "ts": ...} — weekly dedup
    morning_anchor_personal.txt one anchor item per line (optional)
    exercise_goal_personal.txt  weekly target, e.g. "2-3" or "3" (optional)

CLI (ad-hoc use from sessions):
    python3 -m core.lifelog diet-add --meal 午 --items "牛肉面,青菜"
    python3 -m core.lifelog diet-week
    python3 -m core.lifelog exercise-add --activity 游泳
    python3 -m core.lifelog exercise-week [--harvest]
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

from core.jsonl import read_jsonl
from core.safety import atomic_write

JARVIS_DIR = Path(os.environ.get("JARVIS_DIR",
                                 Path(__file__).resolve().parent.parent))

# Exercise-flavoured calendar keywords. Generic activity words (NOT a
# per-user config for now — see module docstring). Latin words are matched
# case-insensitively.
EXERCISE_KEYWORDS = ["游泳", "篮球", "康复", "复动", "PT", "Stretch", "健身", "拉伸"]

MEALS = ("早", "午", "晚", "加餐")

# Normalize the common ways a meal is named to the canonical 4 buckets.
_MEAL_ALIASES = {
    "早": "早", "早餐": "早", "早饭": "早", "breakfast": "早",
    "午": "午", "午餐": "午", "午饭": "午", "中饭": "午", "中餐": "午", "lunch": "午",
    "晚": "晚", "晚餐": "晚", "晚饭": "晚", "dinner": "晚",
    "加餐": "加餐", "夜宵": "加餐", "宵夜": "加餐", "下午茶": "加餐", "零食": "加餐",
    "snack": "加餐",
}

# Idiom / non-food captures that must never become a diet item ("吃了亏",
# "吃了一惊", "吃了药"…). Conservative: an unmatchable mention is dropped,
# never guessed.
_NON_FOOD_ITEMS = {"亏", "苦", "惊", "一惊", "一堑", "药", "一顿", "顿", "苦头",
                   "东西", "点东西", "些东西", "饭", "点", "很多", "不少"}

_TS_FMT = "%Y-%m-%d %H:%M"


# ── paths (functions, so tests can monkeypatch JARVIS_DIR) ───────────────


def _data_dir() -> Path:
    return JARVIS_DIR / "data"


def diet_log_path() -> Path:
    return _data_dir() / "diet_log.jsonl"


def exercise_log_path() -> Path:
    return _data_dir() / "exercise_log.jsonl"


def anchor_state_path() -> Path:
    return _data_dir() / "morning_anchor_state.json"


def anchor_personal_path() -> Path:
    return _data_dir() / "morning_anchor_personal.txt"


def exercise_goal_path() -> Path:
    return _data_dir() / "exercise_goal_personal.txt"


def week_state_path() -> Path:
    return _data_dir() / "exercise_week_state.json"


def calendar_today_path() -> Path:
    memory_dir = Path(os.environ.get("MEMORY_DIR",
                                     Path.home() / ".jarvis" / "memory"))
    return memory_dir / "hot" / "calendar_today.md"


# ── low-level io ─────────────────────────────────────────────────────────


def _append_line(path: Path, entry: dict) -> None:
    """O_APPEND one compact JSON line (same idiom as core.memorial — atomic
    for small writes, safe across heartbeat / CLI / bot writers)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _read_state(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError, ValueError):
        return {}


def _now(now: datetime | None = None) -> datetime:
    return now or datetime.now()


def _parse_ts(ts: str) -> datetime | None:
    ts = str(ts or "").strip()
    for fmt in (_TS_FMT, "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(ts[:len(datetime.now().strftime(fmt))], fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


def _recent(entries: list[dict], days: int, now: datetime) -> list[dict]:
    """Entries whose ts date falls in the last `days` days (inclusive of today)."""
    cutoff = (now - timedelta(days=days - 1)).date()
    out = []
    for e in entries:
        dt = _parse_ts(e.get("ts", ""))
        if dt is not None and cutoff <= dt.date() <= now.date():
            out.append(e)
    return out


def _window_dates(days: int, now: datetime) -> list[str]:
    return [(now - timedelta(days=i)).strftime("%Y-%m-%d")
            for i in range(days - 1, -1, -1)]


# ── diet log (REQ-114) ───────────────────────────────────────────────────


def normalize_meal(raw: str, when: datetime | None = None) -> str:
    """Map a meal name to 早/午/晚/加餐; unknown → infer from time of day."""
    meal = _MEAL_ALIASES.get(str(raw or "").strip().lower().strip("：: "))
    if not meal:
        meal = _MEAL_ALIASES.get(str(raw or "").strip())
    if meal:
        return meal
    hour = _now(when).hour
    if hour < 11:
        return "早"
    if hour < 15:
        return "午"
    if hour < 21:
        return "晚"
    return "加餐"


def diet_append(entry: dict) -> dict:
    """Normalize + append one diet entry to data/diet_log.jsonl.

    Entry: {ts, meal (早/午/晚/加餐), items: [str], source: 'checkin'|'chat',
    note}. Missing ts → now; meal is normalized (falling back to a
    time-of-day guess only for the bucket, never for the items).
    """
    now = datetime.now()
    ts = str(entry.get("ts") or "").strip() or now.strftime(_TS_FMT)
    when = _parse_ts(ts) or now
    items = entry.get("items") or []
    if isinstance(items, str):
        items = [items]
    items = [str(i).strip() for i in items if str(i).strip()]
    source = str(entry.get("source") or "chat").strip() or "chat"
    normalized = {
        "ts": ts,
        "meal": normalize_meal(entry.get("meal", ""), when),
        "items": items,
        "source": source,
        "note": str(entry.get("note") or "").strip(),
    }
    _append_line(diet_log_path(), normalized)
    return normalized


def diet_week_summary(now: datetime | None = None, days: int = 7) -> dict:
    """Aggregate the last `days` days of diet_log. Pure data, no advice."""
    now = _now(now)
    entries = _recent(read_jsonl(diet_log_path()), days, now)
    by_meal: Counter = Counter(e.get("meal", "") for e in entries)
    items: Counter = Counter(i for e in entries for i in e.get("items", []))
    logged_dates = {(_parse_ts(e.get("ts", "")) or now).strftime("%Y-%m-%d")
                    for e in entries}
    window = _window_dates(days, now)
    return {
        "days": days,
        "since": window[0],
        "meals_logged": len(entries),
        "by_meal": {m: by_meal[m] for m in MEALS if by_meal[m]},
        "common_items": [{"item": i, "count": c}
                         for i, c in items.most_common(8)],
        "days_with_log": sorted(d for d in window if d in logged_dates),
        "gaps": [d for d in window if d not in logged_dates],
    }


# ── diet parsing (conservative — never hallucinate items) ───────────────

# Structured contract line a task's LLM output may end with:
#     DIET: 午|牛肉面、青菜[|备注]
_DIET_LINE_RE = re.compile(r"^\s*DIET\s*[:：]\s*(.+?)\s*$", re.I)
_ITEM_SPLIT_RE = re.compile(r"[、,，;；/＋+]|\s+和\s+|(?<=[一-鿿])和(?=[一-鿿])")

# NOTE: no comma/period in the free-text capture class — in prose a comma
# ends the food list ("吃了牛肉面和青菜，下午继续干活"). Lists use 、/和/+.
_ITEM_CHARS = r"[一-鿿A-Za-z0-9、和+ ]"
_MEAL_WORD = r"(早餐|早饭|午餐|午饭|中饭|中餐|晚餐|晚饭|夜宵|宵夜|加餐|下午茶)"
# "午饭吃了牛肉面" / "早餐是三明治和咖啡" / "晚饭点了外卖披萨"
_MEAL_MENTION_RE = re.compile(
    _MEAL_WORD + r"\s*(?:吃了|喝了|吃的是|吃的|是|点了)\s*(" + _ITEM_CHARS + r"{1,30})")
# Bare "吃了X/喝了X" (meal bucket inferred from timestamp)
_ATE_RE = re.compile(r"(?:吃了|喝了)\s*(" + _ITEM_CHARS + r"{1,30})")


def _clean_items(raw: str) -> list[str]:
    items = []
    for part in _ITEM_SPLIT_RE.split(raw or ""):
        part = str(part or "").strip()
        part = re.sub(r"^(点|些|一点|一些|一份|一个|个)", "", part).strip()
        part = re.sub(r"(外卖)$", "", part).strip()
        if not part or len(part) > 15 or part in _NON_FOOD_ITEMS:
            continue
        if part not in items:
            items.append(part)
    return items


def parse_diet_line(line: str) -> dict | None:
    """Parse one ``DIET: 餐次|食物1、食物2[|备注]`` contract line → entry dict."""
    m = _DIET_LINE_RE.match(str(line or ""))
    if not m:
        return None
    parts = [p.strip() for p in re.split(r"[|｜]", m.group(1))]
    if not parts or not parts[0]:
        return None
    meal_raw = parts[0]
    if meal_raw not in _MEAL_ALIASES:
        return None  # malformed meal → drop the whole line, never guess
    items = _clean_items(parts[1]) if len(parts) > 1 else []
    if not items:
        return None  # a diet entry without concrete items is noise
    return {
        "meal": _MEAL_ALIASES[meal_raw],
        "items": items,
        "note": parts[2] if len(parts) > 2 else "",
    }


def split_diet_line(text: str) -> tuple[str, dict | None]:
    """Split a trailing DIET: contract line off a message.

    Returns (message_without_diet_lines, entry_or_None). Only trailing lines
    count (mirrors memorial's OPTIONS: convention) — a "DIET:" in the middle
    of prose is prose. The line is stripped even when malformed, so a broken
    contract line never leaks onto a user-facing card.
    """
    lines = str(text or "").splitlines()
    entry = None
    while lines:
        tail = lines[-1].strip()
        if not tail:
            lines.pop()
            continue
        if not _DIET_LINE_RE.match(tail):
            break
        parsed = parse_diet_line(tail)
        if parsed and entry is None:
            entry = parsed
        lines.pop()
    return "\n".join(lines).rstrip(), entry


def parse_diet_mentions(text: str, when: datetime | None = None) -> list[dict]:
    """Conservative keyword parse of free chat text → diet entries.

    Only fires on explicit past-tense food mentions (吃了/喝了/早餐是…) with
    an extractable item; idioms ("吃了亏") and vague mentions ("吃了点东西")
    are dropped. Never guesses items.
    """
    text = str(text or "")
    entries: list[dict] = []
    seen_spans: list[tuple[int, int]] = []
    for m in _MEAL_MENTION_RE.finditer(text):
        items = _clean_items(m.group(2))
        if items:
            entries.append({"meal": _MEAL_ALIASES[m.group(1)], "items": items,
                            "note": ""})
            seen_spans.append(m.span())
    for m in _ATE_RE.finditer(text):
        if any(s <= m.start() < e for s, e in seen_spans):
            continue  # already captured with its meal word
        items = _clean_items(m.group(1))
        if items:
            entries.append({"meal": normalize_meal("", when), "items": items,
                            "note": ""})
    return entries


# ── exercise log + calendar aggregation (REQ-116) ────────────────────────


def exercise_append(entry: dict) -> dict:
    """Normalize + append one exercise entry {ts, activity, source, note}."""
    activity = str(entry.get("activity") or "").strip()
    if not activity:
        raise ValueError("exercise entry needs an activity")
    normalized = {
        "ts": str(entry.get("ts") or "").strip() or datetime.now().strftime(_TS_FMT),
        "activity": activity[:30],
        "source": str(entry.get("source") or "chat").strip() or "chat",
        "note": str(entry.get("note") or "").strip(),
    }
    _append_line(exercise_log_path(), normalized)
    return normalized


_CAL_HEADER_RE = re.compile(r"\((\d{4}-\d{2}-\d{2})")
_CAL_COMPACT_RE = re.compile(
    r"^\s+(\d{2}/\d{2})\s+\S+\s+(\d{1,2}:\d{2})-\d{1,2}:\d{2}\s+(.+)")
_CAL_DETAIL_RE = re.compile(r"^\s*-?\s*(\d{1,2}:\d{2})-\d{1,2}:\d{2}\s+(.+)")


def _clean_title(raw: str) -> str:
    """Same normalization calendar_sync_post applies: drop @location and
    parenthetical description, truncate."""
    t = re.sub(r"\s*@.+", "", str(raw or ""))
    t = re.sub(r"\s*[（(].*", "", t)
    return t.strip()[:30]


def _compact_date(mm_dd: str, now: datetime) -> str:
    """MM/DD → YYYY-MM-DD, picking the year that lands nearest to now."""
    month, day = int(mm_dd[:2]), int(mm_dd[3:5])
    best = None
    for year in (now.year - 1, now.year, now.year + 1):
        try:
            candidate = datetime(year, month, day)
        except ValueError:
            continue
        if best is None or abs((candidate - now).days) < abs((best - now).days):
            best = candidate
    return best.strftime("%Y-%m-%d") if best else ""


def parse_calendar_events(text: str, now: datetime | None = None) -> list[dict]:
    """Parse hot/calendar_today.md into [{date, start, title}].

    Handles both shapes calendar_sync_post writes: dated section headers
    ("Today (2026-07-21 Monday):" / "Day 3 (…)") followed by detail lines
    ("  14:00-15:00  Title"), and compact upcoming lines
    ("  07/23 Wed  14:00-15:00  Title").
    """
    now = _now(now)
    events: list[dict] = []
    current_date = ""
    for line in str(text or "").splitlines():
        hm = _CAL_HEADER_RE.search(line)
        if hm:
            current_date = hm.group(1)
            continue
        cm = _CAL_COMPACT_RE.match(line)
        if cm:
            date = _compact_date(cm.group(1), now)
            if date:
                events.append({"date": date, "start": cm.group(2),
                               "title": _clean_title(cm.group(3))})
            continue
        if current_date:
            dm = _CAL_DETAIL_RE.match(line)
            if dm:
                events.append({"date": current_date, "start": dm.group(1),
                               "title": _clean_title(dm.group(2))})
    return events


def _exercise_excludes() -> list[str]:
    """Per-user standing-block exclusions (gitignored, one keyword per line).

    A recurring daily calendar block (e.g. a standing morning-rehab slot) is
    a SCHEDULE, not evidence the session happened — counting it makes the
    weekly headline number trivially met and dishonest (red-team 7/21
    finding 9). Blocks listed here are skipped by the calendar harvest;
    they're tracked by their own mechanisms (e.g. morning-anchor)."""
    try:
        path = _data_dir() / "exercise_exclude_personal.txt"
        return [l.strip() for l in path.read_text(encoding="utf-8").splitlines()
                if l.strip() and not l.strip().startswith("#")]
    except OSError:
        return []


def _matches_exercise(title: str) -> bool:
    low = str(title or "").lower()
    if any(ex.lower() in low for ex in _exercise_excludes()):
        return False
    return any(kw.lower() in low for kw in EXERCISE_KEYWORDS)


def calendar_exercise_events(now: datetime | None = None,
                             text: str | None = None) -> list[dict]:
    """Exercise-keyword events from the calendar cache (missing file → [])."""
    if text is None:
        try:
            text = calendar_today_path().read_text(encoding="utf-8")
        except OSError:
            return []
    return [e for e in parse_calendar_events(text, now)
            if _matches_exercise(e["title"])]


def harvest_calendar_exercise(now: datetime | None = None,
                              text: str | None = None) -> list[dict]:
    """Append calendar exercise events (last 7 days up to today) to
    exercise_log.jsonl with source='calendar', deduped on (date, activity,
    start) against existing rows. Future events are never harvested — the
    cache is a schedule, not a record.

    Run daily (morning-anchor pre) so past days accumulate: the calendar
    cache only ever shows today + upcoming, so each day must be captured on
    the day itself.
    """
    now = _now(now)
    existing = set()
    for e in read_jsonl(exercise_log_path()):
        dt = _parse_ts(e.get("ts", ""))
        if dt is not None:
            existing.add((dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M"),
                          e.get("activity", "")))
    added = []
    cutoff = (now - timedelta(days=6)).date()
    for ev in calendar_exercise_events(now, text):
        dt = _parse_ts(f"{ev['date']} {ev['start']}")
        if dt is None or not (cutoff <= dt.date() <= now.date()):
            continue
        key = (ev["date"], ev["start"], ev["title"][:30])
        if key in existing:
            continue
        existing.add(key)
        added.append(exercise_append({"ts": f"{ev['date']} {ev['start']}",
                                      "activity": ev["title"],
                                      "source": "calendar"}))
    return added


def exercise_goal() -> str:
    """Weekly session target from per-user config; neutral default '2-3'."""
    try:
        for line in exercise_goal_path().read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                return line
    except OSError:
        pass
    return "2-3"


def goal_range(goal: str | None = None) -> tuple[int, int]:
    goal = goal if goal is not None else exercise_goal()
    m = re.match(r"^\s*(\d+)\s*[-~－到]\s*(\d+)\s*$", goal)
    if m:
        lo, hi = int(m.group(1)), int(m.group(2))
        return (min(lo, hi), max(lo, hi))
    m = re.match(r"^\s*(\d+)\s*$", goal)
    if m:
        n = int(m.group(1))
        return (n, n)
    return (2, 3)


def exercise_week_summary(now: datetime | None = None, days: int = 7,
                          harvest: bool = False) -> dict:
    """Aggregate the last `days` days of exercise from ①the calendar cache
    (harvested rows, source='calendar') and ②manual/chat entries. Pure data."""
    now = _now(now)
    if harvest:
        harvest_calendar_exercise(now)
    entries = _recent(read_jsonl(exercise_log_path()), days, now)
    by_activity: Counter = Counter(e.get("activity", "") for e in entries)
    by_source: Counter = Counter(e.get("source", "") for e in entries)
    active_dates = sorted({(_parse_ts(e.get("ts", "")) or now).strftime("%Y-%m-%d")
                           for e in entries})
    goal = exercise_goal()
    lo, hi = goal_range(goal)
    return {
        "days": days,
        "since": _window_dates(days, now)[0],
        "sessions": len(entries),
        "goal": goal,
        "goal_min": lo,
        "goal_max": hi,
        "goal_met": len(entries) >= lo,
        "by_activity": dict(by_activity.most_common()),
        "by_source": dict(by_source),
        "days_active": active_dates,
    }


# ── morning anchor state (REQ-115) ───────────────────────────────────────

# Neutral defaults (multi-user product): person-specific anchor items belong
# in the gitignored data/morning_anchor_personal.txt, one per line.
DEFAULT_ANCHOR_ITEMS = [
    "晨间脑力热身：一道小谜题（10 分钟内那种）",
    "晨间身体激活：一组简短的拉伸/热身动作",
]


def morning_anchor_items() -> list[str]:
    try:
        lines = [l.strip() for l in
                 anchor_personal_path().read_text(encoding="utf-8").splitlines()]
        items = [l for l in lines if l and not l.startswith("#")]
        if items:
            return items
    except OSError:
        pass
    return list(DEFAULT_ANCHOR_ITEMS)


def morning_anchor_fired(now: datetime | None = None) -> bool:
    """True if today's anchor nudge already went out (state stamp match)."""
    return _read_state(anchor_state_path()).get("date") == \
        _now(now).strftime("%Y-%m-%d")


def morning_anchor_mark(now: datetime | None = None) -> None:
    now = _now(now)
    atomic_write(anchor_state_path(),
                 json.dumps({"date": now.strftime("%Y-%m-%d"),
                             "ts": now.strftime(_TS_FMT)},
                            ensure_ascii=False) + "\n")


# ── weekly exercise card state (REQ-116) ─────────────────────────────────


def _iso_week(now: datetime) -> str:
    year, week, _ = now.isocalendar()
    return f"{year}-W{week:02d}"


def exercise_card_sent_this_week(now: datetime | None = None) -> bool:
    return _read_state(week_state_path()).get("week") == _iso_week(_now(now))


def exercise_card_mark(now: datetime | None = None) -> None:
    now = _now(now)
    atomic_write(week_state_path(),
                 json.dumps({"week": _iso_week(now),
                             "ts": now.strftime(_TS_FMT)},
                            ensure_ascii=False) + "\n")


# ── CLI ──────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(
        prog="core.lifelog",
        description="饮食/运动记录与 7 天聚合（REQ-114/115/116）")
    sub = parser.add_subparsers(dest="cmd")

    da = sub.add_parser("diet-add", help="append one diet entry")
    da.add_argument("--meal", default="", help="早/午/晚/加餐 (或早餐/午饭等)")
    da.add_argument("--items", required=True, help="食物，逗号/、分隔")
    da.add_argument("--note", default="")
    da.add_argument("--source", default="chat", choices=["chat", "checkin"])
    da.add_argument("--ts", default="")

    dw = sub.add_parser("diet-week", help="last-7-days diet summary (JSON)")
    dw.add_argument("--days", type=int, default=7)

    ea = sub.add_parser("exercise-add", help="append one exercise entry")
    ea.add_argument("--activity", required=True)
    ea.add_argument("--note", default="")
    ea.add_argument("--source", default="chat")
    ea.add_argument("--ts", default="")

    ew = sub.add_parser("exercise-week",
                        help="last-7-days exercise summary (JSON)")
    ew.add_argument("--days", type=int, default=7)
    ew.add_argument("--harvest", action="store_true",
                    help="harvest calendar exercise events into the log first")

    sub.add_parser("anchor-status",
                   help="morning anchor today: prints 'sent' or 'due'")
    sub.add_parser("anchor-items", help="print anchor items (one per line)")
    sub.add_parser("week-card-status",
                   help="weekly exercise card this ISO week: 'sent' or 'due'")

    args = parser.parse_args(argv)

    if args.cmd == "diet-add":
        entry = diet_append({"ts": args.ts, "meal": args.meal,
                             "items": _clean_items(args.items) or
                                      [p.strip() for p in
                                       re.split(r"[、,，]", args.items)
                                       if p.strip()],
                             "source": args.source, "note": args.note})
        print(json.dumps(entry, ensure_ascii=False))
        return 0
    if args.cmd == "diet-week":
        print(json.dumps(diet_week_summary(days=args.days), ensure_ascii=False,
                         indent=2))
        return 0
    if args.cmd == "exercise-add":
        entry = exercise_append({"ts": args.ts, "activity": args.activity,
                                 "source": args.source, "note": args.note})
        print(json.dumps(entry, ensure_ascii=False))
        return 0
    if args.cmd == "exercise-week":
        print(json.dumps(exercise_week_summary(days=args.days,
                                               harvest=args.harvest),
                         ensure_ascii=False, indent=2))
        return 0
    if args.cmd == "anchor-status":
        print("sent" if morning_anchor_fired() else "due")
        return 0
    if args.cmd == "anchor-items":
        for item in morning_anchor_items():
            print(item)
        return 0
    if args.cmd == "week-card-status":
        print("sent" if exercise_card_sent_this_week() else "due")
        return 0

    parser.print_usage(sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
