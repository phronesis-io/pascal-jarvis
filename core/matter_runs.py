"""Durable acquire/run/release protocol for provider execution sessions.

Matter is the product-level identity. Claude Code and Codex sessions are
replaceable execution windows. This module makes their ownership, context and
result evidence explicit without granting a model authority to finish a
Matter or self-attest an external side effect.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

from core.conversation_context import current_context_generation
from core.matter_run_evidence import (
    EvidenceValidationError,
    verify_artifacts,
    verify_effects,
)
from core.matter_run_projection import project_event, project_receipt
from core.matters import get_matter


ACTIVE_STATUSES = {"acquired", "running"}
TERMINAL_STATUSES = {"released", "failed", "expired"}
_EXECUTOR_RE = re.compile(r"^[a-z0-9_.-]{1,80}$")
VALID_SURFACES = {"", "desktop", "mobile", "lark", "api"}


class MatterRunError(RuntimeError):
    """Base error for the Matter execution protocol."""


class MatterRunConflict(MatterRunError):
    """A lease, generation or immutable receipt conflicts with this action."""


class MatterRunValidationError(MatterRunError):
    """Submitted result evidence cannot be verified safely."""


def _db():
    from core.db import get_db
    return get_db()


def _canonical(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _object(value: Any) -> dict[str, Any]:
    if value in (None, ""):
        return {}
    if not isinstance(value, dict):
        raise MatterRunValidationError("value must be an object")
    return dict(value)


def _decode(value: str | None) -> dict[str, Any]:
    try:
        decoded = json.loads(value or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _row(value: sqlite3.Row | None) -> dict[str, Any] | None:
    if value is None:
        return None
    result = dict(value)
    result["authority"] = _decode(result.pop("authority_json", "{}"))
    result["receipt"] = _decode(result.pop("receipt_json", "{}"))
    return result


def _text(value: Any, field: str, limit: int) -> str:
    result = str(value or "").strip()
    if len(result) > limit:
        raise MatterRunValidationError(f"{field} exceeds {limit} characters")
    return result


def default_authority(executor: str, workspace: str | Path) -> dict[str, Any]:
    """Return the non-escalatable authority granted to one execution window."""
    return {
        "executor": executor,
        "workspace": str(Path(workspace).expanduser().resolve()),
        "may_modify_workspace": True,
        "may_complete_matter": False,
        "may_self_attest_external_effects": False,
        "external_effect_evidence": "required",
    }


def _validate_executor(executor: str) -> str:
    value = str(executor or "").strip().lower()
    if not _EXECUTOR_RE.fullmatch(value):
        raise MatterRunValidationError("executor contains unsafe characters")
    return value


def _workspace_path(workspace: str | Path) -> Path:
    path = Path(workspace).expanduser().resolve()
    if not path.is_dir():
        raise MatterRunValidationError("workspace must be an existing directory")
    return path


def get_run(run_id: str) -> dict[str, Any] | None:
    row = _db().execute("SELECT * FROM matter_runs WHERE id = ?", (str(run_id),)).fetchone()
    return _row(row)


def list_runs(
    matter_id: str = "", status: str = "", limit: int = 100
) -> list[dict[str, Any]]:
    where, params = [], []
    if matter_id:
        where.append("matter_id = ?")
        params.append(str(matter_id))
    if status:
        where.append("status = ?")
        params.append(str(status))
    clause = " WHERE " + " AND ".join(where) if where else ""
    params.append(max(1, min(int(limit), 500)))
    rows = _db().execute(
        f"SELECT * FROM matter_runs{clause} ORDER BY acquired_epoch DESC LIMIT ?",
        params,
    ).fetchall()
    return [_row(row) or {} for row in rows]


def recover_expired_runs(
    *, matter_id: str = "", now: float | None = None
) -> list[str]:
    """Release expired ownership so another provider can acquire the Matter."""
    epoch = float(time.time() if now is None else now)
    db = _db()
    params: list[Any] = [epoch, epoch]
    matter_clause = ""
    if matter_id:
        matter_clause = " AND matter_id = ?"
        params.append(str(matter_id))
    try:
        db.execute("BEGIN IMMEDIATE")
        rows = db.execute(
            "SELECT id,matter_id FROM matter_runs "
            "WHERE status IN ('acquired','running') AND lease_expires_epoch <= ?"
            + matter_clause,
            [epoch, *([str(matter_id)] if matter_id else [])],
        ).fetchall()
        db.execute(
            "UPDATE matter_runs SET status='expired',released_epoch=?,"
            "last_error='lease_expired' WHERE status IN ('acquired','running') "
            "AND lease_expires_epoch <= ?" + matter_clause,
            params,
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    for row in rows:
        project_event(
            str(row["matter_id"]),
            "matter_run_expired",
            "执行租约到期，已释放给后续会话",
            {"run_id": str(row["id"])},
        )
    return [str(row["id"]) for row in rows]


def acquire_run(
    matter_id: str,
    *,
    executor: str,
    task: str = "",
    workspace: str | Path = ".",
    authority: dict[str, Any] | None = None,
    surface: str = "",
    lease_seconds: int = 3600,
    now: float | None = None,
) -> dict[str, Any]:
    """Atomically acquire the only live execution lease for one Matter."""
    matter = get_matter(matter_id, include_links=False, include_events=False)
    if matter is None:
        raise KeyError(f"matter not found: {matter_id}")
    if matter.get("status") in {"done", "archived"}:
        raise MatterRunConflict("terminal Matter cannot be acquired")
    executor = _validate_executor(executor)
    workspace_path = _workspace_path(workspace)
    task = _text(task, "task", 4000)
    surface = str(surface or "").strip().lower()
    if surface not in VALID_SURFACES:
        raise MatterRunValidationError("invalid frontstage surface")
    lease_seconds = max(30, min(int(lease_seconds), 86400))
    epoch = float(time.time() if now is None else now)
    generation = current_context_generation(f"matter:{matter_id}")
    granted = default_authority(executor, workspace_path)
    requested = _object(authority)
    unsupported = set(requested) - {
        "may_modify_workspace",
        "allowed_tools",
        "may_complete_matter",
        "may_self_attest_external_effects",
    }
    if unsupported:
        raise MatterRunValidationError(
            "unsupported authority fields: " + ",".join(sorted(unsupported))
        )
    for key in ("may_complete_matter", "may_self_attest_external_effects"):
        if requested.get(key):
            raise MatterRunValidationError(f"{key} cannot be granted to an executor")
    if "may_modify_workspace" in requested and not isinstance(
        requested["may_modify_workspace"], bool
    ):
        raise MatterRunValidationError("may_modify_workspace must be boolean")
    if "allowed_tools" in requested:
        tools = requested["allowed_tools"]
        if (
            not isinstance(tools, list)
            or len(tools) > 64
            or any(not _EXECUTOR_RE.fullmatch(str(tool)) for tool in tools)
        ):
            raise MatterRunValidationError("allowed_tools must be a safe bounded list")
    granted.update(requested)
    granted["may_complete_matter"] = False
    granted["may_self_attest_external_effects"] = False
    run_id = f"mrun_{uuid.uuid4().hex[:20]}"
    db = _db()
    expired: list[sqlite3.Row] = []
    try:
        db.execute("BEGIN IMMEDIATE")
        expired = db.execute(
            "SELECT id,matter_id FROM matter_runs "
            "WHERE matter_id=? AND status IN ('acquired','running') "
            "AND lease_expires_epoch <= ?",
            (matter_id, epoch),
        ).fetchall()
        db.execute(
            "UPDATE matter_runs SET status='expired',released_epoch=?,"
            "last_error='lease_expired' WHERE matter_id=? "
            "AND status IN ('acquired','running') AND lease_expires_epoch <= ?",
            (epoch, matter_id, epoch),
        )
        active = db.execute(
            "SELECT id,executor,lease_expires_epoch FROM matter_runs "
            "WHERE matter_id=? AND status IN ('acquired','running')",
            (matter_id,),
        ).fetchone()
        if active is not None:
            raise MatterRunConflict(
                f"Matter has an active run: {active['id']} ({active['executor']})"
            )
        row = db.execute(
            "SELECT COALESCE(MAX(run_sequence), 0) + 1 FROM matter_runs "
            "WHERE matter_id=?",
            (matter_id,),
        ).fetchone()
        sequence = int(row[0])
        db.execute(
            """INSERT INTO matter_runs(
                   id,matter_id,executor,status,run_sequence,task,workspace,
                   context_generation,authority_json,surface,acquired_epoch,
                   lease_expires_epoch
               ) VALUES (?,?,?,'acquired',?,?,?,?,?,?,?,?)""",
            (
                run_id,
                matter_id,
                executor,
                sequence,
                task,
                str(workspace_path),
                generation,
                _canonical(granted),
                surface,
                epoch,
                epoch + lease_seconds,
            ),
        )
        db.commit()
    except MatterRunConflict:
        db.rollback()
        raise
    except sqlite3.IntegrityError as exc:
        db.rollback()
        raise MatterRunConflict("Matter has an active run") from exc
    except Exception:
        db.rollback()
        raise
    for old in expired:
        project_event(
            matter_id,
            "matter_run_expired",
            "执行租约到期，已释放给后续会话",
            {"run_id": str(old["id"])},
        )
    project_event(
        matter_id,
        "matter_run_acquired",
        f"{executor} 已取得第 {sequence} 次执行租约",
        {
            "run_id": run_id,
            "executor": executor,
            "run_sequence": sequence,
            "context_generation": generation,
            "lease_expires_epoch": epoch + lease_seconds,
        },
    )
    return get_run(run_id) or {}


def bind_context_packet(
    run_id: str,
    *,
    packet_id: str,
    context_digest: str,
    context_path: str | Path,
    now: float | None = None,
) -> dict[str, Any]:
    """Bind one immutable Context Packet to a live run."""
    packet_id = _text(packet_id, "packet_id", 120)
    context_digest = _text(context_digest, "context_digest", 100)
    packet_path = Path(context_path).expanduser().resolve()
    path = str(packet_path)
    if not packet_id.startswith("ctx_") or not context_digest.startswith("sha256:"):
        raise MatterRunValidationError("invalid context packet identity")
    existing_run = get_run(run_id)
    if existing_run is None:
        raise KeyError(f"matter run not found: {run_id}")
    existing_identity = (
        str(existing_run.get("context_packet_id") or ""),
        str(existing_run.get("context_digest") or ""),
    )
    if any(existing_identity) and existing_identity != (packet_id, context_digest):
        raise MatterRunConflict("run is already bound to a different context packet")
    try:
        stored = json.loads(packet_path.with_suffix(".json").read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise MatterRunValidationError("Context Packet files are not readable") from exc
    body = dict(stored)
    body.pop("packet_id", None)
    body.pop("digest", None)
    calculated = _digest(body)
    from core.matter_context import render_context_markdown
    expected_markdown = render_context_markdown(stored)
    if (
        not packet_path.is_file()
        or packet_path.read_text(encoding="utf-8") != expected_markdown
        or stored.get("packet_id") != packet_id
        or stored.get("digest") != context_digest
        or calculated != context_digest
        or packet_id != f"ctx_{calculated.split(':', 1)[1][:20]}"
    ):
        raise MatterRunValidationError("Context Packet files do not match the identity")
    epoch = float(time.time() if now is None else now)
    db = _db()
    try:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM matter_runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(f"matter run not found: {run_id}")
        if row["status"] not in ACTIVE_STATUSES:
            raise MatterRunConflict("context cannot be bound to a terminal run")
        if float(row["lease_expires_epoch"]) <= epoch:
            raise MatterRunConflict("run lease has expired")
        existing = (str(row["context_packet_id"]), str(row["context_digest"]))
        if any(existing) and existing != (packet_id, context_digest):
            raise MatterRunConflict("run is already bound to a different context packet")
        db.execute(
            "UPDATE matter_runs SET context_packet_id=?,context_digest=?,"
            "context_path=? WHERE id=?",
            (packet_id, context_digest, path, run_id),
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    return get_run(run_id) or {}


def mark_run_running(
    run_id: str,
    *,
    session_id: str = "",
    model: str = "",
    now: float | None = None,
) -> dict[str, Any]:
    epoch = float(time.time() if now is None else now)
    db = _db()
    try:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM matter_runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(f"matter run not found: {run_id}")
        if row["status"] not in ACTIVE_STATUSES:
            raise MatterRunConflict("terminal run cannot be started")
        if float(row["lease_expires_epoch"]) <= epoch:
            raise MatterRunConflict("run lease has expired")
        if not row["context_digest"]:
            raise MatterRunConflict("run has no bound Context Packet")
        db.execute(
            "UPDATE matter_runs SET status='running',started_epoch=COALESCE("
            "started_epoch,?),session_id=?,model=? WHERE id=?",
            (
                epoch,
                _text(session_id, "session_id", 500),
                _text(model, "model", 200),
                run_id,
            ),
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    return get_run(run_id) or {}


def renew_run(
    run_id: str, *, lease_seconds: int = 3600, now: float | None = None
) -> dict[str, Any]:
    """Extend a live run without reviving an expired or terminal lease."""
    epoch = float(time.time() if now is None else now)
    lease_seconds = max(30, min(int(lease_seconds), 86400))
    db = _db()
    try:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM matter_runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(f"matter run not found: {run_id}")
        if row["status"] not in ACTIVE_STATUSES:
            raise MatterRunConflict("terminal run cannot be renewed")
        if float(row["lease_expires_epoch"]) <= epoch:
            raise MatterRunConflict("run lease has expired")
        db.execute(
            "UPDATE matter_runs SET lease_expires_epoch=MAX(lease_expires_epoch, ?) "
            "WHERE id=?",
            (epoch + lease_seconds, run_id),
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    return get_run(run_id) or {}


def abort_run(
    run_id: str, *, error: str, exit_code: int = 1, now: float | None = None
) -> dict[str, Any]:
    """Release a run that failed before a Result Receipt could be produced."""
    epoch = float(time.time() if now is None else now)
    message = _text(error, "error", 1000) or "execution_aborted"
    db = _db()
    try:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM matter_runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(f"matter run not found: {run_id}")
        if row["status"] in TERMINAL_STATUSES:
            db.rollback()
            return _row(row) or {}
        receipt = {
            "schema": "jarvis.result-receipt.v1",
            "receipt_id": f"rr_{uuid.uuid4().hex[:20]}",
            "run_id": run_id,
            "matter_id": str(row["matter_id"]),
            "executor": str(row["executor"]),
            "run_sequence": int(row["run_sequence"]),
            "context_packet_id": str(row["context_packet_id"]),
            "context_generation": int(row["context_generation"]),
            "context_digest": str(row["context_digest"]),
            "released_epoch": epoch,
            "session": {
                "id": str(row["session_id"]),
                "model": str(row["model"]),
            },
            "execution": {"outcome": "aborted", "exit_code": int(exit_code)},
            "narrative": message,
            "narrative_trust": "system_observation",
            "artifacts": [],
            "effects": [],
            "matter_completed": False,
            "completion_boundary": "separate_verified_matter_transition_required",
        }
        receipt["submission_digest"] = _digest({
            "run_id": run_id,
            "error": message,
            "exit_code": int(exit_code),
        })
        receipt["digest"] = _digest(receipt)
        db.execute(
            "UPDATE matter_runs SET status='failed',released_epoch=?,exit_code=?,"
            "last_error=?,result_digest=?,receipt_json=? WHERE id=?",
            (
                epoch,
                int(exit_code),
                message,
                receipt["digest"],
                _canonical(receipt),
                run_id,
            ),
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    run = get_run(run_id) or {}
    project_event(
        str(run.get("matter_id") or row["matter_id"]),
        "matter_run_aborted",
        "执行窗口异常退出，租约已释放",
        {"run_id": run_id, "error": message, "exit_code": int(exit_code)},
    )
    return run


def release_run(
    run_id: str,
    *,
    context_generation: int,
    context_digest: str,
    narrative: str = "",
    exit_code: int = 0,
    artifacts: list[str] | None = None,
    effects: list[dict[str, str]] | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Release a run with verified artifacts and referenced external evidence."""
    epoch = float(time.time() if now is None else now)
    run = get_run(run_id)
    if run is None:
        raise KeyError(f"matter run not found: {run_id}")
    if int(context_generation) != int(run["context_generation"]):
        raise MatterRunConflict("submitted context generation does not match the run")
    live_generation = current_context_generation(f"matter:{run['matter_id']}")
    if live_generation != int(run["context_generation"]):
        raise MatterRunConflict("logical context generation changed during the run")
    if str(context_digest) != str(run["context_digest"]):
        raise MatterRunConflict("submitted Context Packet digest does not match the run")
    try:
        verified_artifacts = verify_artifacts(run["workspace"], artifacts)
        verified_effects = verify_effects(
            workspace=run["workspace"],
            matter_id=run["matter_id"],
            effects=effects,
            epoch=epoch,
        )
    except EvidenceValidationError as exc:
        raise MatterRunValidationError(str(exc)) from exc
    submission = {
        "run_id": run_id,
        "context_generation": int(context_generation),
        "context_digest": str(context_digest),
        "narrative": _text(narrative, "narrative", 4000),
        "exit_code": int(exit_code),
        "artifacts": verified_artifacts,
        "effects": verified_effects,
    }
    submission_digest = _digest(submission)
    existing_receipt = run.get("receipt") or {}
    if run["status"] in TERMINAL_STATUSES:
        if existing_receipt.get("submission_digest") == submission_digest:
            return existing_receipt
        raise MatterRunConflict("run already has a different receipt")
    if float(run["lease_expires_epoch"]) <= epoch:
        raise MatterRunConflict("run lease has expired")
    outcome = "exited" if int(exit_code) == 0 else "failed"
    matter = get_matter(
        str(run["matter_id"]), include_links=False, include_events=False
    ) or {}
    receipt = {
        "schema": "jarvis.result-receipt.v1",
        "receipt_id": f"rr_{submission_digest.split(':', 1)[1][:20]}",
        "run_id": run_id,
        "matter_id": run["matter_id"],
        "executor": run["executor"],
        "run_sequence": int(run["run_sequence"]),
        "context_packet_id": run["context_packet_id"],
        "context_generation": int(run["context_generation"]),
        "context_digest": run["context_digest"],
        "released_epoch": epoch,
        "session": {"id": run.get("session_id", ""), "model": run.get("model", "")},
        "execution": {"outcome": outcome, "exit_code": int(exit_code)},
        "narrative": submission["narrative"],
        "narrative_trust": "unverified_model_report",
        "artifacts": verified_artifacts,
        "effects": verified_effects,
        "matter_state_at_release": {
            "status": str(matter.get("status") or ""),
            "next_action": _text(matter.get("next_action", ""), "next_action", 1600),
            "updated_at": str(matter.get("updated_at") or ""),
        },
        "matter_completed": False,
        "completion_boundary": "separate_verified_matter_transition_required",
        "submission_digest": submission_digest,
    }
    receipt["digest"] = _digest(receipt)
    final_status = "released" if int(exit_code) == 0 else "failed"
    db = _db()
    try:
        db.execute("BEGIN IMMEDIATE")
        current = db.execute("SELECT * FROM matter_runs WHERE id=?", (run_id,)).fetchone()
        if current is None:
            raise KeyError(f"matter run not found: {run_id}")
        if current["status"] in TERMINAL_STATUSES:
            stored = _decode(current["receipt_json"])
            if stored.get("submission_digest") == submission_digest:
                db.rollback()
                return stored
            raise MatterRunConflict("run already has a different receipt")
        if float(current["lease_expires_epoch"]) <= epoch:
            raise MatterRunConflict("run lease has expired")
        db.execute(
            "UPDATE matter_runs SET status=?,released_epoch=?,exit_code=?,"
            "result_digest=?,receipt_json=?,last_error=? WHERE id=?",
            (
                final_status,
                epoch,
                int(exit_code),
                receipt["digest"],
                _canonical(receipt),
                "" if int(exit_code) == 0 else f"executor_exit_{int(exit_code)}",
                run_id,
            ),
        )
        db.commit()
    except Exception:
        db.rollback()
        raise

    project_receipt(
        run=run,
        receipt=receipt,
        artifacts=verified_artifacts,
        effects=verified_effects,
        final_status=final_status,
    )
    return receipt
