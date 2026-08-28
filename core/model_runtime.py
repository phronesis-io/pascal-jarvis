"""Provider-neutral execution policy and receipts for Jarvis model calls.

The runtime chooses routes, owns one wall-clock and effect-replay budget, and
records what actually happened. Adapter implementations start model processes;
product state, permissions, and completion semantics remain outside this
module. Prompts and credentials are never persisted.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from core.config import Config
from core.model_control import CONTEXTS, ModelRoute, route_plan


EFFECT_AUTHORITIES = {"none", "read_only", "workspace_write", "external"}
ADAPTER_STATUSES = {
    "succeeded",
    "preexecution_failure",
    "transport_failure",
    "ambiguous_failure",
    "rejected",
    "cancelled",
}
TERMINAL_STATUSES = {"succeeded", "failed", "ambiguous", "cancelled"}
VALID_ZERO_ATTEMPT_REASONS = {
    "adapter_unavailable",
    "no_eligible_route",
    "process_interrupted",
    "total_timeout",
}
MAX_MODELS_PER_ROUTE = 3
REASON_CODES = ADAPTER_STATUSES | {
    "account_limit",
    "adapter_failure",
    "auth_error",
    "cli_unavailable",
    "context_overflow",
    "empty_or_error_output",
    "model_unavailable",
    "network_error",
    "process_interrupted",
    "request_failed",
    "rate_limited",
    "server_error",
    "server_overloaded",
    "timeout",
    "total_timeout",
    "unregistered_provider_session",
}


@dataclass(frozen=True)
class RuntimeRequest:
    task_id: str
    prompt: str
    context: str = "auxiliary_trusted"
    system_prompt: str = ""
    matter_id: str = ""
    requested_model: str = ""
    preference: str = "auto"
    gate_state: str = "primary"
    effect_authority: str = "none"
    allow_tools: bool = False
    timeout_seconds: float = 120.0
    workspace: str = ""

    def validate(self) -> None:
        _validate_reference("task_id", self.task_id, required=True)
        _validate_reference("matter_id", self.matter_id, required=False)
        if not str(self.prompt or "").strip():
            raise ValueError("model runtime prompt is required")
        if self.context not in CONTEXTS:
            raise ValueError(f"unsupported model context: {self.context}")
        if self.effect_authority not in EFFECT_AUTHORITIES:
            raise ValueError(
                f"unsupported effect authority: {self.effect_authority}"
            )
        if float(self.timeout_seconds) <= 0:
            raise ValueError("model runtime timeout_seconds must be positive")
        requested_model = str(self.requested_model or "").strip()
        if (
            requested_model
            and _sanitize_model(requested_model) != requested_model
        ):
            raise ValueError("model runtime requested_model is invalid")
        if self.effect_authority in {"workspace_write", "external"} \
                and not self.allow_tools:
            raise ValueError(
                "write/external effect authority requires a tool-capable call"
            )


@dataclass(frozen=True)
class AdapterResult:
    status: str
    text: str = ""
    reason: str = ""
    observed_model: str = ""
    effects_started: bool | None = None
    cost_usd: float | None = None
    next_model: str = ""

    def validate(self) -> None:
        if self.status not in ADAPTER_STATUSES:
            raise ValueError(f"unsupported adapter status: {self.status}")
        if self.status == "succeeded" and not str(self.text or "").strip():
            raise ValueError("successful adapter result requires text")
        if self.cost_usd is not None and float(self.cost_usd) < 0:
            raise ValueError("adapter cost_usd cannot be negative")
        if (
            self.status == "preexecution_failure"
            and self.effects_started is not False
        ):
            raise ValueError(
                "preexecution failure must prove effects_started=false"
            )


@dataclass(frozen=True)
class RuntimeAttempt:
    attempt: int
    route_id: str
    upstream: str
    adapter: str
    requested_model: str
    observed_model: str
    status: str
    reason: str
    effects_started: bool | None
    cost_usd: float | None
    started_epoch: float
    finished_epoch: float
    latency_ms: int


@dataclass(frozen=True)
class RuntimeResult:
    call_id: str
    task_id: str
    matter_id: str
    status: str
    text: str = ""
    route_id: str = ""
    requested_model: str = ""
    observed_model: str = ""
    terminal_reason: str = ""
    cost_usd: float | None = None
    elapsed_ms: int = 0
    attempts: tuple[RuntimeAttempt, ...] = field(default_factory=tuple)

    def public(self) -> dict[str, Any]:
        return {
            "schema": "jarvis.model-runtime-result.v1",
            **asdict(self),
            "attempts": [asdict(item) for item in self.attempts],
        }


Adapter = Callable[[ModelRoute, RuntimeRequest, str, float], AdapterResult]
Observer = Callable[[str, str, str], None]


def _digest(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _request_digest(request: RuntimeRequest) -> str:
    payload = json.dumps(
        [str(request.system_prompt or ""), str(request.prompt or "")],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return _digest(payload)


def _validate_reference(name: str, value: object, *, required: bool) -> None:
    text = str(value or "").strip()
    if required and not text:
        raise ValueError(f"model runtime {name} is required")
    if len(text) > 240 or any(ord(char) < 32 for char in text):
        raise ValueError(f"model runtime {name} is invalid")


def _sanitize_reason(value: object) -> str:
    reason = str(value or "").strip().lower().replace(" ", "_")
    if reason in REASON_CODES:
        return reason
    return "adapter_failure"


def _sanitize_model(value: object) -> str:
    model = str(value or "").strip()
    if 0 < len(model) <= 120 and not any(ord(char) < 32 for char in model):
        return model
    return ""


def _requested_family(model: str) -> str:
    lowered = str(model or "").lower()
    if any(name in lowered for name in ("gpt", "o1", "o3", "o4")):
        return "gpt"
    if any(name in lowered for name in ("claude", "opus", "sonnet", "haiku")):
        return "claude"
    return "other"


def _model_for_route(requested: str, route: ModelRoute) -> str:
    requested = _sanitize_model(requested)
    if not requested:
        return _sanitize_model(route.model)
    family = _requested_family(requested)
    if family == route.model_family or route.model_family == "other":
        return requested
    return _sanitize_model(route.model)


def _db():
    from core.db import get_db
    return get_db()


def _insert_call(call_id: str, request: RuntimeRequest, started: float) -> None:
    db = _db()
    db.execute(
        """INSERT INTO model_runtime_calls
           (id,task_id,matter_id,context,effect_authority,prompt_digest,
            status,requested_model,executor_pid,started_epoch)
           VALUES (?,?,?,?,?,?,'running',?,?,?)""",
        (
            call_id,
            str(request.task_id).strip(),
            str(request.matter_id or "").strip(),
            request.context,
            request.effect_authority,
            _request_digest(request),
            _sanitize_model(request.requested_model),
            os.getpid(),
            started,
        ),
    )
    db.commit()


def _pid_alive(pid: int) -> bool:
    if int(pid) <= 0:
        return False
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def recover_abandoned(
    *,
    now_epoch: float | None = None,
    stale_after: float = 1800,
    pid_alive: Callable[[int], bool] | None = None,
) -> list[str]:
    """Close stale running receipts only when their executor is gone.

    A live PID is deliberately left alone even when the call is old. PID reuse
    can therefore delay recovery, but can never make Jarvis declare a live call
    interrupted or replay its effects.
    """
    now_epoch = float(time.time() if now_epoch is None else now_epoch)
    threshold = now_epoch - max(1.0, float(stale_after))
    is_alive = pid_alive or _pid_alive
    db = _db()
    candidates = db.execute(
        """SELECT id,effect_authority,executor_pid,started_epoch
             FROM model_runtime_calls
             WHERE status='running' AND started_epoch < ?
             ORDER BY started_epoch""",
        (threshold,),
    ).fetchall()
    recovered: list[str] = []
    for row in candidates:
        pid = int(row["executor_pid"] or 0)
        if is_alive(pid):
            continue
        duration_ms = max(
            0, int((now_epoch - float(row["started_epoch"])) * 1000)
        )
        terminal_status = (
            "ambiguous"
            if row["effect_authority"] in {"workspace_write", "external"}
            else "failed"
        )
        cursor = db.execute(
            """UPDATE model_runtime_calls
                  SET status=?,terminal_reason='process_interrupted',
                      finished_epoch=?,duration_ms=?,
                      attempt_count=(
                          SELECT COUNT(*) FROM model_runtime_attempts
                           WHERE call_id=model_runtime_calls.id
                      )
                WHERE id=? AND status='running'
                  AND executor_pid=? AND started_epoch=?""",
            (
                terminal_status,
                now_epoch,
                duration_ms,
                row["id"],
                pid,
                row["started_epoch"],
            ),
        )
        if cursor.rowcount:
            recovered.append(str(row["id"]))
    db.commit()
    return recovered


def _insert_attempt(call_id: str, item: RuntimeAttempt) -> None:
    db = _db()
    db.execute(
        """INSERT INTO model_runtime_attempts
           (call_id,attempt,route_id,upstream,adapter,requested_model,
            observed_model,status,reason,effects_started,cost_usd,
            started_epoch,finished_epoch,latency_ms)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            call_id,
            item.attempt,
            item.route_id,
            item.upstream,
            item.adapter,
            item.requested_model,
            item.observed_model,
            item.status,
            item.reason,
            None if item.effects_started is None else int(item.effects_started),
            item.cost_usd,
            item.started_epoch,
            item.finished_epoch,
            item.latency_ms,
        ),
    )
    db.commit()


