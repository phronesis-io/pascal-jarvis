"""Best-effort projections from authoritative Matter Run state."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from core.log import log
from core.matters import add_event, link_entity


def project_event(
    matter_id: str,
    event_type: str,
    summary: str,
    payload: dict[str, Any],
) -> bool:
    """Project an audit event without changing the committed run outcome."""
    try:
        add_event(
            matter_id,
            event_type,
            summary,
            actor="matter-runtime",
            payload=payload,
        )
        return True
    except Exception as exc:
        log(
            "matter-runtime",
            "run_event_projection_failed",
            level="error",
            matter_id=matter_id,
            event_type=event_type,
            error_type=type(exc).__name__,
        )
        return False


def project_receipt(
    *,
    run: dict[str, Any],
    receipt: dict[str, Any],
    artifacts: list[dict[str, Any]],
    effects: list[dict[str, str]],
    final_status: str,
) -> None:
    """Link receipt evidence; preserve the receipt if a projection conflicts."""
    errors = []
    try:
        if run.get("session_id"):
            session_provider = (
                run["executor"]
                if run["executor"] in {"claude", "codex"}
                else "jarvis"
            )
            link_entity(
                run["matter_id"],
                "session",
                run["session_id"],
                provider=session_provider,
                title=f"{run['executor']} run {run['run_sequence']}",
                metadata={
                    "workspace": run["workspace"],
                    "model": run.get("model", ""),
                    "status": final_status,
                },
                actor="matter-runtime",
            )
        for artifact in artifacts:
            path = Path(run["workspace"]) / artifact["path"]
            link_entity(
                run["matter_id"],
                "artifact",
                str(path),
                provider="file",
                title=artifact["path"],
                metadata={
                    "workspace": run["workspace"],
                    "exists": artifact["state"] == "present",
                    "source": run["executor"],
                    "status": (
                        "verified" if artifact["state"] == "present"
                        else "verified-deleted"
                    ),
                    "sha256": artifact["sha256"],
                    "size": artifact["size"],
                },
                actor="matter-runtime",
            )
    except Exception as exc:
        errors.append(type(exc).__name__)
        log(
            "matter-runtime",
            "receipt_projection_failed",
            level="error",
            run_id=run["id"],
            matter_id=run["matter_id"],
            error_type=type(exc).__name__,
        )
    project_event(
        run["matter_id"],
        "matter_run_released",
        "执行会话已释放；Matter 未被自动标记完成",
        {
            "run_id": run["id"],
            "receipt_id": receipt["receipt_id"],
            "receipt_digest": receipt["digest"],
            "executor": run["executor"],
            "exit_code": receipt["execution"]["exit_code"],
            "artifact_count": len(artifacts),
            "effect_count": len(effects),
            "matter_completed": False,
            "projection_errors": errors,
        },
    )
    if errors:
        project_event(
            run["matter_id"],
            "matter_run_projection_failed",
            "Result Receipt 已落库，但关联投影需要修复",
            {"run_id": run["id"], "errors": errors},
        )
