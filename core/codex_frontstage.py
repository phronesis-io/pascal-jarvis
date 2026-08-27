"""Application-owned Matter contract exposed to interactive agent frontstages.

Codex owns the conversation, tools, approvals and task UX. Jarvis owns durable
Matter identity, bounded context, execution leases and result evidence. This
module keeps that product boundary independent from MCP so other harnesses can
reuse the same contract without becoming a second source of truth.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

from core.matter_context import build_context_bundle, write_context_bundle
from core.matter_run_audit import audit_matter_runs
from core.matter_runs import (
    ACTIVE_STATUSES,
    abort_run,
    acquire_run,
    bind_context_packet,
    get_run,
    list_runs,
    mark_run_running,
    recover_expired_runs,
    release_run,
    renew_run,
)
from core.matters import create_matter, get_matter, link_entity, list_matters


DEFAULT_OPEN_STATUSES = "active,waiting,blocked"
_SPACE_RE = re.compile(r"\s+")


def _compact_matter(matter: dict[str, Any]) -> dict[str, Any]:
    return {
        key: matter.get(key)
        for key in (
            "id",
            "title",
            "summary",
            "next_action",
            "outcome",
            "kind",
            "status",
            "priority",
            "source",
            "created_at",
            "updated_at",
            "closed_at",
            "link_count",
            "providers",
        )
        if key in matter
    }


def _normal(value: Any) -> str:
    return _SPACE_RE.sub(" ", str(value or "").strip()).casefold()


def search_matters(
    *, query: str = "", status: str = DEFAULT_OPEN_STATUSES, limit: int = 20
) -> dict[str, Any]:
    """Return bounded Matter candidates for one interactive task."""
    query_text = _normal(query)
    candidates = list_matters(status=status or None, limit=500)
    if query_text:
        candidates = [
            item
            for item in candidates
            if query_text
            in _normal(
                " ".join(
                    str(item.get(field) or "")
                    for field in ("id", "title", "summary", "next_action")
                )
            )
        ]
    bounded = candidates[: max(1, min(int(limit), 50))]
    return {
        "schema": "jarvis.matter-search.v1",
        "query": str(query or ""),
        "status": str(status or ""),
        "count": len(bounded),
        "matters": [_compact_matter(item) for item in bounded],
    }


def create_frontstage_matter(
    *,
    title: str,
    summary: str = "",
    next_action: str = "",
    kind: str = "project",
    priority: int = 5,
    source: str = "codex",
) -> dict[str, Any]:
    """Create durable work only after the frontstage has identified a Matter.

    Exact open-title matches are returned instead of duplicated. The Codex
    skill decides whether work deserves durable identity; ordinary one-turn
    questions should remain ordinary Codex tasks.
    """
    normalized_title = _normal(title)
    if not normalized_title:
        raise ValueError("matter title is required")
    for existing in list_matters(status=DEFAULT_OPEN_STATUSES, limit=500):
        if _normal(existing.get("title")) == normalized_title:
            return {
                "schema": "jarvis.matter-create.v1",
                "created": False,
                "reason": "exact_open_title_match",
                "matter": _compact_matter(existing),
            }
    matter = create_matter(
        title=title,
        summary=summary,
        next_action=next_action,
        kind=kind,
        priority=priority,
        source=source or "codex",
        actor="frontstage",
    )
    return {
        "schema": "jarvis.matter-create.v1",
        "created": True,
        "reason": "durable_work_requested",
        "matter": _compact_matter(matter),
    }


def matter_status(matter_id: str, *, event_limit: int = 12) -> dict[str, Any]:
    """Return current Matter truth and run residue without raw transcripts."""
    matter = get_matter(matter_id)
    if matter is None:
        raise KeyError(f"matter not found: {matter_id}")
    events = list(reversed(matter.get("events", [])[: max(1, min(event_limit, 30))]))
    active = [
        run
        for run in list_runs(matter_id=matter_id, limit=20)
        if run.get("status") in ACTIVE_STATUSES
    ]
    return {
        "schema": "jarvis.matter-status.v1",
        "matter": _compact_matter(matter),
        "active_runs": active,
        "recent_events": [
            {
                "id": item.get("id"),
                "type": item.get("event_type"),
                "actor": item.get("actor"),
                "summary": item.get("summary"),
                "created_at": item.get("created_at"),
            }
            for item in events
        ],
    }


def start_matter_run(
    *,
    matter_id: str,
    task: str,
    workspace: str,
    executor: str = "codex",
    task_ref: str = "",
    model: str = "",
    surface: str = "",
    lease_seconds: int = 21600,
) -> dict[str, Any]:
    """Acquire a fresh run and return its immutable bounded Context Packet."""
    recover_expired_runs(matter_id=matter_id)
    run: dict[str, Any] | None = None
    try:
        run = acquire_run(
            matter_id,
            executor=executor,
            task=task,
            workspace=Path(workspace).expanduser(),
            surface=surface,
            lease_seconds=lease_seconds,
        )
        bundle = build_context_bundle(matter_id, run=run)
        context_path = write_context_bundle(matter_id, run=run)
        run = bind_context_packet(
            run["id"],
            packet_id=bundle["packet_id"],
            context_digest=bundle["digest"],
            context_path=context_path,
        )
        run = mark_run_running(
            run["id"], session_id=task_ref, model=model
        )
        if task_ref and executor in {"codex", "claude"}:
            link_entity(
                matter_id,
                "session",
                task_ref,
                provider=executor,
                title=f"{executor} frontstage task",
                metadata={
                    "workspace": str(Path(workspace).expanduser().resolve()),
                    "status": "running",
                },
                actor="frontstage",
            )
    except Exception as exc:
        if run and run.get("id"):
            try:
                abort_run(
                    str(run["id"]),
                    error=f"frontstage_start_failed:{type(exc).__name__}",
                )
            except Exception:
                # Preserve the start failure. Residue remains visible to the
                # lease audit and expires without stealing the real cause.
                pass
        raise
    return {
        "schema": "jarvis.frontstage-run.v1",
        "run": run,
        "context_packet": bundle,
        "context_path": str(context_path),
        "next_protocol_action": "renew_or_release",
    }


def renew_matter_run(
    run_id: str, *, lease_seconds: int = 3600
) -> dict[str, Any]:
    run = renew_run(run_id, lease_seconds=lease_seconds)
    return {
        "schema": "jarvis.frontstage-renewal.v1",
        "run_id": run["id"],
        "status": run["status"],
        "lease_expires_epoch": run["lease_expires_epoch"],
    }


def release_matter_run(
    *,
    run_id: str,
    context_generation: int,
    context_digest: str,
    narrative: str = "",
    exit_code: int = 0,
    artifacts: list[str] | None = None,
    effects: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Release one execution window; never complete the Matter implicitly."""
    return release_run(
        run_id,
        context_generation=context_generation,
        context_digest=context_digest,
        narrative=narrative,
        exit_code=exit_code,
        artifacts=artifacts or [],
        effects=effects or [],
    )


