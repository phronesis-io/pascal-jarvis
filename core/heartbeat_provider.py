"""Small provider-response helpers shared by heartbeat execution paths."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Callable


_BENIGN_CLI_NOTICES = ("connectors are disabled",)


def relay_model(requested_model: str, configured_model: str,
                current_model: str) -> str:
    """Preserve an explicit cheap task tier across Claude-compatible relays."""
    requested = str(requested_model or "").strip().lower()
    if requested in {"haiku", "sonnet"}:
        return requested
    return str(configured_model or current_model)


def provider_id(use_backup: bool, backup2_active: bool) -> str:
    return "backup2" if backup2_active else ("backup1" if use_backup else "primary")


def provider_env(use_backup: bool, backup2_active: bool) -> dict[str, str] | None:
    if not use_backup:
        return None
    env = os.environ.copy()
    prefix = "CLAUDE_BACKUP2" if backup2_active else "CLAUDE_BACKUP"
    env["ANTHROPIC_AUTH_TOKEN"] = os.environ.get(f"{prefix}_AUTH_TOKEN", "")
    env["ANTHROPIC_BASE_URL"] = os.environ.get(f"{prefix}_BASE_URL", "")
    return env


def fallback_attempt_timeout(
    remaining_budget: Callable[..., int],
    call_timeout: int,
    *,
    use_backup: bool,
    backup2_active: bool,
    safe_replay: bool,
) -> int:
    """Reserve one equal wall-clock slot for every configured later route."""
    cap = call_timeout
    if use_backup and safe_replay:
        backup2 = (
            not backup2_active
            and os.environ.get("CLAUDE_BACKUP2_ENABLED", "false") == "true"
            and bool(os.environ.get("CLAUDE_BACKUP2_AUTH_TOKEN"))
            and bool(os.environ.get("CLAUDE_BACKUP2_BASE_URL"))
        )
        openai = (
            os.environ.get("OPENAI_FALLBACK_ENABLED", "true") == "true"
            and bool(os.environ.get("OPENAI_API_KEY"))
        )
        downstream = int(backup2) + int(openai)
        if downstream:
            cap = max(1, remaining_budget() // (1 + downstream))
    return remaining_budget(cap=cap)


def run_provider_attempt(
    runner: Callable[..., subprocess.CompletedProcess[str]],
    command: list[str],
    *,
    timeout: int,
    cwd: str,
    env: dict[str, str] | None,
    safe_replay: bool,
    provider: str,
    root: str | Path,
) -> tuple[subprocess.CompletedProcess[str], bool]:
    """Turn replay-safe timeouts into normal failures for the next route."""
    try:
        return runner(command, timeout=timeout, cwd=cwd, env=env), False
    except subprocess.TimeoutExpired:
        if not safe_replay:
            raise
        observe_provider(provider, "unhealthy", "timeout", root)
        return subprocess.CompletedProcess(
            command, 124, "", "request timed out"), True


def observe_provider(provider: str, status: str, detail: str,
                     root: str | Path) -> None:
    try:
        from core.provider_health import observe
        observe(provider, status, detail, root=root)
    except Exception:
        pass


def drop_benign_notices(text: str) -> str:
    """Remove CLI banners so the first retained line is the real failure."""
    if not text:
        return text
    kept = [
        line for line in text.splitlines()
        if not any(notice in line.lower() for notice in _BENIGN_CLI_NOTICES)
    ]
    cleaned = "\n".join(kept).strip()
    return cleaned or text


def openai_usage_fields(response: object) -> dict[str, int]:
    """Normalize numeric Responses API usage for the scheduler ledger."""
    if not isinstance(response, dict) or not isinstance(response.get("usage"), dict):
        return {}
    usage = response["usage"]
    fields: dict[str, int] = {}
    for source in ("input_tokens", "output_tokens"):
        value = usage.get(source)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            fields[source] = int(value)
    details = usage.get("input_tokens_details")
    cached = details.get("cached_tokens") if isinstance(details, dict) else None
    if isinstance(cached, (int, float)) and not isinstance(cached, bool):
        fields["cache_read_input_tokens"] = int(cached)
    return fields
