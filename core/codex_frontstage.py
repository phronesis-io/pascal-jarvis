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
    MatterRunConflict,
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
from core.matters import (
    create_matter,
    find_by_entity,
    get_matter,
    link_entity,
    list_matters,
)


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


def continuation_prompt(matter: dict[str, Any]) -> str:
    """Stable user-facing resume phrase; never depends on Codex internals."""
    return f"继续 Jarvis 事项「{matter['title']}」（{matter['id']}）"


def continue_matter_run(
    *,
    task: str,
    workspace: str,
    matter_id: str = "",
    query: str = "",
    task_ref: str = "",
    model: str = "",
    surface: str = "",
    lease_seconds: int = 21600,
) -> dict[str, Any]:
    """Resolve and acquire one Matter without exposing a multi-step protocol."""
    selected: dict[str, Any] | None = None
    candidates: list[dict[str, Any]] = []
    direct_id = str(matter_id or "").strip()
    if direct_id:
        candidate = get_matter(
            direct_id, include_links=False, include_events=False,
        )
        if candidate and candidate.get("status") not in {"done", "archived"}:
            selected = candidate
        elif candidate:
            return {
                "schema": "jarvis.matter-continuation.v1",
                "status": "closed",
                "matter": _compact_matter(candidate),
                "candidates": [],
            }
    else:
        search = search_matters(query=query)
        candidates = search["matters"]
        if len(candidates) == 1:
            selected = get_matter(
                candidates[0]["id"], include_links=False, include_events=False,
            )

    if selected is None:
        return {
            "schema": "jarvis.matter-continuation.v1",
            "status": "ambiguous" if len(candidates) > 1 else "not_found",
            "query": str(query or ""),
            "candidates": [
                {**item, "continuation_prompt": continuation_prompt(item)}
                for item in candidates
            ],
        }

    started = start_matter_run(
        matter_id=selected["id"],
        task=task,
        workspace=workspace,
        executor="codex",
        task_ref=task_ref,
        model=model,
        surface=surface,
        lease_seconds=lease_seconds,
    )
    return {
        "schema": "jarvis.matter-continuation.v1",
        "status": "started",
        "matter": _compact_matter(selected),
        "continuation_prompt": continuation_prompt(selected),
        **started,
    }


def close_frontstage_matter(
    *, matter_id: str, outcome: str, owner_confirmation: str,
) -> dict[str, Any]:
    """Close linked backstage state only from explicit owner words."""
    from core.matter_closure import close_matter

    return close_matter(
        matter_id,
        outcome=outcome,
        confirmation_text=owner_confirmation,
        source="codex",
    )


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
    session_provider = str(executor or "").strip().lower()
    session_ref = str(task_ref or "").strip()
    if session_ref and session_provider in {"codex", "claude"}:
        linked_matter = find_by_entity(
            "session", session_ref, provider=session_provider,
        )
        if (
            linked_matter
            and linked_matter.get("id") != matter_id
            and linked_matter.get("status") not in {"done", "archived"}
        ):
            raise MatterRunConflict(
                f"session is still linked to active matter "
                f"{linked_matter['id']}"
            )
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
            run["id"], session_id=session_ref, model=model
        )
        if session_ref and session_provider in {"codex", "claude"}:
            link_entity(
                matter_id,
                "session",
                session_ref,
                provider=session_provider,
                title=f"{executor} frontstage task",
                metadata={
                    "workspace": str(Path(workspace).expanduser().resolve()),
                    "status": "running",
                },
                actor="frontstage",
                move_from_terminal=True,
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


def review_matters(*, days: int = 7, limit: int = 8) -> dict[str, Any]:
    """Read recent Matter outcomes and bounded next actions."""
    from core.matter_review import build_matter_review

    return build_matter_review(days=days, limit=limit)


def claim_frontstage_feedback_prompt(run_id: str) -> dict[str, Any]:
    """Return the optional owner-feedback prompt at most once for one run."""
    from core.frontstage_acceptance import claim_acceptance_prompt

    return claim_acceptance_prompt(run_id)


def record_frontstage_feedback(run_id: str, feedback: str) -> dict[str, Any]:
    """Persist one exact owner label and return the updated migration gate."""
    from core.frontstage_acceptance import acceptance_report, record_owner_feedback

    review = record_owner_feedback(run_id, feedback)
    return {
        "schema": "jarvis.frontstage-feedback.v1",
        "review": review,
        "acceptance": acceptance_report(),
    }


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