def abort_matter_run(run_id: str, *, error: str) -> dict[str, Any]:
    run = abort_run(run_id, error=error)
    return {
        "schema": "jarvis.frontstage-abort.v1",
        "run_id": run["id"],
        "matter_id": run["matter_id"],
        "status": run["status"],
        "receipt": run["receipt"],
    }


def search_memory(
    *, query: str = "", matter_id: str | None = None,
    include_candidates: bool = False, limit: int = 20,
) -> dict[str, Any]:
    """Search compiled claims without returning raw provider transcripts."""
    from core.memory_compiler import search_compiled_memory
    return search_compiled_memory(
        query,
        matter_id=matter_id,
        include_candidates=include_candidates,
        limit=limit,
    )


def review_memory_claim(
    *, claim_id: str, action: str, reviewer: str,
) -> dict[str, Any]:
    """Apply one explicit human review to a candidate or conflict."""
    from core.memory_compiler import resolve_claim
    return resolve_claim(
        claim_id, action=action, reviewer=reviewer,
    )


def model_usage_status(*, refresh: bool = True) -> dict[str, Any]:
    """Read the unified model package, health, and fallback status."""
    from core.model_usage import build_report, load_latest

    if refresh:
        return build_report()
    return load_latest()


def frontstage_health() -> dict[str, Any]:
    """Return protocol health and recoverable residue for operator review."""
    audit = audit_matter_runs(now=time.time())
    from core.frontstage_acceptance import acceptance_report
    from core.memory_compiler import compiler_status

    return {
        "schema": "jarvis.frontstage-health.v1",
        "healthy": audit["healthy"],
        "audit": audit,
        "acceptance": acceptance_report(),
        "memory_compiler": compiler_status(),
    }
