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


def isolated_failure_scope(is_heavy: bool, context_overflow: bool,
                           timed_out: bool, error: str) -> str:
    """Classify an isolated model failure without poisoning task circuits."""
    if is_heavy or context_overflow or not (timed_out or error):
        return "task"
    return "provider"


def record_isolated_failure(circuit, is_heavy: bool, context_overflow: bool,
                            timed_out: bool, error: str) -> tuple[bool, str]:
    """Charge only task-owned failures to a task's circuit breaker."""
    scope = isolated_failure_scope(
        is_heavy, context_overflow, timed_out, error,
    )
    return (circuit.record_failure() if scope == "task" else False), scope


def fallback_attempt_timeout(
    remaining_budget: Callable[..., int],
    call_timeout: int,
    *,
    use_backup: bool,
    backup2_active: bool,
    safe_replay: bool,
) -> int:
    """Bound one attempt while preserving later routes inside one deadline."""

    def configured(prefix: str, *, default_enabled: str) -> bool:
        return (
            os.environ.get(f"{prefix}_ENABLED", default_enabled) == "true"
            and bool(os.environ.get(f"{prefix}_AUTH_TOKEN"))
            and bool(os.environ.get(f"{prefix}_BASE_URL"))
        )

    def bounded_env(name: str, default: int) -> int:
        try:
            value = int(os.environ.get(name, str(default)))
        except (TypeError, ValueError):
            value = default
        return min(call_timeout, max(1, value))

    remaining = remaining_budget()
    if remaining <= 0:
        return 0

    relay_cap = bounded_env("CLAUDE_RELAY_ATTEMPT_TIMEOUT", 120)
    current_cap = min(remaining, relay_cap if use_backup else call_timeout)
    if not safe_replay:
        # An ambiguous tool-capable timeout still fails closed. Relays are
        # capped so a degraded intermediary cannot hold the scheduler for the
        # full task envelope before that safe failure is recorded.
        return remaining_budget(cap=current_cap)

    downstream_caps: list[int] = []
    if (not use_backup
            and configured("CLAUDE_BACKUP", default_enabled="true")):
        downstream_caps.append(relay_cap)
    if (not backup2_active
            and configured("CLAUDE_BACKUP2", default_enabled="false")):
        downstream_caps.append(relay_cap)
    if (os.environ.get("OPENAI_FALLBACK_ENABLED", "true") == "true"
            and bool(os.environ.get("OPENAI_API_KEY"))):
        downstream_caps.append(
            bounded_env("OPENAI_FALLBACK_TIMEOUT", 120)
        )

    if downstream_caps:
        # Fixed downstream caps are ideal for the normal 600s envelope. For a
        # smaller caller budget, shrink every slot fairly instead of reserving
        # more time than the logical call owns.
        fair_cap = max(1, remaining // (1 + len(downstream_caps)))
        reserved = sum(min(cap, fair_cap) for cap in downstream_caps)
        current_cap = min(current_cap, max(1, remaining - reserved))
    return remaining_budget(cap=current_cap)


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
