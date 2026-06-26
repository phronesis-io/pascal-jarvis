#!/usr/bin/env python3
"""Time-anchor guard for proactive narration.

Root cause this closes (2026-06-22): a cross-session narration line shipped to
Lark claiming "今早 6:35 你在 repos session 敲 ls 时撞到月度消费上限". The signal
(spend limit being hit) was real, but the *when/where* — a concrete HH:MM plus a
concrete action — was fabricated to make the alert read as grounded. No log line
at 6:35 existed. Same failure family as the fake basketball score and the fake
"56 公式": a true signal wrapped in an unverified concrete shell.

The rule encoded here: any proactive narration that names a concrete clock time
(HH:MM) describing a *past* event must correspond to a real log line near that
time. Anchors that cannot be grounded are reported, and the caller suppresses the
user-facing surface rather than ping Pascal with a fabricated specific.

This is deliberately narrow — it verifies clock-time anchors against the actual
log tail, nothing more. It does not try to validate arbitrary prose claims.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from core.timeutil import now_local

# Bound the log scan — only the tail can be recent.
_LOG_TAIL_BYTES = 512 * 1024
# A log line within this many minutes of the claimed time grounds the anchor.
_DEFAULT_TOLERANCE_MIN = 20

# HH:MM in narration prose. Guard against matching inside longer digit runs
# (version strings, ids) by forbidding an adjacent digit on either side.
_HHMM_RE = re.compile(r"(?<!\d)([01]?\d|2[0-3]):([0-5]\d)(?!\d)")
# Any log timestamp shape across the live files:
#   jarvis.log ISO  : 2026-06-22T14:31:02
#   bracket form    : [2026-06-22 14:44:46]
_LOG_TS_RE = re.compile(r"(\d{4}-\d{2}-\d{2})[ T](\d{2}):(\d{2})")
# "昨天/昨晚/昨日" near an anchor → resolve to yesterday rather than today.
_YESTERDAY_RE = re.compile(r"昨[天日晚]")
# Afternoon/evening qualifiers that bump a <12 hour into 24h reckoning.
_PM_RE = re.compile(r"(下午|晚上|傍晚|今晚|昨晚)")


@dataclass(frozen=True)
class TimeAnchor:
    """A clock-time mention pulled from narration, with where it points."""
    raw: str            # the literal "6:35" as written
    minute_of_day: int  # 0..1439, after any PM adjustment
    date: str           # "YYYY-MM-DD" the anchor is taken to describe


def _default_log_paths(root: Path | None = None) -> list[Path]:
    base = root or Path.cwd()
    return [base / "jarvis.log", base / "daemon.log"]


def _read_tail(path: Path, max_bytes: int = _LOG_TAIL_BYTES) -> str:
    """Read the trailing window of a log file. Never raises."""
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
                f.readline()  # drop the partial leading line
            return f.read().decode("utf-8", errors="ignore")
    except OSError:
        return ""


def _log_minutes(log_paths: list[Path]) -> set[tuple[str, int]]:
    """Set of (date, minute-of-day) present in the recent log tail."""
    found: set[tuple[str, int]] = set()
    for p in log_paths:
        text = _read_tail(p)
        if not text:
            continue
        for m in _LOG_TS_RE.finditer(text):
            date, hh, mm = m.group(1), int(m.group(2)), int(m.group(3))
            found.add((date, hh * 60 + mm))
    return found


def extract_anchors(text: str, now=None) -> list[TimeAnchor]:
    """Pull HH:MM clock anchors out of narration, resolving the date they point at.

    Date resolution is intentionally simple: yesterday if a 昨* marker sits near
    the anchor, otherwise today. Hours < 12 are bumped to PM when a 下午/晚上 style
    qualifier precedes them.
    """
    now = now or now_local()
    today = now.strftime("%Y-%m-%d")
    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    anchors: list[TimeAnchor] = []
    for m in _HHMM_RE.finditer(text):
        hh, mm = int(m.group(1)), int(m.group(2))
        # Look at a small window before the anchor for day / PM qualifiers.
        ctx = text[max(0, m.start() - 8): m.start()]
        if hh < 12 and _PM_RE.search(ctx):
            hh += 12
        date = yesterday if _YESTERDAY_RE.search(ctx) else today
        anchors.append(TimeAnchor(raw=m.group(0), minute_of_day=hh * 60 + mm, date=date))
    return anchors


def unverified_anchors(
    text: str,
    log_paths: list[Path] | None = None,
    now=None,
    tolerance_min: int = _DEFAULT_TOLERANCE_MIN,
) -> list[TimeAnchor]:
    """Return the clock anchors in `text` that no nearby log line supports.

    An empty list means every concrete time in the text is grounded (or there
    were no concrete times at all) — safe to surface.
    """
    anchors = extract_anchors(text, now=now)
    if not anchors:
        return []
    log_paths = log_paths if log_paths is not None else _default_log_paths()
    minutes = _log_minutes(log_paths)
    # If the log tail is empty/unreadable we cannot prove anything either way.
    # Fail open (do not block) so a logging outage never silences real nudges.
    if not minutes:
        return []
    bad: list[TimeAnchor] = []
    for a in anchors:
        grounded = any(
            d == a.date and abs(mod - a.minute_of_day) <= tolerance_min
            for (d, mod) in minutes
        )
        if not grounded:
            bad.append(a)
    return bad


def is_groundable(text: str, **kwargs) -> bool:
    """True if the narration carries no unverifiable concrete clock time."""
    return not unverified_anchors(text, **kwargs)
