"""Conservative retention and privacy maintenance for local runtime assets."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable


TERMINAL_DELIVERY_STATES = ("delivered", "read", "acted", "suppressed", "failed")
PRIVATE_ROOT_FILES = (
    ".memory_cache",
    "engagement_log.jsonl",
    "heartbeat_outbox.jsonl",
    "memorials.jsonl",
    "sched_events.jsonl",
    "silent_outputs.jsonl",
)
TEMP_PATTERNS = (
    "jarvis-audit-lark-*.json",
    "jarvis-admin-*.html",
    "jarvis-*-audit.*",
    "jarvis-tailscaled.log",
)


def _table_exists(db: sqlite3.Connection, name: str) -> bool:
    return db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def maintain_sqlite(
    path: str | Path,
    *,
    now: float | None = None,
    operational_days: int = 90,
    audit_days: int = 180,
) -> dict:
    """Prune old operational detail while preserving user-visible source rows."""
    db_path = Path(path)
    result = {"database": db_path.name, "deleted": 0, "status": "skipped"}
    if not db_path.is_file() or db_path.is_symlink():
        return result
    current = float(time.time() if now is None else now)
    operational_cutoff = current - max(30, operational_days) * 86400
    audit_cutoff = (
        datetime.fromtimestamp(current, timezone.utc)
        - timedelta(days=max(90, audit_days))
    ).isoformat()
    deleted = 0
    try:
        with closing(sqlite3.connect(str(db_path), timeout=3)) as db:
            db.execute("PRAGMA busy_timeout=3000")
            if _table_exists(db, "schedule_events"):
                deleted += db.execute(
                    "DELETE FROM schedule_events WHERE created_epoch<?",
                    (operational_cutoff,),
                ).rowcount
            if _table_exists(db, "delivery_envelopes"):
                placeholders = ",".join("?" for _ in TERMINAL_DELIVERY_STATES)
                params = (operational_cutoff, *TERMINAL_DELIVERY_STATES)
                for table in ("delivery_attempts", "delivery_events"):
                    if _table_exists(db, table):
                        deleted += db.execute(
                            f"DELETE FROM {table} WHERE delivery_id IN ("
                            "SELECT id FROM delivery_envelopes "
                            f"WHERE updated_epoch<? AND state IN ({placeholders}))",
                            params,
                        ).rowcount
            if _table_exists(db, "audit_runs"):
                old_runs = (
                    "SELECT id FROM audit_runs WHERE started_at<? AND NOT EXISTS ("
                    "SELECT 1 FROM audit_issues WHERE audit_issues.run_id=audit_runs.id "
                    "AND audit_issues.status='open')"
                )
                for table in ("conversation_events", "session_messages", "audit_issues"):
                    if _table_exists(db, table):
                        deleted += db.execute(
                            f"DELETE FROM {table} WHERE run_id IN ({old_runs})",
                            (audit_cutoff,),
                        ).rowcount
                deleted += db.execute(
                    f"DELETE FROM audit_runs WHERE id IN ({old_runs})",
                    (audit_cutoff,),
                ).rowcount
            db.execute("PRAGMA optimize")
            db.commit()
    except sqlite3.Error as exc:
        result.update(status="error", error=type(exc).__name__)
        return result
    result.update(status="ok", deleted=max(0, deleted))
    return result


def enforce_private_modes(root: str | Path) -> dict:
    """Migrate allow-listed runtime state to owner-only permissions."""
    base = Path(root)
    changed = 0
    for directory in (base / "data", base / "tmp"):
        if not directory.is_dir() or directory.is_symlink():
            continue
        for path in (directory, *directory.rglob("*")):
            try:
                if path.is_symlink():
                    continue
                desired = 0o700 if path.is_dir() else 0o600
                if path.stat().st_mode & 0o777 != desired:
                    path.chmod(desired)
                    changed += 1
            except OSError:
                continue
    for name in PRIVATE_ROOT_FILES:
        path = base / name
        try:
            if path.is_file() and not path.is_symlink() \
                    and path.stat().st_mode & 0o777 != 0o600:
                path.chmod(0o600)
                changed += 1
        except OSError:
            continue
    return {"status": "ok", "changed": changed}


def clean_private_temp(
    temp_root: str | Path = "/tmp",
    *,
    now: float | None = None,
    min_age_days: int = 7,
) -> dict:
    """Delete only retired/audit temp files owned by the current user."""
    base = Path(temp_root)
    current = float(time.time() if now is None else now)
    cutoff = current - max(1, min_age_days) * 86400
    removed: list[str] = []
    seen: set[Path] = set()
    for pattern in TEMP_PATTERNS:
        for path in base.glob(pattern):
            if path in seen:
                continue
            seen.add(path)
            try:
                stat = path.lstat()
                if (not path.is_file() or path.is_symlink()
                        or stat.st_uid != os.getuid() or stat.st_mtime > cutoff):
                    continue
                path.unlink()
                removed.append(path.name)
            except OSError:
                continue
    return {"status": "ok", "removed": sorted(removed)}


def memory_git_gc(
    memory_root: str | Path | None,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict:
    path = Path(memory_root).expanduser() if memory_root else None
    if path is None or not (path / ".git").is_dir():
        return {"status": "not_a_repository"}
    try:
        result = runner(
            ["git", "-C", str(path), "gc", "--auto"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"status": "error", "error": type(exc).__name__}
    return {"status": "ok" if result.returncode == 0 else "error"}


def maintain(
    root: str | Path,
    *,
    memory_root: str | Path | None = None,
    temp_root: str | Path = "/tmp",
    now: float | None = None,
) -> dict:
    base = Path(root)
    databases = [
        maintain_sqlite(path, now=now)
        for path in sorted((base / "data").glob("*.db"))
    ] if (base / "data").is_dir() else []
    return {
        "status": "ok",
        "permissions": enforce_private_modes(base),
        "databases": databases,
        "temporary": clean_private_temp(temp_root, now=now),
        "memory_git": memory_git_gc(memory_root),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=os.environ.get("JARVIS_DIR", "."))
    parser.add_argument("--memory", default=os.environ.get("MEMORY_DIR", ""))
    parser.add_argument("--temp-root", default="/tmp")
    args = parser.parse_args(argv)
    print(json.dumps(
        maintain(args.root, memory_root=args.memory or None, temp_root=args.temp_root),
        ensure_ascii=False,
        sort_keys=True,
    ))
    return 0


if __name__ == "__main__":
    sys.exit(main())
