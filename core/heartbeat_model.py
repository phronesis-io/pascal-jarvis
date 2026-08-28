"""Heartbeat adapter boundary for the shared provider-neutral Model Runtime.

The heartbeat owns task prompts and private-memory composition. This module
owns provider process adapters, while ``core.model_runtime`` owns route policy,
the total deadline, replay safety, health observations, and durable receipts.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from core.config import Config
from core.heartbeat_provider import (
    drop_benign_notices,
    error_summary,
    openai_usage_fields,
)
from core.model_control import ModelRoute, model_routes
from core.model_adapter_support import (
    PREEXECUTION_REASONS,
    TRANSPORT_REASONS,
    bounded_timeout,
    claude_env,
    provider_health_rows,
    provider_reason,
    safe_error_detail,
)
from core.model_fallback import (
    fallback_for_stderr,
    is_preexecution_error,
    limit_reason,
    trip,
)
from core.model_runtime import AdapterResult, RuntimeRequest, execute
from core.safety import looks_like_error, parse_result_envelope


Runner = Callable[..., subprocess.CompletedProcess[str]]
PromptBuilder = Callable[[ModelRoute], tuple[str, str]]
Logger = Callable[..., None]
UsageObserver = Callable[[str, str, dict[str, int]], None]

@dataclass(frozen=True)
class HeartbeatModelResult:
    text: str = ""
    provider: str = ""
    model: str = ""
    call_id: str = ""
    route_id: str = ""
    status: str = ""
    terminal_reason: str = ""
    error_detail: str = ""
    timed_out: bool = False
    context_overflow: bool = False
    killed: bool = False


def run_heartbeat_model(
    logical_prompt: str,
    *,
    task_id: str,
    root: str | Path,
    work_dir: str | Path,
    claude_bin: str,
    default_model: str,
    requested_model: str = "",
    timeout: int = 600,
    allow_tools: bool = True,
    restrict_tools: bool = False,
    gate_state: str = "primary",
    prompt_builder: PromptBuilder,
    runner: Runner,
    logger: Logger,
    usage_observer: UsageObserver | None = None,
    config: Config | None = None,
    health_rows: list[dict[str, Any]] | None = None,
) -> HeartbeatModelResult:
    """Execute one attributed heartbeat request through Model Runtime."""
    base = Path(root).expanduser().resolve()
    config = config or Config(base / "jarvis.yaml")
    requested = str(requested_model or "").strip().lower()
    route_ids = (
        ("openai",)
        if requested == "gpt"
        else ("primary", "backup1", "backup2", "openai")
    )
    if requested == "gpt":
        runtime_model = ""
    elif requested in {"haiku", "sonnet", "opus"}:
        runtime_model = requested
    else:
        runtime_model = str(default_model or "").strip()
    last_error_detail = ""

    def claude_adapter(
        route: ModelRoute,
        request: RuntimeRequest,
        current_model: str,
        attempt_timeout: float,
    ) -> AdapterResult:
        nonlocal last_error_detail
        system_prompt, request_prompt = prompt_builder(route)
        command = [
            claude_bin,
            "--dangerously-skip-permissions",
            "--no-session-persistence",
            "--system-prompt",
            system_prompt,
            "--disable-slash-commands",
            "--output-format",
            "json",
            "-p",
            request_prompt,
        ]
        if not request.allow_tools:
            command.extend(["--tools", ""])
        if current_model:
            command.extend(["--model", current_model])
        logger(
            f"Calling heartbeat route={route.id} model="
            f"{current_model or '(default)'}"
        )
        attempt_limit = bounded_timeout(route, attempt_timeout)
        try:
            completed = runner(
                command,
                timeout=attempt_limit,
                cwd=str(work_dir),
                env=claude_env(route),
            )
        except subprocess.TimeoutExpired as exc:
            timeout_value = getattr(exc, "timeout", attempt_limit)
            last_error_detail = (
                f"claude call timed out ({int(float(timeout_value))}s)"
            )
            logger(
                f"Heartbeat route {route.id} timed out; runtime will apply "
                "the replay-safety policy",
                level="warn",
            )
            return AdapterResult(
                status="transport_failure",
                reason="timeout",
                effects_started=(None if request.allow_tools else False),
            )
        except OSError:
            last_error_detail = "claude CLI not found"
            return AdapterResult(
                status="preexecution_failure",
                reason="cli_unavailable",
                effects_started=False,
            )

        if completed.returncode == 0:
            text, usage = parse_result_envelope(completed.stdout or "")
            if usage is not None and usage_observer is not None:
                usage_observer(route.id, current_model or default_model, usage)
            if text.strip():
                return AdapterResult(
                    status="succeeded",
                    text=text.strip(),
                    observed_model=current_model or default_model,
                )
            return AdapterResult(
                status="ambiguous_failure",
                reason="empty_or_error_output",
                effects_started=(None if request.allow_tools else False),
            )

        error_text = drop_benign_notices("\n".join(
            value for value in (
                (completed.stderr or "").strip(),
                (completed.stdout or "").strip(),
            ) if value
        ))
        if error_text:
            last_error_detail = safe_error_detail(
                error_text, summary=error_summary,
            )
            logger(
                f"Heartbeat route {route.id} failed: "
                f"{last_error_detail}",
                level="warn",
            )
        account_reason = limit_reason(error_text)
        if route.id == "primary" and account_reason:
            try:
                trip(account_reason, base)
            except Exception:
                pass
        reason = provider_reason(error_text)
        next_model = fallback_for_stderr(current_model, error_text) or ""
        if completed.returncode in {137, 143}:
            return AdapterResult(
                status="cancelled",
                reason="process_interrupted",
                effects_started=None,
            )
        if is_preexecution_error(error_text) or reason in PREEXECUTION_REASONS:
            # Account/model/auth/rate/overload rejection happens before the
            # model can run a tool, even though the CLI process itself started.
            status = "preexecution_failure"
        elif request.allow_tools:
            # Any later tool-capable failure cannot prove that an earlier
            # model round caused no local/external effect.
            status = "ambiguous_failure"
        elif reason in TRANSPORT_REASONS:
            status = "transport_failure"
        else:
            status = "ambiguous_failure"
        return AdapterResult(
            status=status,
            reason=reason,
            effects_started=(
                False if status == "preexecution_failure"
                else (None if request.allow_tools else False)
            ),
            next_model=next_model,
        )

    def openai_adapter(
        route: ModelRoute,
        request: RuntimeRequest,
        current_model: str,
        attempt_timeout: float,
    ) -> AdapterResult:
        nonlocal last_error_detail
        from core import openai_fallback

        system_prompt, request_prompt = prompt_builder(route)
        model = current_model or route.model or openai_fallback.DEFAULT_MODEL
        max_tokens = int(os.environ.get(
            "OPENAI_FALLBACK_MAX_OUTPUT_TOKENS",
            str(openai_fallback.DEFAULT_MAX_OUTPUT_TOKENS),
        ))
        logger(f"Calling heartbeat route=openai model={model}")
        response: object | None = None
        try:
            if request.allow_tools:
                text = openai_fallback.run_agentic(
                    system_prompt,
                    request_prompt,
                    model,
                    max_tokens,
                    route.credential,
                    route.base_url or openai_fallback.DEFAULT_BASE_URL,
                    max(1, int(bounded_timeout(route, attempt_timeout))),
                    route.user_agent,
                )
            else:
                payload = openai_fallback.build_payload(
                    system_prompt, request_prompt, model, max_tokens,
                )
                response = openai_fallback.call_openai(
                    payload,
                    route.credential,
                    route.base_url or openai_fallback.DEFAULT_BASE_URL,
                    max(1, int(bounded_timeout(route, attempt_timeout))),
                    route.user_agent,
                )
                text = openai_fallback.extract_text(response)
        except Exception as exc:
            error_text = str(exc)
            reason = provider_reason(error_text)
            last_error_detail = safe_error_detail(
                error_text, summary=error_summary,
            )
            logger(
                f"Heartbeat route openai failed: {last_error_detail}",
                level="warn",
            )
            if is_preexecution_error(error_text) or reason in PREEXECUTION_REASONS:
                status = "preexecution_failure"
            elif request.allow_tools:
                status = "ambiguous_failure"
            elif reason in TRANSPORT_REASONS:
                status = "transport_failure"
            else:
                status = "ambiguous_failure"
            return AdapterResult(
                status=status,
                reason=reason,
                effects_started=(
                    False if status == "preexecution_failure"
                    else (None if request.allow_tools else False)
                ),
            )
        if response is not None and usage_observer is not None:
            fields = openai_usage_fields(response)
            if fields:
                usage_observer("openai", model, fields)
        text = str(text or "").strip()
        if text and not looks_like_error(text, proactive=True):
            return AdapterResult(
                status="succeeded", text=text, observed_model=model,
            )
        return AdapterResult(
            status="ambiguous_failure",
            reason="empty_or_error_output",
            effects_started=(None if request.allow_tools else False),
        )

    runtime = execute(
        RuntimeRequest(
            task_id=task_id,
            prompt=logical_prompt,
            context="heartbeat",
            requested_model=runtime_model,
            route_ids=route_ids,
            gate_state=gate_state,
            effect_authority=("external" if allow_tools else "none"),
            allow_tools=allow_tools,
            timeout_seconds=timeout,
            workspace=str(work_dir),
        ),
        {
            "claude_cli": claude_adapter,
            "openai_responses": openai_adapter,
        },
        root=base,
        config=config,
        health_rows=(
            provider_health_rows(base) if health_rows is None else health_rows
        ),
    )
    labels = {route.id: route.label for route in model_routes(config)}
    if runtime.status == "succeeded" and runtime.route_id == "primary" \
            and gate_state != "primary":
        try:
            from core.model_fallback import clear

            clear(base)
        except Exception:
            pass
    reason = runtime.terminal_reason
    return HeartbeatModelResult(
        text=runtime.text,
        provider=labels.get(runtime.route_id, ""),
        model=runtime.observed_model or runtime.requested_model,
        call_id=runtime.call_id,
        route_id=runtime.route_id,
        status=runtime.status,
        terminal_reason=reason,
        error_detail=last_error_detail,
        timed_out=reason in {"timeout", "total_timeout"},
        context_overflow=reason == "context_overflow",
        killed=reason == "process_interrupted",
    )
