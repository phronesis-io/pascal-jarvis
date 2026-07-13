"""Locate Claude Code project directories for THIS install.

Claude Code stores each project's sessions and auto-memory under
``~/.claude/projects/<slug>`` where ``<slug>`` is the project working
directory with ``/`` and ``.`` mapped to ``-``. Jarvis code must never
hardcode a specific user's slug — derive it from the configured paths so
every install (any username, any checkout location) finds its own data.

Jarvis cares about three roots:
  - ``work_dir`` (jarvis.yaml; where conversation Claude runs)
  - ``work_dir/repos`` (coding sessions started in the repos dir)
  - the Jarvis repo itself (this checkout)
"""
from __future__ import annotations

import os
from pathlib import Path

JARVIS_DIR = Path(__file__).resolve().parents[1]


def path_slug(path: str | Path) -> str:
    """The Claude Code project-dir slug for a filesystem path."""
    resolved = str(Path(path).expanduser().resolve())
    return resolved.replace("/", "-").replace(".", "-")


def projects_root() -> Path:
    return Path.home() / ".claude" / "projects"


def project_dir(path: str | Path) -> Path:
    """``~/.claude/projects/<slug>`` for the given working directory."""
    return projects_root() / path_slug(path)


def jarvis_project_dirs(work_dir: str | Path | None = None) -> list[Path]:
    """Project dirs for this install's three roots (deduped, ordered).

    ``work_dir`` defaults to the configured jarvis work_dir; pass explicitly
    to avoid importing config (e.g. in early-boot contexts).
    """
    if work_dir is None:
        work_dir = _resolve_work_dir()
    roots = [Path(work_dir), Path(work_dir) / "repos", JARVIS_DIR]
    seen: set[str] = set()
    dirs: list[Path] = []
    for root in roots:
        d = project_dir(root)
        if str(d) not in seen:
            seen.add(str(d))
            dirs.append(d)
    return dirs


def _resolve_work_dir() -> Path:
    """Best-effort work_dir: env > jarvis.yaml > two levels above the repo."""
    wd = os.environ.get("WORK_DIR")
    if wd:
        return Path(wd)
    try:
        from core.config import Config
        cfg = Config()
        return cfg.work_dir
    except Exception:
        return JARVIS_DIR.parents[1]


def auto_memory_dir(work_dir: str | Path | None = None) -> Path:
    """Auto-memory for the jarvis root: ``project_dir(work_dir) / "memory"``."""
    if work_dir is None:
        work_dir = _resolve_work_dir()
    return project_dir(work_dir) / "memory"


def heartbeat_memory_dir() -> Path:
    """Heartbeat memory: ``project_dir(JARVIS_DIR) / "memory"``."""
    return project_dir(JARVIS_DIR) / "memory"
