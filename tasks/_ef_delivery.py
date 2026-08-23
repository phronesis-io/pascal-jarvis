#!/usr/bin/env python3
"""Legacy EigenFlux quiet-hours backlog compatibility helpers.

Pascal asked (2026-06-07, ~00:08): stop pinging me with EigenFlux feed / 知会
at night. Hold them, and give me ONE consolidated card in the morning at a
good time — not a one-by-one drip at midnight, and not a giant dump either.
Only genuinely urgent items break through at night.

New delivery is centralized in ``core.heartbeat_loop`` and preserves one
intact memorial card per event.  This module remains only to drain backlog
files created by older versions; no active post-hook calls ``hold`` anymore.

Knobs (env, all optional):
  JARVIS_TZ              timezone name           (default Asia/Shanghai)
  JARVIS_QUIET_START / JARVIS_QUIET_END  the one product-wide quiet window
                         (core.attention_policy, default 23:30-09:30); this
                         module no longer keeps a window of its own
  JARVIS_EF_FLUSH_HOUR   earliest morning flush  (default 9)
  JARVIS_EF_QUIET_OVERRIDE  "quiet"|"awake"      force state (tests only)
"""
import json
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from zoneinfo import ZoneInfo
    _TZ = ZoneInfo(os.environ.get("JARVIS_TZ", "Asia/Shanghai"))
except Exception:  # pragma: no cover - fallback if tzdata missing
    _TZ = None


def _int_env(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, default))
    except (TypeError, ValueError):
        return default


def _jarvis_dir() -> Path:
    return Path(os.environ.get("JARVIS_DIR", Path(__file__).resolve().parent.parent))


def backlog_path() -> Path:
    p = _jarvis_dir() / "eigenflux" / "feed_backlog.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _flush_stamp() -> Path:
    return _jarvis_dir() / "eigenflux" / ".feed_backlog_flushed"


def local_now() -> datetime:
    return datetime.now(_TZ) if _TZ else datetime.now()


def in_quiet_hours(now: datetime | None = None) -> bool:
    """True when EigenFlux pushes should be held rather than sent."""
    override = os.environ.get("JARVIS_EF_QUIET_OVERRIDE")
    if override == "quiet":
        return True
    if override == "awake":
        return False
    # One quiet window for every surface: until 2026-08-23 this module kept
    # a private 22:00-09:00 window that drifted from the 23:30-09:30 the
    # delivery pipeline and feed-triage actually honour.
    from core.attention_policy import in_quiet_hours as _canonical
    moment = now or local_now()
    return _canonical(moment.hour * 60 + moment.minute)


def hold(message: str, source: str, now: datetime | None = None) -> None:
    """Append a held EigenFlux message to the overnight backlog."""
    message = (message or "").strip()
    if not message:
        return
    entry = {
        "ts": (now or local_now()).isoformat(),
        "source": source,
        "message": message,
    }
    with open(backlog_path(), "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _backlog_nonempty() -> bool:
    p = backlog_path()
    return p.exists() and p.stat().st_size > 0


def drain() -> list[dict]:
    """Read and clear the backlog. Returns held entries in arrival order."""
    p = backlog_path()
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except (json.JSONDecodeError, ValueError):
            continue
    p.unlink(missing_ok=True)
    return out


def should_flush(now: datetime | None = None) -> bool:
    """Fire once per day: first awake cycle at/after the flush hour with a
    non-empty backlog. A date stamp guarantees one flush, not every cycle."""
    now = now or local_now()
    if in_quiet_hours(now):
        return False
    if now.hour < _int_env("JARVIS_EF_FLUSH_HOUR", 9):
        return False
    if not _backlog_nonempty():
        return False
    stamp = _flush_stamp()
    today = now.strftime("%Y-%m-%d")
    if stamp.exists() and stamp.read_text(encoding="utf-8").strip() == today:
        return False
    return True


def mark_flushed(now: datetime | None = None) -> None:
    _flush_stamp().write_text((now or local_now()).strftime("%Y-%m-%d"),
                              encoding="utf-8")


# Header / footer chrome to strip when condensing held cards into one digest.
_DROP_PREFIXES = ("📡 Powered", "📡 知会", "🎯 行动", "📡 EigenFlux", "Powered by EigenFlux")


def render_digest_body(held: list[dict], cap_lines: int = 8) -> tuple[str, int]:
    """Condense held messages into one tight digest body.

    Strips per-card chrome (section headers, the Powered-by footer), dedups by
    line prefix, keeps arrival order, caps to cap_lines. Returns (body, n_total).
    """
    lines: list[str] = []
    seen: set[str] = set()
    for h in held:
        for raw in (h.get("message") or "").splitlines():
            s = raw.strip()
            if not s or any(s.startswith(p) for p in _DROP_PREFIXES):
                continue
            key = s[:40]
            if key in seen:
                continue
            seen.add(key)
            lines.append(s)
    n_total = len(lines)
    shown = lines[:cap_lines]
    parts = ["你睡的时候攒下这些，挑出来给你："]
    parts.extend(shown)
    if n_total > cap_lines:
        parts.append(f"\n_另有 {n_total - cap_lines} 条已存档，想看全部跟我说一声_")
    parts.append("\n📡 Powered by EigenFlux")
    return "\n".join(parts), n_total
