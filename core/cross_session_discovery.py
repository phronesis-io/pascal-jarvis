"""Session discovery and ownership filtering for cross-session continuity."""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Callable, Iterable

from core.cross_session_parsing import SessionTail, _claude_tail, _codex_tail


DEFAULT_WINDOW_HOURS = 24
SESSION_NAMESPACE = uuid.UUID("a1b2c3d4-e5f6-7890-abcd-ef1234567890")
MAX_SCAN_FILES = 240
MAX_CONTEXT_SESSIONS = 6


def _read_mapping(path: Path | None) -> dict:
    if not path or not path.is_file():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _managed_claude_ids(
    tracker_path: Path | None,
    jobs_registry_path: Path | None,
) -> set[str]:
    """Find Lark conversations and background Claude sessions owned by Jarvis."""
    tracker = _read_mapping(tracker_path)
    jobs = _read_mapping(jobs_registry_path)
    if not tracker and not jobs:
        return set()
    managed: set[str] = set()
    for conv_key, entry in tracker.items():
        if not isinstance(entry, dict):
            continue
        try:
            counter = max(0, int(entry.get("counter") or 0))
        except (TypeError, ValueError):
            counter = 0
        managed.update(
            str(uuid.uuid5(SESSION_NAMESPACE, f"{conv_key}-{index}"))
            for index in range(1, counter + 1)
        )
        if entry.get("session_id"):
            managed.add(str(entry["session_id"]))
    for entry in jobs.values():
        if not isinstance(entry, dict):
            continue
        if entry.get("session_id"):
            managed.add(str(entry["session_id"]))
        session_ids = entry.get("session_ids")
        if isinstance(session_ids, list):
            managed.update(str(value) for value in session_ids if value)
    return managed


def _paths(root: Path, provider: str) -> Iterable[Path]:
    if not root.is_dir():
        return ()
    if provider == "claude":
        # Top-level provider sessions only; nested files are Claude subagents.
        return root.glob("*/*.jsonl")
    return root.rglob("*.jsonl")


def discover_interactive_sessions(
    *,
    claude_root: str | Path | None = None,
    codex_root: str | Path | None = None,
    tracker_path: str | Path | None = None,
    jobs_registry_path: str | Path | None = None,
    window_hours: int = DEFAULT_WINDOW_HOURS,
    limit: int = MAX_CONTEXT_SESSIONS,
    now: Callable[[], float] = time.time,
    max_scan_files: int = MAX_SCAN_FILES,
    claude_parser: Callable[[Path], SessionTail | None] = _claude_tail,
    codex_parser: Callable[[Path], SessionTail | None] = _codex_tail,
) -> list[SessionTail]:
    """Discover recent human-driven provider sessions, newest first."""
    home = Path.home()
    claude_root = Path(claude_root or os.environ.get("CROSS_SESSION_CLAUDE_ROOT")
                       or home / ".claude" / "projects")
    codex_root = Path(codex_root or os.environ.get("CROSS_SESSION_CODEX_ROOT")
                      or home / ".codex" / "sessions")
    if tracker_path is None:
        jarvis_dir = os.environ.get("JARVIS_DIR", "")
        tracker_path = Path(jarvis_dir) / "active_sessions.json" if jarvis_dir else None
    else:
        tracker_path = Path(tracker_path)
    if jobs_registry_path is None:
        jarvis_dir = os.environ.get("JARVIS_DIR", "")
        if jarvis_dir:
            jobs_registry_path = Path(jarvis_dir) / "jobs" / "registry.json"
        elif tracker_path and tracker_path.name == "active_sessions.json":
            jobs_registry_path = tracker_path.parent / "jobs" / "registry.json"
    else:
        jobs_registry_path = Path(jobs_registry_path)
    managed_claude = _managed_claude_ids(tracker_path, jobs_registry_path)
    cutoff = now() - max(1, int(window_hours)) * 3600
    candidates: list[tuple[float, str, Path]] = []
    for provider, root in (("claude", claude_root), ("codex", codex_root)):
        for path in _paths(root, provider):
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if mtime >= cutoff:
                candidates.append((mtime, provider, path))
    candidates.sort(key=lambda item: item[0], reverse=True)

    sessions: list[SessionTail] = []
    valid_scans = 0
    for _, provider, path in candidates:
        if provider == "claude" and path.stem in managed_claude:
            continue
        session = claude_parser(path) if provider == "claude" else codex_parser(path)
        if session is None:
            continue
        valid_scans += 1
        if session.session_id in managed_claude:
            if valid_scans >= max_scan_files:
                break
            continue
        sessions.append(session)
        if len(sessions) >= max(1, int(limit)):
            break
        if valid_scans >= max_scan_files:
            break
    return sessions
