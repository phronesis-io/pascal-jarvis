"""Deploy registration, stale-runtime verification, and delivery smoke tests."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import time
from pathlib import Path

from core.delivery import DeliveryEnvelope, DeliveryPipeline

RUNTIME_PATHS = (
    "core",
    "dashboard",
    "admin.py",
    "daemon.py",
    "bot.sh",
    "jarvis.yaml",
    "HEARTBEAT.md",
)


def _root(value: str | Path | None = None) -> Path:
    return Path(value or os.environ.get("JARVIS_DIR")
                or Path(__file__).resolve().parent.parent).resolve()


def _db_path(root: Path, value: str | Path | None = None) -> Path:
    return Path(value or os.environ.get("JARVIS_DB_PATH")
                or root / "data" / "jarvis.db")


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path), timeout=5)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=5000")
    db.execute("""
        CREATE TABLE IF NOT EXISTS runtime_versions (
            component TEXT PRIMARY KEY,
            pid INTEGER NOT NULL,
            git_head TEXT NOT NULL DEFAULT '',
            code_mtime REAL NOT NULL DEFAULT 0,
            started_epoch REAL NOT NULL,
            heartbeat_sha256 TEXT NOT NULL DEFAULT '',
            metadata TEXT NOT NULL DEFAULT '{}'
        )
    """)
    db.commit()
    return db


def _git(root: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", *args], cwd=root, capture_output=True, text=True,
            timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    # Porcelain status uses a meaningful leading space for worktree-only
    # changes. Preserve it so callers can parse the fixed-width XY prefix.
    return result.stdout.rstrip() if result.returncode == 0 else ""


def git_head(root: str | Path | None = None) -> str:
    return _git(_root(root), "rev-parse", "HEAD")


def _runtime_files(root: Path):
    for relative in RUNTIME_PATHS:
        path = root / relative
        if path.is_file():
            yield path
        elif path.is_dir():
            for candidate in path.rglob("*"):
                if (candidate.is_file()
                        and "__pycache__" not in candidate.parts
                        and candidate.suffix in {".py", ".sh", ".yaml", ".json"}):
                    yield candidate


def code_mtime(root: str | Path | None = None) -> float:
    values = []
    for path in _runtime_files(_root(root)):
        try:
            values.append(path.stat().st_mtime)
        except OSError:
            continue
    return max(values, default=0.0)


def _digest(path: Path | None) -> str:
    if not path or not path.is_file():
        return ""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def _heartbeat_metadata(path: Path | None) -> dict:
    if not path or not path.is_file():
        return {"heartbeat_loaded": False, "heartbeat_tasks": 0}
    try:
        from core.heartbeat import parse_heartbeat
        tasks = parse_heartbeat(path)
    except Exception:
        return {"heartbeat_loaded": False, "heartbeat_tasks": 0}
    return {
        "heartbeat_loaded": bool(tasks),
        "heartbeat_tasks": len(tasks),
        "heartbeat_path": str(path),
    }


def register_runtime(
    component: str,
    *,
    pid: int | None = None,
    root: str | Path | None = None,
    db_path: str | Path | None = None,
    heartbeat_file: str | Path | None = None,
    metadata: dict | None = None,
) -> dict:
    project = _root(root)
    process_id = int(pid or os.getpid())
    heartbeat_path = (
        Path(heartbeat_file).resolve() if heartbeat_file else None)
    details = dict(metadata or {})
    if heartbeat_path:
        details.update(_heartbeat_metadata(heartbeat_path))
    row = {
        "component": str(component).strip(),
        "pid": process_id,
        "git_head": git_head(project),
        "code_mtime": code_mtime(project),
        "started_epoch": time.time(),
        "heartbeat_sha256": _digest(heartbeat_path),
        "metadata": json.dumps(details, ensure_ascii=False, sort_keys=True),
    }
    if not row["component"]:
        raise ValueError("component is required")
    with _connect(_db_path(project, db_path)) as db:
        db.execute(
            """
            INSERT INTO runtime_versions (
                component,pid,git_head,code_mtime,started_epoch,
                heartbeat_sha256,metadata
            ) VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(component) DO UPDATE SET
                pid=excluded.pid,
                git_head=excluded.git_head,
                code_mtime=excluded.code_mtime,
                started_epoch=excluded.started_epoch,
                heartbeat_sha256=excluded.heartbeat_sha256,
                metadata=excluded.metadata
            """,
            (
                row["component"],
                row["pid"],
                row["git_head"],
                row["code_mtime"],
                row["started_epoch"],
                row["heartbeat_sha256"],
                row["metadata"],
            ),
        )
    return {**row, "metadata": details}


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError):
        return False


def _dirty_runtime_paths(root: Path) -> list[str]:
    output = _git(
        root, "status", "--porcelain", "--",
        "core", "dashboard", "admin.py", "daemon.py", "bot.sh",
        "jarvis.yaml", "HEARTBEAT.md",
    )
    return [line[3:] for line in output.splitlines() if len(line) > 3]


def verify_runtime(
    *,
    root: str | Path | None = None,
    db_path: str | Path | None = None,
    required: list[str] | tuple[str, ...] = (),
) -> dict:
    project = _root(root)
    current_head = git_head(project)
    current_mtime = code_mtime(project)
    with _connect(_db_path(project, db_path)) as db:
        rows = [
            dict(row) for row in db.execute(
                "SELECT * FROM runtime_versions ORDER BY component")
        ]
    by_name = {str(row["component"]): row for row in rows}
    issues: list[str] = []
    components = []
    for name in required:
        if name not in by_name:
            issues.append(f"{name}: no runtime registration")
    for row in rows:
        name = str(row["component"])
        alive = _pid_alive(int(row["pid"]))
        status_issues = []
        if not alive:
            status_issues.append("process is not alive")
        if current_head and str(row["git_head"]) != current_head:
            status_issues.append("running git commit differs from HEAD")
        if alive and current_mtime > float(row["code_mtime"] or 0) + 0.001:
            status_issues.append("runtime code changed after process start")
        heartbeat_hash = str(row.get("heartbeat_sha256") or "")
        metadata = {}
        try:
            metadata = json.loads(row.get("metadata") or "{}")
        except (json.JSONDecodeError, TypeError):
            pass
        heartbeat_path = Path(str(metadata.get("heartbeat_path", "")))
        if heartbeat_hash:
            if _digest(heartbeat_path) != heartbeat_hash:
                status_issues.append("HEARTBEAT.md changed after process start")
            if not metadata.get("heartbeat_loaded"):
                status_issues.append("HEARTBEAT.md was not loaded")
            if int(metadata.get("heartbeat_tasks", 0) or 0) < 1:
                status_issues.append("HEARTBEAT.md has no parsed tasks")
        if status_issues:
            issues.extend(f"{name}: {item}" for item in status_issues)
        components.append({
            "component": name,
            "pid": int(row["pid"]),
            "alive": alive,
            "git_head": str(row["git_head"]),
            "started_epoch": float(row["started_epoch"]),
            "issues": status_issues,
        })
    if not rows:
        issues.append("no runtime registrations")
    dirty_paths = _dirty_runtime_paths(project)
    return {
        "ok": not issues,
        "git_head": current_head,
        "components": components,
        "issues": issues,
        "warnings": [
            "uncommitted runtime code: " + ", ".join(dirty_paths)
        ] if dirty_paths else [],
    }


def smoke_delivery(
    *,
    root: str | Path | None = None,
    db_path: str | Path | None = None,
    timeout: float = 3.0,
) -> dict:
    """Exercise the full policy/state machine without interrupting the owner."""
    project = _root(root)
    started = time.monotonic()
    pipeline = DeliveryPipeline(project, db_path=db_path)
    smoke_id = f"deploy-smoke:{int(time.time() * 1000)}:{os.getpid()}"
    result = pipeline.deliver(DeliveryEnvelope(
        source="deploy-smoke",
        kind="web",
        payload={"text": "Jarvis deploy smoke delivery"},
        attention="notice",
        requested_channel="web",
        memorial_id=smoke_id,
        dedup_key=smoke_id,
        metadata={
            "bypass_dedup": True,
            "bypass_throttle": True,
            "bypass_quiet": True,
            "healthcheck": True,
        },
    ))
    elapsed = time.monotonic() - started
    ok = result.state in {"delivered", "read", "acted"} and elapsed <= timeout
    if ok:
        pipeline.confirm(result.delivery_id, "acted")
    return {
        "ok": ok,
        "delivery_id": result.delivery_id,
        "state": "acted" if ok else result.state,
        "channel": result.channel,
        "elapsed_seconds": round(elapsed, 4),
        "timeout_seconds": float(timeout),
        "reason": result.reason,
    }


def _print(value: dict) -> int:
    print(json.dumps(value, ensure_ascii=False, indent=2))
    return 0 if value.get("ok", True) else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Jarvis deploy verification")
    sub = parser.add_subparsers(dest="command", required=True)

    register = sub.add_parser("register")
    register.add_argument("component")
    register.add_argument("--pid", type=int, default=0)
    register.add_argument("--heartbeat-file", default="")
    register.set_defaults(func=lambda args: _print({
        "ok": True,
        **register_runtime(
            args.component, pid=args.pid or None,
            heartbeat_file=args.heartbeat_file or None),
    }))

    verify = sub.add_parser("verify")
    verify.add_argument("--require", action="append", default=[])
    verify.set_defaults(func=lambda args: _print(
        verify_runtime(required=args.require)))

    smoke = sub.add_parser("smoke")
    smoke.add_argument("--timeout", type=float, default=3.0)
    smoke.set_defaults(func=lambda args: _print(
        smoke_delivery(timeout=args.timeout)))

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
