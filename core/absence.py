"""Absence reporting — telling the owner when Jarvis was not there at all.

2026-08-19 audit. The host slept for 39 of 39 hours and the guardian daemon
detected it 38 separate times, correctly, to the second. Every one of those
observations ended in ``post-wake grace, NOT restarting`` or ``would alert but
in post-wake grace``. The grace is right about restarts and right about
component alerts — a task whose ``last_success`` is stale by exactly the
length of the nap is an artefact, not a fault — and it is silent about the one
thing that was true: the system had been gone all day, two intents expired
unfired, and the owner learned it by asking for an audit two days later.

Sleeping is not a fault and must never page. A closed lid on battery is the
owner's own choice, macOS sleeps the machine regardless of any ``caffeinate``
assertion, and "we slept 23:40-08:20" every single morning would be pure
noise. What earns exactly one card is absence during the hours Jarvis is
expected to be working — the same non-quiet window every other surface uses
(``core.attention_policy``). An overnight lid-close overlaps it by ~0 and
stays silent; a day like 08-18 overlaps it by 13 hours and gets one card.

The card is a receipt, not an alarm: nothing here is actionable beyond
plugging in and opening the lid, so it says so and stops.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

STATE_FILE = "data/.absence_state.json"

# Absence overlapping the owner's active hours before it is worth one card.
REPORT_ACTIVE_SECONDS = 3 * 3600
# Continuously awake for this long ⇒ the episode is over and the host is
# really back. Deliberately longer than a macOS DarkWake maintenance window
# (2-20s observed on 08-19, ~2min at the outside): reporting inside one would
# both understate the absence and queue the card behind the next sleep.
AWAKE_CONFIRM_SECONDS = 300
# One episode is a run of sleeps separated by less than a confirmed wake, so
# 38 DarkWake-punctuated gaps stay one 39-hour absence instead of 38 cards.
SOURCE = "host-absence"
MISSED_NAMES_SHOWN = 3
# A month of absence is already absurd; past this the minute-walk that scores
# active hours is capped rather than left unbounded on a corrupt timestamp.
MAX_EPISODE_DAYS = 30


@dataclass(frozen=True)
class Report:
    start: float
    end: float
    slept_seconds: float
    active_seconds: float
    gaps: int


def _state_path(root: Path | str) -> Path:
    return Path(root) / STATE_FILE


def _load(root: Path | str) -> dict:
    try:
        data = json.loads(_state_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(root: Path | str, state: dict) -> None:
    path = _state_path(root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass


def _local(epoch: float) -> datetime:
    from core.timeutil import now_local

    return datetime.fromtimestamp(float(epoch), now_local().tzinfo)


def active_seconds(start_epoch: float, end_epoch: float) -> float:
    """Seconds of ``[start, end]`` that fall in the owner's non-quiet hours."""
    from core.attention_policy import in_quiet_hours

    start, end = float(start_epoch), float(end_epoch)
    if end <= start:
        return 0.0
    end = min(end, start + MAX_EPISODE_DAYS * 86400)
    cursor = _local(start).replace(second=0, microsecond=0)
    last = _local(end)
    total = 0.0
    while cursor <= last:
        if not in_quiet_hours(cursor.hour * 60 + cursor.minute):
            total += 60
        cursor += timedelta(minutes=1)
    return total