def _finish_call(result: RuntimeResult, finished: float) -> None:
    if result.status not in TERMINAL_STATUSES:
        raise ValueError(f"unsupported runtime terminal status: {result.status}")
    db = _db()
    db.execute(
        """UPDATE model_runtime_calls
              SET status=?,selected_route=?,requested_model=?,observed_model=?,
                  terminal_reason=?,cost_usd=?,finished_epoch=?,duration_ms=?,
                  attempt_count=?
            WHERE id=?""",
        (
            result.status,
            result.route_id,
            result.requested_model,
            result.observed_model,
            result.terminal_reason,
            result.cost_usd,
            finished,
            result.elapsed_ms,
            len(result.attempts),
            result.call_id,
        ),
    )
    db.commit()


def _default_observer(root: Path) -> Observer:
    def observe(route_id: str, status: str, reason: str) -> None:
        try:
            from core.provider_health import observe as record
            record(route_id, status, reason, root=root)
        except Exception:
            pass
    return observe


def _may_replay(request: RuntimeRequest, outcome: AdapterResult) -> bool:
    if outcome.status == "preexecution_failure":
        return True
    if outcome.status not in {"transport_failure", "ambiguous_failure"}:
        return False
    if request.effect_authority in {"none", "read_only"}:
        return True
    return outcome.effects_started is False


