#!/usr/bin/env python3
"""Release smoke for the repo-owned Codex plugin's stdio MCP server."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path


async def _check(root: Path) -> dict:
    from mcp import Client, StdioServerParameters

    launcher = (
        root / "plugins" / "jarvis-matters" / "scripts" / "launch-jarvis-mcp"
    )
    params = StdioServerParameters(
        command=str(launcher),
        cwd=root,
        env={**os.environ, "JARVIS_DIR": str(root)},
    )
    async with Client(params, read_timeout_seconds=10) as client:
        listed = await client.list_tools()
        names = {tool.name for tool in listed.tools}
        required = {
            "jarvis_frontstage_health",
            "jarvis_matter_review",
            "jarvis_matter_search",
            "jarvis_model_status",
        }
        missing = sorted(required - names)
        if missing:
            raise RuntimeError("missing MCP tools: " + ", ".join(missing))

        health = await client.call_tool("jarvis_frontstage_health", {})
        if health.is_error or (
            health.structured_content or {}
        ).get("schema") != "jarvis.frontstage-health.v1":
            raise RuntimeError("frontstage health request failed")

        review = await client.call_tool(
            "jarvis_matter_review", {"days": 1, "limit": 1},
        )
        if review.is_error or (
            review.structured_content or {}
        ).get("schema") != "jarvis.matter-review.v1":
            raise RuntimeError("Matter review request failed")

    return {
        "schema": "jarvis.codex-frontstage-smoke.v1",
        "healthy": True,
        "tools_verified": sorted(required),
    }


def main() -> int:
    root = Path(
        os.environ.get("JARVIS_DIR") or Path(__file__).resolve().parent.parent
    ).resolve()
    try:
        report = asyncio.run(asyncio.wait_for(_check(root), timeout=25))
    except Exception as exc:
        print(
            json.dumps({
                "schema": "jarvis.codex-frontstage-smoke.v1",
                "healthy": False,
                "error_type": type(exc).__name__,
            }),
            file=sys.stderr,
        )
        return 1
    print(json.dumps(report, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
