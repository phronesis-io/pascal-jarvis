"""Sanitized model catalog and route policy for every Jarvis harness.

This module decides *which* configured route is eligible.  It never starts a
model process: Claude CLI, Codex CLI, and OpenAI Responses remain execution
adapters with their own process, tool, and receipt boundaries.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

from core.codex_fallback import resolve_codex_bin
from core.config import Config


ROUTE_IDS = ("primary", "backup1", "backup2", "codex", "openai")
CONTEXTS = {
    "owner_chat",
    "group",
    "heartbeat",
    "auxiliary_trusted",
    "auxiliary_untrusted",
}
DEFAULT_ORDERS = {
    "owner_chat": ROUTE_IDS,
    "group": ("primary", "backup1", "backup2", "openai"),
    "heartbeat": ("primary", "backup1", "backup2", "openai"),
    "auxiliary_trusted": ("primary", "backup1", "backup2", "openai"),
    "auxiliary_untrusted": ("primary", "backup1", "backup2", "openai"),
}
TOOL_CONTEXTS = {"owner_chat", "heartbeat", "auxiliary_trusted"}
_PRIMARY_RECOVERY_REASONS = {
    "network_error",
    "rate_limited",
    "server_error",
    "server_overloaded",
}


def _bool(value: object, default: bool = False) -> bool:
    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() == "true"


def _setting(
    env: Mapping[str, str], name: str, configured: object = "",
) -> str:
    value = env.get(name)
    return str(configured or "") if value is None else str(value)


def _host(value: str, fallback: str) -> str:
    hostname = str(urlparse(str(value or "")).hostname or "").lower()
    if hostname.startswith("www."):
        hostname = hostname[4:]
    return hostname or fallback


def _upstream_name(configured: object, endpoint: str, fallback: str) -> str:
    """Return a stable diagnostic label without echoing credentials or URLs."""
    value = str(configured or "").strip()
    if not value:
        return _host(endpoint, fallback)
    parsed = urlparse(value)
    if parsed.hostname:
        return _host(value, fallback)
    if re.fullmatch(r"[A-Za-z0-9._-]{1,80}", value):
        return value.lower()
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:10]
    return f"custom-{digest}"


def _model_family(model: str) -> str:
    lowered = str(model or "").lower()
    if any(name in lowered for name in ("gpt", "o1", "o3", "o4")):
        return "gpt"
    if any(name in lowered for name in ("claude", "opus", "sonnet", "haiku")):
        return "claude"
    return "other"


@dataclass(frozen=True)
class ModelRoute:
    id: str
    label: str
    adapter: str
    model: str
    upstream: str
    enabled: bool
    configured: bool
    capabilities: frozenset[str]
    base_url: str = ""
    binary: str = ""
    credential: str = field(default="", repr=False)
    user_agent: str = ""

    @property
    def model_family(self) -> str:
        return _model_family(self.model)

    def public(self) -> dict[str, Any]:
        """Return the complete non-secret route contract."""
        return {
            "id": self.id,
            "label": self.label,
            "adapter": self.adapter,
            "model": self.model,
            "model_family": self.model_family,
            "upstream": self.upstream,
            "enabled": self.enabled,
            "configured": self.configured,
            "capabilities": sorted(self.capabilities),
        }

    def probe_spec(self) -> dict[str, Any]:
        """Private adapter input used only inside a bounded health probe."""
        kind = {
            "claude_cli": "claude",
            "codex_cli": "codex",
            "openai_responses": "openai",
        }[self.adapter]
        spec = {
            "id": self.id,
            "label": self.label,
            "kind": kind,
            "enabled": self.enabled,
            "configured": self.configured,
            "model": self.model,
        }
        if self.base_url:
            spec["base_url"] = self.base_url
        if self.credential:
            spec["token"] = self.credential
        if self.binary:
            spec["binary"] = self.binary
        if self.user_agent:
            spec["user_agent"] = self.user_agent
        return spec


@dataclass(frozen=True)
class RoutePlan:
    context: str
    preference: str
    allow_tools: bool
    routes: tuple[ModelRoute, ...]
    skipped: dict[str, str]

    def public(self) -> dict[str, Any]:
        return {
            "context": self.context,
            "preference": self.preference,
            "allow_tools": self.allow_tools,
            "routes": [route.public() for route in self.routes],
            "skipped": dict(self.skipped),
        }


def model_routes(
    config: Config | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> tuple[ModelRoute, ...]:
    """Build the canonical route catalog from private config and environment."""
    config = config or Config()
    env = os.environ if env is None else env
    claude = config.claude
    codex = config.codex
    openai = config.openai
    main_model = str(claude.get("main_model") or "opus")

    backup_token = _setting(
        env, "CLAUDE_BACKUP_AUTH_TOKEN", claude.get("backup_auth_token")
    )
    backup_url = _setting(
        env, "CLAUDE_BACKUP_BASE_URL", claude.get("backup_base_url")
    )
    backup2_token = _setting(
        env, "CLAUDE_BACKUP2_AUTH_TOKEN", claude.get("backup2_auth_token")
    )
    backup2_url = _setting(
        env, "CLAUDE_BACKUP2_BASE_URL", claude.get("backup2_base_url")
    )
    codex_binary = _setting(
        env,
        "CODEX_FALLBACK_BINARY",
        env.get("CODEX_BIN") or codex.get("binary"),
    )
    openai_token = _setting(env, "OPENAI_API_KEY", openai.get("api_key"))
    openai_url = _setting(
        env,
        "OPENAI_BASE_URL",
        openai.get("base_url") or "https://api.openai.com/v1",
    )

    return (
        ModelRoute(
            id="primary",
            label="Claude primary",
            adapter="claude_cli",
            model=main_model,
            upstream=_upstream_name(
                claude.get("primary_upstream"), "", "claude-account"
            ),
            enabled=True,
            configured=True,
            capabilities=frozenset({"text", "tools", "session"}),
        ),
        ModelRoute(
            id="backup1",
            label="Claude backup",
            adapter="claude_cli",
            model=_setting(
                env,
                "CLAUDE_BACKUP_MODEL",
                claude.get("backup_model") or main_model,
            ),
            upstream=_upstream_name(
                claude.get("backup_upstream"), backup_url, "backup1"
            ),
            enabled=_bool(
                env.get("CLAUDE_BACKUP_ENABLED"),
                _bool(claude.get("backup_enabled"), True),
            ),
            configured=bool(backup_token and backup_url),
            capabilities=frozenset({"text", "tools", "session"}),
            base_url=backup_url,
            credential=backup_token,
        ),
        ModelRoute(
            id="backup2",
            label="Claude backup2",
            adapter="claude_cli",
            model=_setting(
                env,
                "CLAUDE_BACKUP2_MODEL",
                claude.get("backup2_model") or main_model,
            ),
            upstream=_upstream_name(
                claude.get("backup2_upstream"), backup2_url, "backup2"
            ),
            enabled=_bool(
                env.get("CLAUDE_BACKUP2_ENABLED"),
                _bool(claude.get("backup2_enabled"), False),
            ),
            configured=bool(backup2_token and backup2_url),
            capabilities=frozenset({"text", "tools", "session"}),
            base_url=backup2_url,
            credential=backup2_token,
        ),
        ModelRoute(
            id="codex",
            label="Codex fallback",
            adapter="codex_cli",
            model=_setting(
                env,
                "CODEX_FALLBACK_MODEL",
                codex.get("fallback_model") or "gpt-5.5",
            ),
            upstream=_upstream_name(
                codex.get("upstream"), "", "chatgpt-account"
            ),
            enabled=_bool(
                env.get("CODEX_FALLBACK_ENABLED"),
                _bool(codex.get("fallback_enabled"), True),
            ),
            configured=bool(codex_binary or resolve_codex_bin()),
            capabilities=frozenset({"text", "tools", "session", "workspace"}),
            binary=codex_binary,
        ),
        ModelRoute(
            id="openai",
            label="GPT fallback",
            adapter="openai_responses",
            model=_setting(
                env,
                "OPENAI_FALLBACK_MODEL",
                env.get("OPENAI_MODEL")
                or openai.get("fallback_model")
                or "gpt-5.5",
            ),
            upstream=_upstream_name(
                openai.get("upstream"), openai_url, "openai"
            ),
            enabled=_bool(
                env.get("OPENAI_FALLBACK_ENABLED"),
                _bool(openai.get("fallback_enabled"), True),
            ),
            configured=bool(openai_token),
            capabilities=frozenset({"text", "tools"}),
            base_url=openai_url,
            credential=openai_token,
            user_agent=_setting(
                env, "OPENAI_USER_AGENT", openai.get("user_agent")
            ),
        ),
    )


def _checked_epoch(row: Mapping[str, Any]) -> float:
    try:
        value = float(row.get("checked_epoch") or 0)
        if value > 0:
            return value
    except (TypeError, ValueError):
        pass
    try:
        return datetime.fromisoformat(
            str(row.get("checked_at") or "").replace("Z", "+00:00")
        ).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _in_health_cooldown(
    row: Mapping[str, Any],
    *,
    env: Mapping[str, str],
    now_epoch: float,
) -> bool:
    if row.get("status") != "unhealthy":
        return False
    try:
        explicit_until = float(row.get("cooldown_until_epoch") or 0)
    except (TypeError, ValueError):
        explicit_until = 0
    if explicit_until:
        return now_epoch < explicit_until
    cooldown = max(0, int(env.get(
        "JARVIS_PROVIDER_UNHEALTHY_COOLDOWN_SECONDS", "1800"
    )))
    if row.get("observation_source") == "real_request":
        reason = str(row.get("detail") or "").removeprefix("real request: ")
        if reason == "network_error":
            cooldown = min(cooldown, max(0, int(env.get(
                "JARVIS_PROVIDER_TRANSIENT_COOLDOWN_SECONDS", "60"
            ))))
        elif reason == "timeout":
            cooldown = max(cooldown, max(0, int(env.get(
                "JARVIS_PROVIDER_TIMEOUT_COOLDOWN_SECONDS", "1800"
            ))))
        elif reason == "rate_limited":
            cooldown = min(cooldown, max(0, int(env.get(
                "JARVIS_PROVIDER_RATE_LIMIT_COOLDOWN_SECONDS", "300"
            ))))
    return now_epoch - _checked_epoch(row) < cooldown


def _real_failure_reason(row: Mapping[str, Any]) -> str:
    if row.get("observation_source") != "real_request":
        return ""
    return str(row.get("detail") or "").removeprefix("real request: ")


def route_plan(
    context: str,
    *,
    config: Config | None = None,
    env: Mapping[str, str] | None = None,
    preference: str = "auto",
    gate_state: str = "primary",
    health_rows: list[dict[str, Any]] | None = None,
    now_epoch: float | None = None,
    route_ids: tuple[str, ...] | None = None,
) -> RoutePlan:
    """Return an ordered, executable, sanitized route plan."""
    if context not in CONTEXTS:
        raise ValueError(f"unsupported model context: {context}")
    if preference not in {"auto", "codex"}:
        raise ValueError(f"unsupported model preference: {preference}")
    env = os.environ if env is None else env
    by_id = {route.id: route for route in model_routes(config, env=env)}
    order = list(route_ids or DEFAULT_ORDERS[context])
    if context == "owner_chat" and preference == "codex" and "codex" in order:
        order.remove("codex")
        order.insert(0, "codex")
    health = {str(row.get("id") or ""): row for row in (health_rows or [])}
    context_routes = set(DEFAULT_ORDERS[context])
    now_epoch = float(time.time() if now_epoch is None else now_epoch)
    selected: list[ModelRoute] = []
    expired_unhealthy: list[ModelRoute] = []
    skipped: dict[str, str] = {}
    for route_id in order:
        route = by_id.get(route_id)
        if route is None:
            skipped[route_id] = "unknown_route"
        elif route_id not in context_routes:
            skipped[route_id] = "context_forbidden"
        elif not route.enabled:
            skipped[route_id] = "disabled"
        elif not route.configured:
            skipped[route_id] = "unconfigured"
        elif route.id == "primary" and gate_state == "backup":
            skipped[route_id] = "account_gate"
        elif route.id == "primary" and gate_state == "probe":
            # The sticky account gate elected exactly one bounded recovery
            # request. Its own probe lease is the authority here; retaining a
            # stale provider-health cooldown would make recovery impossible
            # while any fallback remains healthy.
            selected.append(route)
        elif route.id in health and _in_health_cooldown(
            health[route.id], env=env, now_epoch=now_epoch
        ):
            skipped[route_id] = "health_cooldown"
        elif health.get(route.id, {}).get("status") == "unhealthy":
            # An elapsed cooldown permits a bounded recovery attempt; it does
            # not make the provider healthy again.  Keep that attempt behind
            # every route that is currently healthy/not_run so a known slow
            # relay cannot block a working fallback with a production-sized
            # request merely because its timer expired.
            if (route.id == "primary"
                    and _real_failure_reason(health[route.id])
                    in _PRIMARY_RECOVERY_REASONS):
                # A transient primary rejection gets one real production-sized
                # recovery attempt after its cooldown. A repeated failure is
                # observed again and expands the next cooldown. Slow/ambiguous
                # timeout and request_failed routes remain behind known-good
                # fallbacks instead of blocking the owner repeatedly.
                selected.append(route)
            else:
                expired_unhealthy.append(route)
        else:
            selected.append(route)
    selected.extend(expired_unhealthy)
    return RoutePlan(
        context=context,
        preference=preference,
        allow_tools=context in TOOL_CONTEXTS,
        routes=tuple(selected),
        skipped=skipped,
    )


def catalog_report(
    config: Config | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    routes = model_routes(config, env=env)
    active = [route for route in routes if route.enabled and route.configured]
    upstreams: dict[str, list[str]] = {}
    for route in active:
        upstreams.setdefault(route.upstream, []).append(route.id)
    return {
        "version": 1,
        "configured_route_count": len(active),
        "independent_upstream_count": len(upstreams),
        "shared_upstreams": {
            upstream: ids for upstream, ids in upstreams.items() if len(ids) > 1
        },
        "routes": [route.public() for route in routes],
    }


def harness_environment(config: Config) -> dict[str, str]:
    """Private compatibility environment consumed by existing adapters.

    This is intentionally not a diagnostic surface: it contains credentials
    for child-process environment injection. Callers must quote it and must
    never log, persist, or return it to a user.
    """
    routes = {route.id: route for route in model_routes(config, env={})}
    claude = config.claude
    codex = config.codex
    openai = config.openai
    return {
        "CLAUDE_BACKUP_ENABLED": str(routes["backup1"].enabled).lower(),
        "CLAUDE_BACKUP_AUTH_TOKEN": routes["backup1"].credential,
        "CLAUDE_BACKUP_BASE_URL": routes["backup1"].base_url,
        "CLAUDE_BACKUP_MODEL": routes["backup1"].model,
        "CLAUDE_BACKUP2_ENABLED": str(routes["backup2"].enabled).lower(),
        "CLAUDE_BACKUP2_AUTH_TOKEN": routes["backup2"].credential,
        "CLAUDE_BACKUP2_BASE_URL": routes["backup2"].base_url,
        "CLAUDE_BACKUP2_MODEL": routes["backup2"].model,
        "BACKUP_MAX_SESSION_SIZE": str(
            claude.get("backup_max_session_size", 100000)
        ),
        "BACKUP_MAX_MEMORY_CHARS": str(
            claude.get("backup_max_memory_chars", 40000)
        ),
        "CLAUDE_RELAY_ATTEMPT_TIMEOUT": str(
            claude.get("relay_attempt_timeout", 120)
        ),
        "CODEX_FALLBACK_ENABLED": str(routes["codex"].enabled).lower(),
        "CODEX_FALLBACK_MODEL": routes["codex"].model,
        "CODEX_FALLBACK_BINARY": routes["codex"].binary,
        "CODEX_FALLBACK_TIMEOUT": str(codex.get("timeout", 300)),
        "OPENAI_FALLBACK_ENABLED": str(routes["openai"].enabled).lower(),
        "OPENAI_FALLBACK_MODEL": routes["openai"].model,
        "OPENAI_API_KEY_CONFIG": routes["openai"].credential,
        "OPENAI_BASE_URL": routes["openai"].base_url,
        "OPENAI_USER_AGENT": routes["openai"].user_agent,
        "OPENAI_FALLBACK_TIMEOUT": str(openai.get("timeout", 120)),
        "OPENAI_FALLBACK_MAX_OUTPUT_TOKENS": str(
            openai.get("max_output_tokens", 4096)
        ),
    }


def runtime_status_text(
    root: str | Path | None = None,
    *,
    context: str = "owner_chat",
    preference: str = "auto",
    gate_state: str = "primary",
    health_rows: list[dict[str, Any]] | None = None,
) -> str:
    """Human-readable, credential-free route/health/diversity status."""
    base = Path(root or os.environ.get("JARVIS_DIR") or Path.cwd()).resolve()
    config = Config(base / "jarvis.yaml")
    health_rows = list(health_rows or [])
    plan = route_plan(
        context,
        config=config,
        preference=preference,
        gate_state=gate_state,
        health_rows=health_rows,
    )
    report = catalog_report(config)
    health = {str(row.get("id") or ""): row for row in health_rows}
    route_parts = []
    for route in plan.routes:
        row = health.get(route.id, {})
        observed = str(row.get("actual_model") or "")
        model = observed or route.model
        status = str(row.get("status") or "not_run")
        route_parts.append(f"{route.label}/{model}({status})")
    route_text = " -> ".join(route_parts) or "无可用 route"
    configured = int(report["configured_route_count"])
    independent = int(report["independent_upstream_count"])
    shared = report["shared_upstreams"]
    shared_text = ""
    if shared:
        rendered = "; ".join(
            f"{upstream}: {','.join(ids)}" for upstream, ids in shared.items()
        )
        shared_text = f"；共享上游：{rendered}"
    return (
        f"当前执行计划：{route_text}\n"
        f"容灾独立性：{configured} 条已配置 route / "
        f"{independent} 个独立上游{shared_text}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Jarvis model control plane")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("catalog")
    plan_parser = sub.add_parser("plan")
    plan_parser.add_argument("--context", choices=sorted(CONTEXTS),
                             default="owner_chat")
    plan_parser.add_argument("--preference", choices=("auto", "codex"),
                             default="auto")
    plan_parser.add_argument("--gate", choices=("primary", "probe", "backup"),
                             default="primary")
    args = parser.parse_args(argv)
    if args.command == "catalog":
        output = catalog_report()
    else:
        output = route_plan(
            args.context, preference=args.preference, gate_state=args.gate
        ).public()
    print(json.dumps(output, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