def execute(
    request: RuntimeRequest,
    adapters: Mapping[str, Adapter],
    *,
    root: str | Path,
    config: Config | None = None,
    health_rows: list[dict[str, Any]] | None = None,
    observer: Observer | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    epoch: Callable[[], float] = time.time,
) -> RuntimeResult:
    """Execute one request through policy-selected routes and persist a receipt."""
    request.validate()
    base = Path(root).expanduser().resolve()
    config = config or Config(base / "jarvis.yaml")
    plan = route_plan(
        request.context,
        config=config,
        preference=request.preference,
        gate_state=request.gate_state,
        health_rows=health_rows or [],
    )
    if request.allow_tools and not plan.allow_tools:
        raise ValueError(
            f"model context {request.context} does not permit tools"
        )
    recover_abandoned(now_epoch=epoch())
    call_id = f"mrc_{uuid.uuid4().hex[:20]}"
    started_monotonic = monotonic()
    started_epoch = epoch()
    _insert_call(call_id, request, started_epoch)
    attempts: list[RuntimeAttempt] = []
    total_cost = 0.0
    cost_known = True
    observe = observer or _default_observer(base)
    final_status = "failed"
    terminal_reason = "no_eligible_route"
    final_text = ""
    final_route = ""
    final_requested_model = _sanitize_model(request.requested_model)
    final_observed_model = ""

    for route_index, route in enumerate(plan.routes):
        adapter = adapters.get(route.adapter)
        if adapter is None:
            if not attempts:
                terminal_reason = "adapter_unavailable"
            continue
        model = _model_for_route(request.requested_model, route)
        attempted_models: set[str] = set()
        while (
            model
            and model not in attempted_models
            and len(attempted_models) < MAX_MODELS_PER_ROUTE
        ):
            elapsed = monotonic() - started_monotonic
            remaining = float(request.timeout_seconds) - elapsed
            if remaining <= 0:
                terminal_reason = "total_timeout"
                break
            remaining_routes = max(1, len(plan.routes) - route_index)
            attempt_timeout = remaining if remaining_routes == 1 else max(
                0.05, remaining / remaining_routes
            )
            attempted_models.add(model)
            attempt_started_mono = monotonic()
            attempt_started_epoch = epoch()
            try:
                outcome = adapter(route, request, model, attempt_timeout)
                outcome.validate()
            except TimeoutError:
                outcome = AdapterResult(
                    status="transport_failure",
                    reason="timeout",
                    effects_started=(None if request.allow_tools else False),
                )
            except Exception:
                outcome = AdapterResult(
                    status="ambiguous_failure",
                    reason="adapter_failure",
                    effects_started=(None if request.allow_tools else False),
                )
            attempt_finished_mono = monotonic()
            attempt_finished_epoch = epoch()
            reason = _sanitize_reason(outcome.reason or outcome.status)
            observed_model = _sanitize_model(outcome.observed_model)
            attempt = RuntimeAttempt(
                attempt=len(attempts) + 1,
                route_id=route.id,
                upstream=route.upstream,
                adapter=route.adapter,
                requested_model=model,
                observed_model=observed_model,
                status=outcome.status,
                reason=reason,
                effects_started=outcome.effects_started,
                cost_usd=outcome.cost_usd,
                started_epoch=attempt_started_epoch,
                finished_epoch=attempt_finished_epoch,
                latency_ms=max(
                    0, int((attempt_finished_mono - attempt_started_mono) * 1000)
                ),
            )
            attempts.append(attempt)
            _insert_attempt(call_id, attempt)
            if outcome.cost_usd is None:
                cost_known = False
            else:
                total_cost += float(outcome.cost_usd)
            terminal_reason = reason
            final_route = route.id
            final_requested_model = model
            final_observed_model = observed_model
            if outcome.status == "succeeded":
                try:
                    observe(route.id, "healthy", "request_succeeded")
                except Exception:
                    pass
                final_status = "succeeded"
                final_text = outcome.text.strip()
                break
            if outcome.status == "cancelled":
                final_status = (
                    "ambiguous"
                    if request.effect_authority in {
                        "workspace_write", "external"
                    } and outcome.effects_started is not False
                    else "cancelled"
                )
                break
            if outcome.status in {
                "transport_failure", "preexecution_failure"
            }:
                try:
                    observe(route.id, "unhealthy", terminal_reason)
                except Exception:
                    pass
            replayable = _may_replay(request, outcome)
            next_model = _sanitize_model(outcome.next_model)
            if (
                replayable
                and next_model
                and next_model not in attempted_models
            ):
                model = next_model
                continue
            if replayable:
                break
            final_status = (
                "ambiguous"
                if outcome.status in {"ambiguous_failure", "transport_failure"}
                and request.effect_authority in {"workspace_write", "external"}
                else "failed"
            )
            break
        if final_status in {"succeeded", "cancelled", "ambiguous"}:
            break
        if terminal_reason == "total_timeout":
            break
        if attempts and not _may_replay(
            request,
            AdapterResult(
                status=attempts[-1].status,
                reason=attempts[-1].reason,
                effects_started=attempts[-1].effects_started,
            ),
        ):
            break

    elapsed_ms = max(0, int((monotonic() - started_monotonic) * 1000))
    result = RuntimeResult(
        call_id=call_id,
        task_id=str(request.task_id).strip(),
        matter_id=str(request.matter_id or "").strip(),
        status=final_status,
        text=final_text,
        route_id=final_route,
        requested_model=final_requested_model,
        observed_model=final_observed_model,
        terminal_reason=terminal_reason,
        cost_usd=total_cost if cost_known and attempts else None,
        elapsed_ms=elapsed_ms,
        attempts=tuple(attempts),
    )
    _finish_call(result, epoch())
    return result


