"""Local stdio MCP adapter for the Jarvis Matter frontstage contract."""

from __future__ import annotations

from typing import Any

from core.frontstage_acceptance import CONNECTOR_VERSION
from core.operating_model import operating_model
from core.codex_frontstage import (
    abort_matter_run,
    claim_frontstage_feedback_prompt,
    close_frontstage_matter,
    continue_matter_run,
    create_frontstage_matter,
    frontstage_health,
    matter_status,
    model_usage_status,
    review_matters,
    review_memory_claim,
    release_matter_run,
    record_frontstage_feedback,
    renew_matter_run,
    search_matters,
    search_memory,
    start_matter_run,
)


SERVER_INSTRUCTIONS = """
Jarvis is the durable backstage, not a competing chat interface. Use a Matter
only for work that must survive tasks, devices, time, or execution providers.
The owner starts ordinary questions, research, writing, coding, files and long
review in Codex. Jarvis talks first only for time, material external change,
an entrusted result, retained companion rhythm, or an owner-only boundary after
it completed reversible work. Quiet is healthy when no result is owed. Git and
GitHub own code history and review; Lark owns bounded wake-up and native work.
Acquire before doing Matter work, preserve the returned context generation and
digest, and release every run. A Result Receipt never completes the Matter.
External effects require trusted Delegation evidence; model prose is not proof.
Compiled memory is read-only unless the owner has explicitly reviewed a named
claim in the current conversation. Never infer approval or invent a reviewer.
For package usage, report numeric allowance only from exact quota evidence.
A login, configured credential, or successful canary is not remaining quota;
show unknown when a provider exposes no supported read surface.
Use the one-step continuation tool when the owner naturally asks to resume durable
work. Close only when the owner explicitly says that named Matter is complete;
pass his exact words as owner_confirmation and never infer them from executor
prose, tests, exit codes, or artifacts.
After a successfully released desktop/mobile continuation, call the feedback
prompt tool once. Show its prompt only when should_ask is true. Record feedback
only when the owner's current message is exactly one or more published labels;
never infer acceptance from silence, praise, completion evidence, or your own
assessment, and never rephrase his owner confirmation.
""".strip()


