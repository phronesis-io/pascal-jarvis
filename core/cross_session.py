"""Owner-only continuity facade for interactive Claude Code and Codex sessions.

Provider parsing, session discovery, and projection state live in dedicated
modules. This facade preserves the original import surface and command line.
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

from core import cross_session_discovery as _discovery
from core import cross_session_parsing as _parsing
from core import cross_session_projection as _projection
from core.cross_session_discovery import (
    DEFAULT_WINDOW_HOURS,
    MAX_CONTEXT_SESSIONS,
    SESSION_NAMESPACE,
)
from core.cross_session_parsing import (
    MAX_LINE_BYTES,
    MAX_TAIL_BYTES,
    MAX_TURN_CHARS,
    MAX_TURNS_PER_SESSION,
    SessionTail,
    Turn,
    _AUTOMATED_SESSION_PREFIXES,
    _SECRET_RE,
    _SYNTHETIC_PREFIXES,
    _codex_initial_user,
    _codex_is_interactive,
    _codex_meta,
    _dedupe_adjacent,
    _head_records,
    _is_synthetic,
    _mtime_iso,
    _recent_with_user,
    _tail_records,
    _turn_identity,
    redact_text,
)
from core.cross_session_projection import (
    CONTEXT_TAIL,
    DEFAULT_STATE_FILE,
    MAX_CONTEXT_CHARS,
    MAX_CONTEXT_TURNS,
    MAX_INCREMENTAL_CHARS,
    MAX_NEW_TURNS_PER_SESSION,
    _first_turn_after,
    _format_turn,
    _load_state,
    _overlap_start,
    _project_label,
    _short_ts,
    _timestamp_epoch,
)


# Kept mutable on the facade for existing runtime/test overrides.
MAX_SCAN_FILES = _discovery.MAX_SCAN_FILES


def _claude_tail(path: Path) -> SessionTail | None:
    return _parsing._claude_tail(
        path,
        head_records=_head_records,
        tail_records=_tail_records,
    )


def _codex_tail(path: Path) -> SessionTail | None:
    return _parsing._codex_tail(
        path,
        head_records=_head_records,
        tail_records=_tail_records,
    )


def _read_mapping(path: Path | None) -> dict:
    return _discovery._read_mapping(path)


def _managed_claude_ids(
    tracker_path: Path | None,
    jobs_registry_path: Path | None,
) -> set[str]:
    return _discovery._managed_claude_ids(tracker_path, jobs_registry_path)


def _paths(root: Path, provider: str):
    return _discovery._paths(root, provider)


def discover_interactive_sessions(
    *,
    claude_root: str | Path | None = None,
    codex_root: str | Path | None = None,
    tracker_path: str | Path | None = None,
    jobs_registry_path: str | Path | None = None,
    window_hours: int = DEFAULT_WINDOW_HOURS,
    limit: int = MAX_CONTEXT_SESSIONS,
) -> list[SessionTail]:
    """Discover recent human-driven provider sessions, newest first."""
    return _discovery.discover_interactive_sessions(
        claude_root=claude_root,
        codex_root=codex_root,
        tracker_path=tracker_path,
        jobs_registry_path=jobs_registry_path,
        window_hours=window_hours,
        limit=limit,
        now=time.time,
        max_scan_files=MAX_SCAN_FILES,
        claude_parser=_claude_tail,
        codex_parser=_codex_tail,
    )


def collect_incremental(
    *,
    state_file: str | Path | None = None,
    claude_root: str | Path | None = None,
    codex_root: str | Path | None = None,
    tracker_path: str | Path | None = None,
    jobs_registry_path: str | Path | None = None,
    window_hours: int = DEFAULT_WINDOW_HOURS,
) -> str:
    """Return unseen provider turns and atomically advance per-file watermarks."""
    return _projection.collect_incremental(
        state_file=state_file,
        claude_root=claude_root,
        codex_root=codex_root,
        tracker_path=tracker_path,
        jobs_registry_path=jobs_registry_path,
        window_hours=window_hours,
        discover=discover_interactive_sessions,
        now=time.time,
    )


def build_prompt_context(
    *,
    claude_root: str | Path | None = None,
    codex_root: str | Path | None = None,
    tracker_path: str | Path | None = None,
    jobs_registry_path: str | Path | None = None,
    window_hours: int = DEFAULT_WINDOW_HOURS,
    max_chars: int = MAX_CONTEXT_CHARS,
) -> str:
    """Build a bounded owner-private projection for the next main-agent turn."""
    return _projection.build_prompt_context(
        claude_root=claude_root,
        codex_root=codex_root,
        tracker_path=tracker_path,
        jobs_registry_path=jobs_registry_path,
        window_hours=window_hours,
        max_chars=max_chars,
        discover=discover_interactive_sessions,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Jarvis cross-session continuity")
    sub = parser.add_subparsers(dest="command", required=True)
    inc = sub.add_parser("incremental")
    inc.add_argument("--state-file", default="")
    inc.add_argument("--window-hours", type=int, default=DEFAULT_WINDOW_HOURS)
    context = sub.add_parser("context")
    context.add_argument("--window-hours", type=int, default=DEFAULT_WINDOW_HOURS)
    context.add_argument("--max-chars", type=int, default=MAX_CONTEXT_CHARS)
    args = parser.parse_args(argv)
    if args.command == "incremental":
        output = collect_incremental(
            state_file=args.state_file or None,
            window_hours=args.window_hours,
        )
    else:
        output = build_prompt_context(
            window_hours=args.window_hours,
            max_chars=args.max_chars,
        )
    if output:
        print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
