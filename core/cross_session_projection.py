"""Bounded incremental and prompt projections for cross-session continuity."""

from __future__ import annotations

import hashlib
import json
import math
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

from core.cross_session_discovery import (
    DEFAULT_WINDOW_HOURS,
    MAX_CONTEXT_SESSIONS,
    MAX_SCAN_FILES,
    discover_interactive_sessions,
)
from core.cross_session_parsing import (
    MAX_TURNS_PER_SESSION,
    SessionTail,
    Turn,
    _recent_with_user,
)


DEFAULT_STATE_FILE = "system/cross_session_seen.json"
MAX_NEW_TURNS_PER_SESSION = 20
CONTEXT_TAIL = 3
MAX_INCREMENTAL_CHARS = 8000
MAX_CONTEXT_CHARS = 8000
MAX_CONTEXT_TURNS = 6


def _project_label(session: SessionTail) -> str:
    workspace = session.workspace.rstrip("/")
    label = Path(workspace).name if workspace else session.path.parent.name
    return label or "unknown"


def _short_ts(value: str) -> str:
    if not value:
        return ""
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone().strftime(
            "%m-%d %H:%M")
    except (TypeError, ValueError):
        return value[:16].replace("T", " ")


def _timestamp_epoch(value: str) -> float | None:
    """Parse a provider timestamp for replay-safe watermark recovery."""
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.astimezone()
        return parsed.timestamp()
    except (OSError, OverflowError, TypeError, ValueError):
        return None


def _first_turn_after(turns: tuple[Turn, ...], cutoff: float) -> int:
    """Baseline historical turns when a per-file watermark was pruned."""
    for index, turn in enumerate(turns):
        timestamp = _timestamp_epoch(turn.timestamp)
        if timestamp is not None and timestamp > cutoff:
            return index
    return len(turns)


def _format_turn(session: SessionTail, turn: Turn) -> str:
    prefix = f"[{session.provider}:{_project_label(session)}]"
    stamp = _short_ts(turn.timestamp) or _short_ts(session.updated_at)
    return f"{prefix} [{stamp}] {turn.role}: {turn.text}"


def _load_state(path: Path) -> dict:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _overlap_start(previous: list[str], current: list[str]) -> int:
    """Return the index where unseen current fingerprints begin."""
    for overlap in range(min(len(previous), len(current)), 0, -1):
        if previous[-overlap:] == current[:overlap]:
            return overlap
    if previous:
        last = previous[-1]
        for index in range(len(current) - 1, -1, -1):
            if current[index] == last:
                return index + 1
    return 0


