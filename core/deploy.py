"""Deploy registration, stale-runtime verification, and delivery smoke tests."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import time
from pathlib import Path

from core.delivery import DeliveryEnvelope, DeliveryPipeline, TransportResult

CODE_PATHS = (
    "core",
    "tasks",
    "scripts",
    "plugins",
    "handlers",
    "sources",
    "static",
    "admin.py",
    "daemon.py",
    "bot.sh",
    "restart.sh",
)
CONFIG_PATHS = (
    "components.yaml",
    "jarvis.yaml",
    "HEARTBEAT.md",
)
RUNTIME_PATHS = CODE_PATHS + CONFIG_PATHS


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
    db.execute("""
        CREATE TABLE IF NOT EXISTS release_receipts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            git_head TEXT NOT NULL,
            mode TEXT NOT NULL,
            recorded_epoch REAL NOT NULL,
            gate_json TEXT NOT NULL,
            runtime_json TEXT NOT NULL,
            components_json TEXT NOT NULL,
            smoke_json TEXT NOT NULL
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


def revision_contains(
    release_sha: str,
    resident_sha: str,
    *,
    root: str | Path | None = None,
    runner=subprocess.run,
) -> bool:
    """Return whether the resident revision contains the released commit."""
    release = str(release_sha or "").strip().lower()
    resident = str(resident_sha or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40}", release):
        raise ValueError("release_sha must be a full commit SHA")
    if not re.fullmatch(r"[0-9a-f]{40}", resident):
        raise ValueError("resident_sha must be a full commit SHA")
    if release == resident:
        return True
    try:
        result = runner(
            ["git", "merge-base", "--is-ancestor", release, resident],
            cwd=str(_root(root)),
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"git ancestry readback failed: {exc}") from exc
    if result.returncode in {0, 1}:
        return result.returncode == 0
    detail = (result.stderr or result.stdout or "git ancestry readback failed").strip()
    raise RuntimeError(detail[:300])


def _runtime_files(root: Path, paths: tuple[str, ...] = RUNTIME_PATHS):
    for relative in paths:
        path = root / relative
        if path.is_file():
            yield path
        elif path.is_dir():
            for candidate in path.rglob("*"):
                if (candidate.is_file()
                        and "__pycache__" not in candidate.parts
                        and candidate.suffix in {
                            ".py", ".sh", ".yaml", ".json", ".html", ".css", ".js"
                        }):
                    yield candidate


def code_mtime(
    root: str | Path | None = None,
    *,
    include_config: bool = True,
) -> float:
    values = []
    paths = RUNTIME_PATHS if include_config else CODE_PATHS
    for path in _runtime_files(_root(root), paths):
        try:
            values.append(path.stat().st_mtime)
        except OSError:
            continue
    return max(values, default=0.0)


def code_digest(
    root: str | Path | None = None,
    *,
    include_config: bool = True,
) -> str:
    """Hash runtime path names and contents; filesystem mtimes are irrelevant."""
    project = _root(root)
    paths = RUNTIME_PATHS if include_config else CODE_PATHS
    digest = hashlib.sha256()
    for path in sorted(_runtime_files(project, paths)):
        try:
            relative = path.relative_to(project).as_posix().encode("utf-8")
            content = path.read_bytes()
        except (OSError, ValueError):
            continue
        digest.update(relative)
        digest.update(b"\0")
        digest.update(content)
        digest.update(b"\0")
    return digest.hexdigest()


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
    details["code_sha256"] = code_digest(project, include_config=False)
    details["runtime_sha256"] = code_digest(project, include_config=True)
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
        root, "status", "--porcelain", "--", *RUNTIME_PATHS,
    )
    return [line[3:] for line in output.splitlines() if len(line) > 3]


def deregister_runtime(
    component: str,
    *,
    root: str | Path | None = None,
    db_path: str | Path | None = None,
) -> dict:
    """Remove a retired component's runtime registration.

    A retired surface (mobile-gateway 2026-08-11, dashboard 2026-08-21) never
    re-registers, so its last row would sit dead in `runtime_versions` and
    fail every unfiltered ``core.deploy verify`` with "process is not alive"
    forever. Deregistration is the explicit teardown step, not a side effect:
    live components must keep failing loudly when they die.
    """
    name = str(component).strip()
    if not name:
        raise ValueError("component is required")
    project = _root(root)
    with _connect(_db_path(project, db_path)) as db:
        removed = db.execute(
            "DELETE FROM runtime_versions WHERE component = ?", (name,)
        ).rowcount
        db.commit()
    return {"component": name, "removed": int(removed or 0)}


