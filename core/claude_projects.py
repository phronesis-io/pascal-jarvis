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


def canonical_jarvis_dir(repo_dir: str | Path | None = None) -> Path:
    """Return the primary checkout behind ``repo_dir``, including worktrees."""
    root = Path(repo_dir or JARVIS_DIR).expanduser().resolve()
    dot_git = root / ".git"
    if not dot_git.is_file():
        return root
    try:
        marker = dot_git.read_text(encoding="utf-8").strip()
        if not marker.startswith("gitdir:"):
            return root
        git_dir = Path(marker.split(":", 1)[1].strip()).expanduser()
        if not git_dir.is_absolute():
            git_dir = (root / git_dir).resolve()
        common_marker = git_dir / "commondir"
        if not common_marker.is_file():
            return root
        common = Path(common_marker.read_text(encoding="utf-8").strip())
        common_dir = (git_dir / common).resolve()
        return common_dir.parent if common_dir.name == ".git" else root
    except OSError:
        return root


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
    roots = [
        Path(work_dir),
        Path(work_dir) / "repos",
        canonical_jarvis_dir(),
    ]
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
    """Heartbeat memory for the primary checkout behind any worktree."""
    return project_dir(canonical_jarvis_dir()) / "memory"
