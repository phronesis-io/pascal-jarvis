"""System-local time helper, robust to subprocess TZ env variations.

Background: `datetime.now()` returns a naive datetime whose interpretation
depends on the `TZ` environment variable. A subprocess started by launchd,
cron, or an IDE may inherit `TZ=UTC` (or no TZ at all) → timestamps in log
files are written in UTC instead of local time, which looks like a bug.

This module resolves the system's IANA timezone by reading the
`/etc/localtime` symlink directly (bypassing env vars entirely), then uses
`zoneinfo` for conversions. Falls back gracefully if detection fails.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path


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


_TZ_NAME = _detect_system_tz_name()


def _get_local_tzinfo():
    """Return a tzinfo object for the system's local timezone, or None."""
    if _TZ_NAME:
        try:
            from zoneinfo import ZoneInfo  # Python 3.9+
            return ZoneInfo(_TZ_NAME)
        except Exception:
            pass
    # Last resort: use whatever Python thinks is local (may be wrong under bad TZ env)
    return None


_LOCAL_TZ = _get_local_tzinfo()


def now_local() -> datetime:
    """Current local time as a timezone-aware datetime.

    Always returns system-local time, even if the process was started with
    TZ=UTC or no TZ at all.
    """
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
    local = now_local()
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
    return _TZ_NAME