def verify_runtime(
    *,
    root: str | Path | None = None,
    db_path: str | Path | None = None,
    required: list[str] | tuple[str, ...] = (),
    allow_config_changes: bool = False,
) -> dict:
    project = _root(root)
    current_head = git_head(project)
    current_mtime = code_mtime(
        project, include_config=not allow_config_changes
    )
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
    selected_rows = (
        [row for row in rows if str(row["component"]) in set(required)]
        if required
        else rows
    )
    for row in selected_rows:
        name = str(row["component"])
        alive = _pid_alive(int(row["pid"]))
        status_issues = []
        if not alive:
            status_issues.append("process is not alive")
        if current_head and str(row["git_head"]) != current_head:
            status_issues.append("running git commit differs from HEAD")
        heartbeat_hash = str(row.get("heartbeat_sha256") or "")
        metadata = {}
        try:
            metadata = json.loads(row.get("metadata") or "{}")
        except (json.JSONDecodeError, TypeError):
            pass
        registered_digest = str(metadata.get(
            "code_sha256" if allow_config_changes else "runtime_sha256",
            "",
        ))
        if alive and registered_digest:
            current_digest = code_digest(
                project,
                include_config=not allow_config_changes,
            )
            if current_digest != registered_digest:
                status_issues.append("runtime code changed after process start")
        elif alive and current_mtime > float(row["code_mtime"] or 0) + 0.001:
            # Backward compatibility for registrations created before content
            # fingerprints existed. The next restart upgrades the evidence.
            status_issues.append("runtime code changed after process start")
        heartbeat_path = Path(str(metadata.get("heartbeat_path", "")))
        if heartbeat_hash and not allow_config_changes:
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
    if not rows and not required:
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
    """Exercise the full policy/state machine without interrupting the owner.

    Uses an injected no-op transport (the pipeline's supported test seam)
    instead of a real Lark send: the point is proving sanitize→dedup→
    throttle→route→state transitions work on this install, not messaging
    the owner on every deploy. The retired web channel (REQ-119) is NOT
    used — it existed only as an unconditional fake success.
    """
    project = _root(root)
    started = time.monotonic()
    pipeline = DeliveryPipeline(
        project, db_path=db_path,
        transport=lambda envelope, channel: TransportResult(
            True, "deploy-smoke"),
    )
    smoke_id = f"deploy-smoke:{int(time.time() * 1000)}:{os.getpid()}"
    result = pipeline.deliver(DeliveryEnvelope(
        source="deploy-smoke",
        kind="text",
        payload={"text": "Jarvis deploy smoke delivery"},
        attention="notice",
        requested_channel="lark",
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
    return {
        "ok": ok,
        "delivery_id": result.delivery_id,
        "state": result.state,
        "channel": result.channel,
        "elapsed_seconds": round(elapsed, 4),
        "timeout_seconds": float(timeout),
        "reason": result.reason,
    }


def _release_gate_evidence(value: dict | str | Path) -> dict:
    if isinstance(value, dict):
        return dict(value)
    path = Path(value)
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read release gate evidence: {exc}") from exc
    if not isinstance(loaded, dict):
        raise ValueError("release gate evidence must be a JSON object")
    return loaded


def _receipt_gate(gate: dict) -> dict:
    """Keep durable release proof useful without copying arbitrary text."""
    return {
        key: gate[key]
        for key in (
            "ok", "repo", "sha", "pr_head_sha", "pr", "required_checks",
            "approval_mode", "branch_protection", "evidence_source", "stale",
            "live_verified_epoch", "cache_age_seconds",
        )
        if key in gate
    } | {
        "review_actors": sorted({
            str(item.get("actor", "")).strip()
            for item in gate.get("review_evidence", [])
            if isinstance(item, dict) and str(item.get("actor", "")).strip()
        }),
        "owner_actors": sorted({
            str(item.get("actor", "")).strip()
            for item in gate.get("owner_release_decisions", [])
            if isinstance(item, dict) and str(item.get("actor", "")).strip()
        }),
    }


def record_release_receipt(
    *,
    gate_evidence: dict | str | Path,
    mode: str,
    root: str | Path | None = None,
    db_path: str | Path | None = None,
    verify_fn=None,
    component_fn=None,
    smoke_fn=None,
    now_epoch: float | None = None,
) -> dict:
    """Persist joined proof only after every post-release check succeeds."""
    project = _root(root)
    issues: list[str] = []
    try:
        gate = _release_gate_evidence(gate_evidence)
    except ValueError as exc:
        return {"ok": False, "issues": [str(exc)]}
    if mode not in {"governed", "runtime"}:
        return {"ok": False, "issues": ["invalid release mode"]}

    head = git_head(project)
    if gate.get("ok") is not True:
        issues.append("release gate did not pass")
    if str(gate.get("sha") or "") != head:
        issues.append("release gate SHA does not match HEAD")
    if issues:
        return {"ok": False, "issues": issues}

    if verify_fn is None:
        verify_fn = verify_runtime
    if component_fn is None:
        from core.components import check_components
        component_fn = check_components
    if smoke_fn is None:
        smoke_fn = smoke_delivery

    runtime = verify_fn(root=project, db_path=db_path)
    components = component_fn(critical_only=True, root=project)
    smoke = smoke_fn(root=project, db_path=db_path, timeout=3)
    if runtime.get("ok") is not True:
        issues.append("runtime verification failed")
    if not components:
        issues.append("no critical component evidence")
    elif any(item.get("ok") is not True for item in components):
        issues.append("critical component verification failed")
    if smoke.get("ok") is not True:
        issues.append("delivery smoke failed")
    if issues:
        return {
            "ok": False,
            "issues": issues,
            "evidence": {
                "gate": _receipt_gate(gate),
                "runtime": runtime,
                "components": components,
                "smoke": smoke,
            },
        }

    recorded = float(time.time() if now_epoch is None else now_epoch)
    gate_record = _receipt_gate(gate)
    with _connect(_db_path(project, db_path)) as db:
        cursor = db.execute(
            """
            INSERT INTO release_receipts (
                git_head, mode, recorded_epoch, gate_json, runtime_json,
                components_json, smoke_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                head,
                mode,
                recorded,
                json.dumps(gate_record, ensure_ascii=False, sort_keys=True),
                json.dumps(runtime, ensure_ascii=False, sort_keys=True),
                json.dumps(components, ensure_ascii=False, sort_keys=True),
                json.dumps(smoke, ensure_ascii=False, sort_keys=True),
            ),
        )
        receipt_id = int(cursor.lastrowid)
    receipt = {
        "id": receipt_id,
        "git_head": head,
        "mode": mode,
        "recorded_epoch": recorded,
        "gate": gate_record,
        "runtime": runtime,
        "components": components,
        "smoke": smoke,
    }
    return {"ok": True, "receipt": receipt}


def latest_release_receipt(
    *,
    root: str | Path | None = None,
    db_path: str | Path | None = None,
) -> dict | None:
    project = _root(root)
    with _connect(_db_path(project, db_path)) as db:
        row = db.execute(
            "SELECT * FROM release_receipts ORDER BY id DESC LIMIT 1"
        ).fetchone()
    if row is None:
        return None
    value = dict(row)
    return {
        "id": int(value["id"]),
        "git_head": str(value["git_head"]),
        "mode": str(value["mode"]),
        "recorded_epoch": float(value["recorded_epoch"]),
        "gate": json.loads(value["gate_json"]),
        "runtime": json.loads(value["runtime_json"]),
        "components": json.loads(value["components_json"]),
        "smoke": json.loads(value["smoke_json"]),
    }


def release_receipt_status(
    *,
    root: str | Path | None = None,
    db_path: str | Path | None = None,
) -> dict:
    project = _root(root)
    head = git_head(project)
    receipt = latest_release_receipt(root=project, db_path=db_path)
    issues = []
    if receipt is None:
        issues.append("no release receipt")
    elif receipt["git_head"] != head:
        issues.append("latest release receipt does not match HEAD")
    return {
        "ok": not issues,
        "git_head": head,
        "receipt": receipt,
        "issues": issues,
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

    deregister = sub.add_parser(
        "deregister",
        help="remove a RETIRED component's runtime registration",
    )
    deregister.add_argument("component", nargs="+")
    deregister.set_defaults(func=lambda args: _print({
        "ok": True,
        "removed": [
            deregister_runtime(name) for name in args.component
        ],
    }))

    verify = sub.add_parser("verify")
    verify.add_argument("--require", action="append", default=[])
    verify.add_argument(
        "--allow-config-changes",
        action="store_true",
        help="allow jarvis.yaml/HEARTBEAT.md changes while still verifying code",
    )
    verify.set_defaults(func=lambda args: _print(
        verify_runtime(
            required=args.require,
            allow_config_changes=args.allow_config_changes,
        )))

    smoke = sub.add_parser("smoke")
    smoke.add_argument("--timeout", type=float, default=3.0)
    smoke.set_defaults(func=lambda args: _print(
        smoke_delivery(timeout=args.timeout)))

    receipt = sub.add_parser(
        "receipt",
        help="verify and persist joined post-release evidence",
    )
    receipt.add_argument("--gate-evidence", required=True)
    receipt.add_argument(
        "--mode", choices=("governed", "runtime"), required=True,
    )
    receipt.set_defaults(func=lambda args: _print(
        record_release_receipt(
            gate_evidence=args.gate_evidence,
            mode=args.mode,
        )))

    latest = sub.add_parser(
        "receipt-latest",
        help="show the latest durable release receipt",
    )
    latest.set_defaults(func=lambda _args: _print(release_receipt_status()))

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