def audit(*, now_epoch: float | None = None, stale_after: float = 1800) -> dict[str, Any]:
    now_epoch = float(time.time() if now_epoch is None else now_epoch)
    db = _db()
    stale = db.execute(
        """SELECT id,task_id,executor_pid,started_epoch
             FROM model_runtime_calls
             WHERE status='running' AND started_epoch < ?
             ORDER BY started_epoch""",
        (now_epoch - max(1.0, float(stale_after)),),
    ).fetchall()
    unattributed = db.execute(
        """SELECT COUNT(*) FROM model_runtime_calls
             WHERE trim(task_id)=''"""
    ).fetchone()[0]
    zero_attempt_reasons = tuple(sorted(VALID_ZERO_ATTEMPT_REASONS))
    placeholders = ",".join("?" for _ in zero_attempt_reasons)
    missing_attempts = db.execute(
        f"""SELECT COUNT(*) FROM model_runtime_calls c
             WHERE c.status != 'running'
               AND c.terminal_reason NOT IN ({placeholders})
               AND NOT EXISTS (
                   SELECT 1 FROM model_runtime_attempts a WHERE a.call_id=c.id
               )""",
        zero_attempt_reasons,
    ).fetchone()[0]
    return {
        "schema": "jarvis.model-runtime-audit.v1",
        "healthy": not stale and not unattributed and not missing_attempts,
        "stale_running": [dict(row) for row in stale],
        "unattributed_calls": int(unattributed),
        "terminal_calls_without_attempts": int(missing_attempts),
    }


def recent(limit: int = 20) -> list[dict[str, Any]]:
    rows = _db().execute(
        """SELECT id,task_id,matter_id,context,effect_authority,status,
                  selected_route,requested_model,observed_model,terminal_reason,
                  cost_usd,started_epoch,finished_epoch,duration_ms,attempt_count
             FROM model_runtime_calls
             ORDER BY started_epoch DESC LIMIT ?""",
        (max(1, min(int(limit), 100)),),
    ).fetchall()
    return [dict(row) for row in rows]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m core.model_runtime")
    sub = parser.add_subparsers(dest="command", required=True)
    recent_parser = sub.add_parser("recent")
    recent_parser.add_argument("--limit", type=int, default=20)
    sub.add_parser("audit")
    args = parser.parse_args(argv)
    payload = recent(args.limit) if args.command == "recent" else audit()
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