def observe(root: Path | str, gap_seconds: float,
            now: float | None = None) -> Report | None:
    """Fold one observation into the current absence episode.

    Called on every daemon tick with the host sleep detected since the last
    tick (0 when the host stayed up). Returns a ``Report`` exactly once per
    qualifying episode, at the moment the host has been confirmably awake
    again — never mid-DarkWake, when the card would only queue behind the
    next sleep.
    """
    moment = time.time() if now is None else float(now)
    gap = float(gap_seconds or 0)
    state = _load(root)
    start = float(state.get("start") or 0)
    end = float(state.get("end") or 0)

    if gap > 0:
        gap_start = moment - gap
        if start and gap_start - end <= AWAKE_CONFIRM_SECONDS:
            state["end"] = moment
            state["slept"] = float(state.get("slept") or 0) + gap
            state["gaps"] = int(state.get("gaps") or 0) + 1
        else:
            state = {"start": gap_start, "end": moment, "slept": gap,
                     "gaps": 1}
        _save(root, state)
        return None

    if not start or moment - end < AWAKE_CONFIRM_SECONDS:
        return None

    # Confirmed awake: the episode is closed either way, reported or not.
    _save(root, {})
    active = active_seconds(start, end)
    if active < REPORT_ACTIVE_SECONDS:
        return None
    # An episode cannot contain more sleep than it spans. Two independent
    # meters feed this (the daemon's own loop overshoot and the wall-vs-
    # monotonic drift) and a clock correction could in principle overlap them;
    # a card claiming 50 hours of absence inside a 40-hour window would be
    # exactly the kind of number nobody can trust twice.
    slept = min(float(state.get("slept") or 0), end - start)
    return Report(start=start, end=end,
                  slept_seconds=slept,
                  active_seconds=active,
                  gaps=int(state.get("gaps") or 1))


def _duration_zh(seconds: float) -> str:
    total = max(0, int(seconds))
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{days} 天 {hours} 小时" if hours else f"{days} 天"
    if hours:
        return f"{hours} 小时 {minutes} 分" if minutes else f"{hours} 小时"
    return f"{minutes} 分钟"


def missed(root: Path | str, start: float, end: float) -> tuple[list[str], int]:
    """(names of intents that expired unfired, count of skipped occurrences).

    Deterministic code, not a model summary: this is the one part of the card
    that says what the absence actually cost.
    """
    try:
        from core.sched_events import query

        rows = query(root,
                     since=_local(start).strftime("%Y-%m-%d %H:%M"),
                     until=_local(end).strftime("%Y-%m-%d %H:%M"))
    except Exception:
        return [], 0
    expired: list[str] = []
    skipped = 0
    for row in rows:
        event = str(row.get("event") or "")
        if event == "intent_expired":
            name = str(row.get("name") or "").strip()
            if name and name not in expired:
                expired.append(name)
        elif event == "intent_occurrence_skipped":
            skipped += 1
    return expired, skipped


def build_card(root: Path | str, report: Report) -> tuple[str, str]:
    """Return ``(title, body)`` for the absence receipt."""
    started, ended = _local(report.start), _local(report.end)
    span = (f"{started.month}/{started.day} {started:%H:%M}"
            f" → {ended.month}/{ended.day} {ended:%H:%M}")
    title = f"我离线了 {_duration_zh(report.slept_seconds)}"

    expired, skipped = missed(root, report.start, report.end)
    cost = []
    if expired:
        shown = "、".join(expired[:MISSED_NAMES_SHOWN])
        more = len(expired) - MISSED_NAMES_SHOWN
        cost.append(f"{len(expired)} 件事过期没提醒你（{shown}"
                    + (f"，另 {more} 件）" if more > 0 else "）"))
    if skipped:
        cost.append(f"{skipped} 次例行没跑")
    active = _duration_zh(report.active_seconds)
    if cost:
        second = f"白天有 {active}没人看着：" + "，".join(cost) + "。"
    else:
        second = f"白天有 {active}没人看着，期间没有到期的事。"

    body = (
        f"{span} 这台机器一直是睡的，我跟着停了——合盖用电池的时候就是这样，不是故障。\n"
        f"{second}\n"
        "要我全天在，只能插电+开着盖；不然知道就行，我已经接着跑了。"
    )
    return title, body


def emit(root: Path | str, report: Report) -> bool:
    """Deliver the receipt as one ordinary notice card."""
    from core.memorial import create

    title, body = build_card(root, report)
    _memorial_id, accepted = create(
        source=SOURCE,
        title=title,
        body=body,
        attention="notice",
        dedup_key=f"absence:{int(report.end)}",
    )
    return bool(accepted)
