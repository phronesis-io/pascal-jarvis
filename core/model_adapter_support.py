"""Shared private helpers for Model Runtime process adapters.

Adapters own provider process details. Route order, total deadline, replay
safety, and durable receipts stay in :mod:`core.model_runtime`.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from core.model_control import ModelRoute
from core.model_credentials import without_model_credentials


OVERFLOW_SIGNATURES = ("autocompact is thrashing", "prompt is too long")
PREEXECUTION_REASONS = {
    "account_limit",
    "auth_error",
    "rate_limited",
    "server_overloaded",
}
TRANSPORT_REASONS = {"network_error", "server_error", "timeout"}
_SECRET_RE = re.compile(
    r"(sk-[A-Za-z0-9_\-]{8,}"
    r"|Bearer\s+\S+"
    r"|\b(?:token|secret|(?:x-)?api[-_]?key|password)\b\s*[=:]\s*\S+)",
    re.IGNORECASE,
)


def provider_health_rows(root: str | Path) -> list[dict[str, Any]]:
    """Read sanitized real-request health evidence; telemetry fails open."""
    try:
        from core.provider_health import snapshot

        return list(snapshot(root).get("providers") or [])
    except Exception:
        return []


def provider_reason(error_text: str) -> str:
    lowered = str(error_text or "").lower()
    if any(signature in lowered for signature in OVERFLOW_SIGNATURES):
        return "context_overflow"
    try:
        from core.provider_health import reason_code_for_error

        return reason_code_for_error(error_text)
    except Exception:
        return "request_failed"


def safe_error_detail(error_text: str, *, summary=None) -> str:
    """Keep a bounded diagnosis without retaining provider output or secrets."""
    if summary is None:
        from core.heartbeat_provider import error_summary

        summary = error_summary
    return _SECRET_RE.sub("[redacted]", summary(error_text))[:900]


def claude_env(route: ModelRoute) -> dict[str, str]:
    if route.id == "primary":
        return without_model_credentials(
            keep=frozenset({"ANTHROPIC_API_KEY"}),
        )
    env = without_model_credentials()
    env["ANTHROPIC_AUTH_TOKEN"] = route.credential
    env["ANTHROPIC_BASE_URL"] = route.base_url
    return env


def bounded_timeout(route: ModelRoute, attempt_timeout: float) -> float:
    cap_name = ""
    default = int(max(1, attempt_timeout))
    if route.id in {"backup1", "backup2"}:
        cap_name = "CLAUDE_RELAY_ATTEMPT_TIMEOUT"
        default = 120
    elif route.id == "openai":
        cap_name = "OPENAI_FALLBACK_TIMEOUT"
        default = 120
    if not cap_name:
        return max(0.05, float(attempt_timeout))
    try:
        cap = max(1, int(os.environ.get(cap_name, str(default))))
    except (TypeError, ValueError):
        cap = default
    return max(0.05, min(float(attempt_timeout), float(cap)))