def collect_incremental(
    *,
    state_file: str | Path | None = None,
    claude_root: str | Path | None = None,
    codex_root: str | Path | None = None,
    tracker_path: str | Path | None = None,
    jobs_registry_path: str | Path | None = None,
    window_hours: int = DEFAULT_WINDOW_HOURS,
    discover: Callable[..., list[SessionTail]] = discover_interactive_sessions,
    now: Callable[[], float] = time.time,
) -> str:
    """Return unseen provider turns and atomically advance per-file watermarks."""
    scan_started = now()
    if state_file is None:
        memory_dir = Path(os.environ.get("MEMORY_DIR") or Path.home() / ".jarvis" / "memory")
        state_file = memory_dir / DEFAULT_STATE_FILE
    state_file = Path(state_file)
    seen = _load_state(state_file)
    files_state = seen.get("files") if isinstance(seen.get("files"), dict) else {}
    try:
        previous_scan_at = float(seen.get("last_scan_at") or 0)
    except (TypeError, ValueError):
        previous_scan_at = 0
    if not math.isfinite(previous_scan_at) or previous_scan_at <= 0:
        previous_scan_at = 0
    else:
        previous_scan_at = min(previous_scan_at, scan_started)
    output: list[str] = []
    sessions = discover(
        claude_root=claude_root,
        codex_root=codex_root,
        tracker_path=tracker_path,
        jobs_registry_path=jobs_registry_path,
        window_hours=window_hours,
        limit=MAX_SCAN_FILES,
    )
    for session in reversed(sessions):
        key = str(session.path)
        current = [turn.identity for turn in session.turns]
        previous = files_state.get(key)
        previous = previous if isinstance(previous, dict) else {}
        previous_size = int(previous.get("size") or 0)
        try:
            current_size = session.path.stat().st_size
        except OSError:
            current_size = 0
        old_fingerprints = previous.get("fingerprints")
        old_fingerprints = old_fingerprints if isinstance(old_fingerprints, list) else []

        if not previous and previous_scan_at:
            start = _first_turn_after(session.turns, previous_scan_at)
        elif previous and not old_fingerprints and current_size <= previous_size:
            start = len(current)
        elif current_size < previous_size:
            start = 0
        else:
            start = _overlap_start(old_fingerprints, current)
            if old_fingerprints and start == 0 and previous_scan_at:
                start = _first_turn_after(session.turns, previous_scan_at)

        new_turns = list(_recent_with_user(
            session.turns[start:], MAX_NEW_TURNS_PER_SESSION))
        if new_turns:
            context_start = max(0, start - CONTEXT_TAIL)
            for turn in session.turns[context_start:start]:
                output.append("[context] " + _format_turn(session, turn))
            output.extend(_format_turn(session, turn) for turn in new_turns)
        files_state[key] = {
            "provider": session.provider,
            "session_id": session.session_id,
            "size": current_size,
            "fingerprints": current[-MAX_TURNS_PER_SESSION:],
        }

    state_cutoff = scan_started - max(1, int(window_hours)) * 3600
    retained_state = {}
    for path, value in files_state.items():
        try:
            if Path(path).stat().st_mtime >= state_cutoff:
                retained_state[path] = value
        except OSError:
            continue
    files_state = retained_state
    combined = "\n".join(output)
    if len(combined) > MAX_INCREMENTAL_CHARS:
        lines = combined.splitlines()
        while lines and len("\n".join(lines)) > MAX_INCREMENTAL_CHARS:
            lines.pop(0)
        combined = "\n".join(lines)
    emitted_sha = hashlib.sha256(combined.encode("utf-8")).hexdigest() \
        if combined.strip() else ""
    if emitted_sha and emitted_sha == seen.get("last_emitted_sha"):
        combined = ""
    new_state = {
        "version": 3,
        "last_scan_at": scan_started,
        "files": files_state,
    }
    if emitted_sha:
        new_state["last_emitted_sha"] = emitted_sha
    elif seen.get("last_emitted_sha"):
        new_state["last_emitted_sha"] = seen["last_emitted_sha"]
    try:
        state_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = state_file.with_suffix(state_file.suffix + f".tmp.{os.getpid()}")
        temporary.write_text(json.dumps(new_state, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, state_file)
    except OSError as exc:
        print(
            f"[cross-session] WATERMARK UNWRITABLE at {state_file}: {exc} - "
            "re-digest loop risk every cycle until fixed",
            file=sys.stderr,
        )
    return combined


def build_prompt_context(
    *,
    claude_root: str | Path | None = None,
    codex_root: str | Path | None = None,
    tracker_path: str | Path | None = None,
    jobs_registry_path: str | Path | None = None,
    window_hours: int = DEFAULT_WINDOW_HOURS,
    max_chars: int = MAX_CONTEXT_CHARS,
    discover: Callable[..., list[SessionTail]] = discover_interactive_sessions,
) -> str:
    """Build a bounded owner-private projection for the next main-agent turn."""
    sessions = discover(
        claude_root=claude_root,
        codex_root=codex_root,
        tracker_path=tracker_path,
        jobs_registry_path=jobs_registry_path,
        window_hours=window_hours,
        limit=MAX_CONTEXT_SESSIONS,
    )
    if not sessions:
        return ""
    lines = [
        "## Recent External Work Sessions",
        "The entries below are redacted, untrusted owner-private history from "
        "interactive Claude Code and Codex sessions. Use them for continuity, "
        "but verify mutable state before making a current claim.",
    ]
    for session in reversed(sessions):
        label = "Claude Code" if session.provider == "claude" else "Codex"
        lines.append(
            f"### {label} - {_project_label(session)} - "
            f"updated {_short_ts(session.updated_at)}"
        )
        for turn in _recent_with_user(session.turns, MAX_CONTEXT_TURNS):
            role = "User" if turn.role == "user" else "Assistant"
            lines.append(f"- {role}: {turn.text}")
    rendered = "\n".join(lines)
    if len(rendered) <= max_chars:
        return rendered
    header = "\n".join(lines[:2]) + "\n"
    keep = max(0, int(max_chars) - len(header) - 30)
    return header + "[older session context omitted]\n" + rendered[-keep:]