def create_server():
    from mcp.server import MCPServer
    from mcp.types import ToolAnnotations

    readonly = ToolAnnotations(
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=False,
    )
    bounded_write = ToolAnnotations(
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=False,
        open_world_hint=False,
    )

    server = MCPServer(
        "jarvis-matters",
        title="Jarvis Matters",
        description="Durable Matter continuity for Codex and other frontstages.",
        instructions=SERVER_INSTRUCTIONS,
        version=CONNECTOR_VERSION,
    )

    @server.tool(
        name="jarvis_operating_model",
        annotations=readonly,
        structured_output=True,
    )
    def get_operating_model() -> dict[str, Any]:
        """Explain when to use Codex, Jarvis, or direct Lark interaction."""
        return operating_model()

    @server.tool(
        name="jarvis_matter_search",
        annotations=readonly,
        structured_output=True,
    )
    def matter_search(
        query: str = "", status: str = "active,waiting,blocked", limit: int = 20
    ) -> dict[str, Any]:
        """Find an existing durable Matter before creating another one."""
        return search_matters(query=query, status=status, limit=limit)

    @server.tool(
        name="jarvis_matter_continue",
        annotations=bounded_write,
        structured_output=True,
    )
    def matter_continue(
        task: str,
        workspace: str,
        matter_id: str = "",
        query: str = "",
        task_ref: str = "",
        wake_id: str = "",
        model: str = "",
        surface: str = "",
        lease_seconds: int = 21600,
    ) -> dict[str, Any]:
        """Find and acquire one Matter in a single natural continuation step."""
        return continue_matter_run(
            task=task,
            workspace=workspace,
            matter_id=matter_id,
            query=query,
            task_ref=task_ref,
            wake_id=wake_id,
            model=model,
            surface=surface,
            lease_seconds=lease_seconds,
        )

    @server.tool(
        name="jarvis_matter_create",
        annotations=bounded_write,
        structured_output=True,
    )
    def matter_create(
        title: str,
        summary: str = "",
        next_action: str = "",
        kind: str = "project",
        priority: int = 5,
    ) -> dict[str, Any]:
        """Create a Matter only for explicit multi-session durable work."""
        return create_frontstage_matter(
            title=title,
            summary=summary,
            next_action=next_action,
            kind=kind,
            priority=priority,
        )

    @server.tool(
        name="jarvis_matter_status",
        annotations=readonly,
        structured_output=True,
    )
    def get_status(
        matter_id: str, include_conversation_events: bool = False,
    ) -> dict[str, Any]:
        """Read state; conversation excerpts are excluded unless requested."""
        return matter_status(
            matter_id,
            include_conversation_events=include_conversation_events,
        )

    @server.tool(
        name="jarvis_matter_start",
        annotations=bounded_write,
        structured_output=True,
    )
    def start(
        matter_id: str,
        task: str,
        workspace: str,
        task_ref: str = "",
        model: str = "",
        surface: str = "",
        lease_seconds: int = 21600,
    ) -> dict[str, Any]:
        """Acquire a fresh execution lease and bounded Context Packet."""
        return start_matter_run(
            matter_id=matter_id,
            task=task,
            workspace=workspace,
            executor="codex",
            task_ref=task_ref,
            model=model,
            surface=surface,
            lease_seconds=lease_seconds,
        )

    @server.tool(
        name="jarvis_matter_renew",
        annotations=bounded_write,
        structured_output=True,
    )
    def renew(run_id: str, lease_seconds: int = 3600) -> dict[str, Any]:
        """Renew a live Matter lease during a long Codex task."""
        return renew_matter_run(run_id, lease_seconds=lease_seconds)

    @server.tool(
        name="jarvis_matter_release",
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=False,
        ),
        structured_output=True,
    )
    def release(
        run_id: str,
        context_generation: int,
        context_digest: str,
        narrative: str = "",
        exit_code: int = 0,
        artifacts: list[str] | None = None,
        effects: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        """Release a run with verifiable artifacts/effects; not Matter closure."""
        return release_matter_run(
            run_id=run_id,
            context_generation=context_generation,
            context_digest=context_digest,
            narrative=narrative,
            exit_code=exit_code,
            artifacts=artifacts,
            effects=effects,
        )

    @server.tool(
        name="jarvis_matter_abort",
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=True,
            idempotent_hint=True,
            open_world_hint=False,
        ),
        structured_output=True,
    )
    def abort(run_id: str, error: str) -> dict[str, Any]:
        """Release a failed execution lease with a system-observed receipt."""
        return abort_matter_run(run_id, error=error)

    @server.tool(
        name="jarvis_matter_close",
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=True,
            idempotent_hint=True,
            open_world_hint=False,
        ),
        structured_output=True,
    )
    def matter_close(
        matter_id: str, outcome: str, owner_confirmation: str,
    ) -> dict[str, Any]:
        """Close a Matter only after the owner explicitly confirms completion."""
        return close_frontstage_matter(
            matter_id=matter_id,
            outcome=outcome,
            owner_confirmation=owner_confirmation,
        )

    @server.tool(
        name="jarvis_model_status",
        annotations=readonly,
        structured_output=True,
    )
    def model_status(refresh: bool = True) -> dict[str, Any]:
        """Read exact known package usage, reset times, health and fallbacks."""
        return model_usage_status(refresh=refresh)

    @server.tool(
        name="jarvis_matter_review",
        annotations=readonly,
        structured_output=True,
    )
    def matter_review(days: int = 7, limit: int = 8) -> dict[str, Any]:
        """Review confirmed Matter outcomes and the most useful next actions."""
        return review_matters(days=days, limit=limit)

    @server.tool(
        name="jarvis_acceptance_prompt",
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=False,
        ),
        structured_output=True,
    )
    def acceptance_prompt(run_id: str) -> dict[str, Any]:
        """Claim the one optional feedback prompt for a released Matter run."""
        return claim_frontstage_feedback_prompt(run_id)

    @server.tool(
        name="jarvis_acceptance_record",
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=True,
            idempotent_hint=True,
            open_world_hint=False,
        ),
        structured_output=True,
    )
    def acceptance_record(run_id: str, feedback: str) -> dict[str, Any]:
        """Record the owner's exact published feedback label for one prompted run."""
        return record_frontstage_feedback(run_id, feedback)

    @server.tool(
        name="jarvis_memory_search",
        annotations=readonly,
        structured_output=True,
    )
    def memory_search(
        query: str = "", matter_id: str | None = None,
        include_candidates: bool = False, limit: int = 20,
    ) -> dict[str, Any]:
        """Search current compiled memory; raw transcripts stay outside the prompt."""
        return search_memory(
            query=query,
            matter_id=matter_id,
            include_candidates=include_candidates,
            limit=limit,
        )

    @server.tool(
        name="jarvis_memory_review",
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=True,
            idempotent_hint=True,
            open_world_hint=False,
        ),
        structured_output=True,
    )
    def memory_review(
        claim_id: str, action: str, reviewer: str,
    ) -> dict[str, Any]:
        """Apply the owner's explicit current-conversation review to one claim."""
        return review_memory_claim(
            claim_id=claim_id, action=action, reviewer=reviewer,
        )

    @server.tool(
        name="jarvis_frontstage_health",
        annotations=readonly,
        structured_output=True,
    )
    def health() -> dict[str, Any]:
        """Inspect stale leases and result-receipt integrity."""
        return frontstage_health()

    return server


def main() -> int:
    create_server().run(transport="stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
