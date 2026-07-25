"""Shared human-attention policy used by every delivery surface."""

from __future__ import annotations

import os
from datetime import datetime, timedelta


DEFAULT_QUIET_START = "23:30"
DEFAULT_QUIET_END = "09:30"

QUIET_START_MIN = 23 * 60 + 30
QUIET_END_MIN = 9 * 60 + 30


def _minutes(value: object, fallback: int) -> int:
    try:
        hour, minute = (int(part) for part in str(value).split(":", 1))
    except (TypeError, ValueError):
        return fallback
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        return fallback
    return hour * 60 + minute


def quiet_window_minutes() -> tuple[int, int]:
    """Return the effective call-time window, including runtime overrides."""
    return (
        _minutes(os.environ.get("JARVIS_QUIET_START"), QUIET_START_MIN),
        _minutes(os.environ.get("JARVIS_QUIET_END"), QUIET_END_MIN),
    )


def quiet_window_labels() -> tuple[str, str]:
    def label(value: int) -> str:
        return f"{value // 60:02d}:{value % 60:02d}"

    start, end = quiet_window_minutes()
    return label(start), label(end)


def in_quiet_hours(minutes_of_day: int) -> bool:
    """Apply the same overnight or daytime window on every surface."""
    start, end = quiet_window_minutes()
    minute = int(minutes_of_day) % (24 * 60)
    if start == end:
        return False
    if start < end:
        return start <= minute < end
    return minute >= start or minute < end


def next_awake(moment: datetime) -> datetime:
    """Return the next effective quiet-window end after ``moment``."""
    _start, end = quiet_window_minutes()
    wake = moment.replace(
        hour=end // 60,
        minute=end % 60,
        second=0,
        microsecond=0,
    )
    if wake <= moment:
        wake += timedelta(days=1)
    return wake
