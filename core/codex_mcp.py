"""Local stdio MCP adapter for the Jarvis Matter frontstage contract."""

from __future__ import annotations

from typing import Any

from core.codex_frontstage import (
    abort_matter_run,
    create_frontstage_matter,
    frontstage_health,
    matter_status,
    release_matter_run,
    renew_matter_run,
    search_matters,
    start_matter_run,
)


SERVER_INSTRUCTIONS = """
Jarvis is the durable backstage, not a competing chat interface. Use a Matter
only for work that must survive tasks, devices, time, or execution providers.
Acquire before doing Matter work, preserve the returned context generation and
digest, and release every run. A Result Receipt never completes the Matter.
External effects require trusted Delegation evidence; model prose is not proof.
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
        version="0.1.0",
    )

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
    def get_status(matter_id: str) -> dict[str, Any]:
        """Read current Matter state, active lease and recent audit events."""
        return matter_status(matter_id)

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
