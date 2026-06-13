"""Telemetry readers for dashboard pages — incremental, cheap, shared.

Two primitives (REQ-44 data layer):

- read_jsonl_tail(path): incremental JSONL reader with a module-level
  (offset, events) cache. On each call only the bytes appended since the
  last call are read and parsed; if the file shrank (rotation) the whole
  file is re-read. A ui.timer(15) refresh therefore costs O(new bytes),
  not O(file size) — sched_events.jsonl is several hundred KB and grows
  every few seconds.

- read_json(path, ttl): JSON file read with a small TTL cache, for state
  files (heartbeat_state.json) polled by multiple pages on short timers.

Convenience: read_sched_events(jarvis_dir) merges the rotated `.1`
generation (if any) with the live file, both via the tail cache.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

# 单文件解析结果上限——防止缓存无限生长（sched_events 轮转在 10MB，
# 远低于这个条数；超限只裁老条目，语义上等价于窗口截断）。
MAX_CACHED_EVENTS = 50_000

# path-str → {"offset": int, "events": list[dict]}
_tail_cache: dict[str, dict] = {}

# path-str → {"mtime_check": float, "data": object}
_json_cache: dict[str, dict] = {}


def reset_cache() -> None:
    """Drop all caches (tests / forced reload)."""
    _tail_cache.clear()
    _json_cache.clear()


def read_jsonl_tail(path: str | Path) -> list[dict]:
    """Parsed events of a JSONL file, reading only appended bytes per call.

    Rotation handling: if the file size is SMALLER than the cached offset the
    file was rotated/truncated — re-read from byte 0. A trailing partial line
    (writer mid-append) is left unconsumed: the offset only advances past the
    last complete newline, so the line is picked up whole on the next call.
    Returns the same cached list object contents (copied) — never raises.
    """
    p = Path(path)
    key = str(p)
    try:
        size = p.stat().st_size
    except OSError:
        _tail_cache.pop(key, None)
        return []

    entry = _tail_cache.get(key)
    if entry is None or size < entry["offset"]:
        entry = {"offset": 0, "events": []}  # fresh / rotated → full re-read
        _tail_cache[key] = entry

    if size > entry["offset"]:
        try:
            with open(p, "rb") as f:
                f.seek(entry["offset"])
                chunk = f.read(size - entry["offset"])
        except OSError:
            return list(entry["events"])
        # 只消费到最后一个完整换行；残行等写完再读
        last_nl = chunk.rfind(b"\n")
        if last_nl < 0:
            return list(entry["events"])
        consumed = chunk[: last_nl + 1]
        entry["offset"] += last_nl + 1
        for line in consumed.decode("utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue  # one bad line must not lose the rest
            if isinstance(e, dict):
                entry["events"].append(e)
        if len(entry["events"]) > MAX_CACHED_EVENTS:
            entry["events"] = entry["events"][-MAX_CACHED_EVENTS:]

    return list(entry["events"])


def read_json(path: str | Path, ttl: float = 5.0, default=None):
    """JSON file read with a TTL cache (state files on short ui.timer polls)."""
    p = Path(path)
    key = str(p)
    now = time.monotonic()
    entry = _json_cache.get(key)
    if entry and now - entry["read_at"] < ttl:
        return entry["data"]
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        data = default
    _json_cache[key] = {"read_at": now, "data": data}
    return data


def read_sched_events(jarvis_dir: str | Path) -> list[dict]:
    """sched_events.jsonl + rotated `.1` generation, oldest first."""
    base = Path(jarvis_dir) / "sched_events.jsonl"
    events: list[dict] = []
    rotated = base.with_name(base.name + ".1")
    if rotated.exists():
        events.extend(read_jsonl_tail(rotated))
    events.extend(read_jsonl_tail(base))
    return events
