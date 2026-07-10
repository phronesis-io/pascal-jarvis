"""System-local time helper, robust to subprocess TZ env variations
and to the system timezone changing while a process is running.

Background: `datetime.now()` returns a naive datetime whose interpretation
depends on the `TZ` environment variable. A subprocess started by launchd,
cron, or an IDE may inherit `TZ=UTC` (or no TZ at all) → timestamps in log
files are written in UTC instead of local time, which looks like a bug.

This module resolves the system's IANA timezone by reading the
`/etc/localtime` symlink directly (bypassing env vars entirely), then uses
`zoneinfo` for conversions. Falls back gracefully if detection fails.

The detected timezone is cached with a short TTL — NOT once at import.
A long-running daemon must follow the OS when the user travels across
timezones and macOS re-points /etc/localtime (2026-07-10 incident: a
heartbeat started under Atlantic/Reykjavik kept writing Reykjavik
timestamps for hours after the Mac had switched back to Asia/Shanghai,
skewing quiet hours, batching windows and every injected 'Current time').
Re-resolving the symlink costs microseconds; the TTL bounds worst-case
staleness to about a minute.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path

# How long a detected timezone is trusted before /etc/localtime is
# re-checked. Short enough that a long-running daemon follows a system TZ
# switch within a minute; long enough that hot loops don't resolve the
# symlink on every single call.
_TZ_CACHE_TTL_SECONDS = 60.0


def _detect_system_tz_name() -> str | None:
    """Return an IANA timezone name (e.g. 'Asia/Shanghai') read from
    /etc/localtime, which is set by the OS and independent of env vars."""
    try:
        link = Path("/etc/localtime").resolve()
        parts = link.parts
        # Possible paths:
        #   macOS: /var/db/timezone/zoneinfo/Asia/Shanghai
        #   Linux: /usr/share/zoneinfo/Asia/Shanghai
        for i, p in enumerate(parts):
            if p == "zoneinfo" and i + 1 < len(parts):
                return "/".join(parts[i + 1:])
    except Exception:
        pass
    return None


def _build_local_tzinfo(tz_name: str | None):
    """Return a tzinfo object for the given IANA name, or None."""
    if tz_name:
        try:
            from zoneinfo import ZoneInfo  # Python 3.9+
            return ZoneInfo(tz_name)
        except Exception:
            pass
    # Last resort: use whatever Python thinks is local (may be wrong under bad TZ env)
    return None


# Cached detection result. Seeded at import for cheap first use, then kept
# fresh by _refresh_local_tz(). Kept as module globals (not a private struct)
# so existing call-time imports of _LOCAL_TZ keep seeing the current value.
_TZ_NAME = _detect_system_tz_name()
_LOCAL_TZ = _build_local_tzinfo(_TZ_NAME)
_tz_checked_at = time.monotonic()


def _refresh_local_tz(force: bool = False) -> None:
    """Re-read /etc/localtime if the cache TTL expired (or force=True).

    Rebuilds the ZoneInfo only when the resolved name actually changed.
    On transient detection failure the last known good value is kept
    (better a slightly stale zone than falling back to naive datetimes
    that follow a possibly-polluted TZ env).
    """
    global _TZ_NAME, _LOCAL_TZ, _tz_checked_at
    now = time.monotonic()
    if not force and now - _tz_checked_at < _TZ_CACHE_TTL_SECONDS:
        return
    _tz_checked_at = now
    name = _detect_system_tz_name()
    if name is None or name == _TZ_NAME:
        return  # unreadable (keep last known good) or unchanged
    _LOCAL_TZ = _build_local_tzinfo(name)
    _TZ_NAME = name


def now_local() -> datetime:
    """Current local time as a timezone-aware datetime.

    Always returns system-local time, even if the process was started with
    TZ=UTC or no TZ at all, and follows the OS (within the cache TTL) if
    the system timezone changes mid-process.
    """
    _refresh_local_tz()
    if _LOCAL_TZ is not None:
        return datetime.now(_LOCAL_TZ)
    # Fallback: naive datetime in process-local interpretation
    return datetime.now()


def now_local_str(fmt: str = "%Y-%m-%d %H:%M") -> str:
    """Format current local time. Default format: 'YYYY-MM-DD HH:MM'."""
    return now_local().strftime(fmt)


_ZH_WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def _zh_weekday(dt: datetime) -> str:
    return _ZH_WEEKDAYS[dt.weekday()]


def msg_timestamp_prefix() -> str:
    """Inline timestamp to prepend to an incoming user message body.

    Why: the authoritative 'Current time' is injected once at session start
    and into the system prompt, but the user's message body itself carries no
    time. In a long, all-day conversation (mixed with heartbeat cards and
    quoted old cards) Claude can anchor on a stale in-conversation timestamp.
    Prefixing each incoming message with the *real* current time fixes that.

    Home (system TZ == Asia/Shanghai): single clean line
        '[2026-06-03 10:12 周三]'
    Abroad (system TZ != Shanghai, e.g. Mac auto-switched): dual display
        '[当地 06-03 09:00 周三 / 上海 22:00]'
    Shanghai is always shown when abroad so nothing drifts vs the calendar.
    """
    local = now_local()  # also refreshes the cached timezone
    # Single-line at home, or whenever TZ detection failed (safe default).
    if _TZ_NAME == "Asia/Shanghai" or _LOCAL_TZ is None:
        return f"[{local.strftime('%Y-%m-%d %H:%M')} {_zh_weekday(local)}]"
    try:
        from zoneinfo import ZoneInfo
        sh = local.astimezone(ZoneInfo("Asia/Shanghai"))
        return (f"[当地 {local.strftime('%m-%d %H:%M')} {_zh_weekday(local)}"
                f" / 上海 {sh.strftime('%H:%M')}]")
    except Exception:
        return f"[{local.strftime('%Y-%m-%d %H:%M')} {_zh_weekday(local)}]"


def system_tz_name() -> str | None:
    """Expose the detected IANA timezone name (for diagnostics)."""
    _refresh_local_tz()
    return _TZ_NAME
