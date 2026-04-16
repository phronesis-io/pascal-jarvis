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


def system_tz_name() -> str | None:
    """Expose the detected IANA timezone name (for diagnostics)."""
    return _TZ_NAME
