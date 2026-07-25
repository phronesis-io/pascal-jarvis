"""Call-time resolution for runtime state paths shared across components."""

from __future__ import annotations

import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def database_path(
    root: str | Path | None = None,
    explicit: str | Path | None = None,
    *,
    default: str | Path | None = None,
) -> Path:
    """Resolve the one Jarvis database path for the current runtime."""
    if explicit is not None:
        return Path(explicit)
    override = str(os.environ.get("JARVIS_DB_PATH") or "").strip()
    if override:
        return Path(override)
    if root is not None:
        return Path(root) / "data" / "jarvis.db"
    runtime_root = str(os.environ.get("JARVIS_DIR") or "").strip()
    if runtime_root:
        return Path(runtime_root) / "data" / "jarvis.db"
    return Path(default) if default is not None else PROJECT_ROOT / "data" / "jarvis.db"
