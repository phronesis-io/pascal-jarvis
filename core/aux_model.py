"""Bounded provider routing for Jarvis auxiliary model calls.

Main Lark replies and heartbeat task execution keep their richer, stateful
runners.  This module covers the smaller call sites that previously followed
only Backup 1: background jobs, EigenFlux message analysis, and idle-noise
classification.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from core.claude_bin import resolve_claude_bin
from core.model_fallback import (
    fallback_for_stderr,
    gate,
    is_preexecution_error,
    limit_reason,
    trip,
)
from core.model_credentials import without_model_credentials
from core.safety import looks_like_error


Runner = Callable[..., subprocess.CompletedProcess[str]]
SessionRegistrar = Callable[[str], bool]


@dataclass(frozen=True)
class AuxiliaryModelResult:
    text: str = ""
    provider: str = ""
    model: str = ""
    attempted: tuple[str, ...] = ()
    call_id: str = ""
    status: str = ""
    terminal_reason: str = ""


def _provider_health_rows(root: str | Path) -> list[dict[str, Any]]:
    """Read one sanitized health snapshot; damaged telemetry must fail open."""
    try:
        from core.provider_health import snapshot

        return list(snapshot(root).get("providers") or [])
    except Exception:
        return []


def _terminate_process_group(
    process: subprocess.Popen[str] | None,
    *,
    grace: float = 0.25,
) -> None:
    """Stop a model process and every tool process in its private session."""
    if process is None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, 0)
    except ProcessLookupError:
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=0.5)
    except subprocess.TimeoutExpired:
        pass


def _invoke(
    command: list[str],
    prompt: str,
    *,
    timeout: float,
    cwd: str,
    env: dict[str, str] | None,
    runner: Runner | None,
    process_holder: dict[str, Any] | None,
    process_key: str,
    cancelled: Callable[[], bool] | None,
) -> subprocess.CompletedProcess[str]:
    if runner is not None:
        return runner(
            command,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
            env=env,
        )

    process: subprocess.Popen[str] | None = None
    spawning_key = f"{process_key}:spawning"
    try:
        if process_holder is not None:
            process_holder[spawning_key] = True
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=cwd,
            env=env,
            start_new_session=True,
        )
        if process_holder is not None:
            process_holder[process_key] = process
            process_holder[spawning_key] = False
        if cancelled is not None and cancelled():
            _terminate_process_group(process)
            return subprocess.CompletedProcess(
                command, 143, "", "cancelled"
            )
        stdout, stderr = process.communicate(prompt, timeout=timeout)
        return subprocess.CompletedProcess(
            command, process.returncode, stdout, stderr
        )
    except subprocess.TimeoutExpired as exc:
        _terminate_process_group(process)
        for stream in (process.stdin, process.stdout, process.stderr):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass
        raise subprocess.TimeoutExpired(
            command,
            timeout,
            output=getattr(exc, "output", None),
            stderr=getattr(exc, "stderr", None),
        ) from None
    finally:
        if (
            process_holder is not None
            and process_holder.get(process_key) is process
        ):
            process_holder[process_key] = None
        if process_holder is not None:
            process_holder[spawning_key] = False


def _session_args(
    base: tuple[str, ...],
    attempt_index: int,
    session_registrar: SessionRegistrar | None = None,
) -> list[str]:
    if not base:
        return ["--no-session-persistence"]
    values = list(base)
    if "--session-id" not in values or not attempt_index:
        return values
    retry_session_id = str(uuid.uuid4())
    if session_registrar is not None and not session_registrar(retry_session_id):
        raise RuntimeError("refusing unregistered provider retry session")
    session_index = values.index("--session-id") + 1
    values[session_index] = retry_session_id
    return values


def _openai_result(
    system_prompt: str,
    prompt: str,
    *,
    allow_tools: bool,
    timeout: int,
    root: str | Path | None = None,
    process_holder: dict[str, Any] | None = None,
    process_key: str = "model",
    cancelled: Callable[[], bool] | None = None,
    route=None,
    model_override: str = "",
) -> tuple[str, str]:
    from core.config import Config
    from core.model_control import model_routes

    base = Path(root or os.environ.get("JARVIS_DIR") or Path.cwd())
    route = route or next(
        item for item in model_routes(Config(base / "jarvis.yaml"))
        if item.id == "openai"
    )
    if not route.enabled or not route.configured:
        return "", ""
    api_key = route.credential

    from core import openai_fallback

    model = model_override or route.model or openai_fallback.DEFAULT_MODEL
    base_url = route.base_url or openai_fallback.DEFAULT_BASE_URL
    max_tokens = int(
        os.environ.get(
            "OPENAI_FALLBACK_MAX_OUTPUT_TOKENS",
            str(openai_fallback.DEFAULT_MAX_OUTPUT_TOKENS),
        )
    )
    user_agent = route.user_agent
    if allow_tools:
        text = openai_fallback.run_agentic(
            system_prompt,
            prompt,
            model,
            max_tokens,
            api_key,
            base_url,
            timeout,
            user_agent,
            process_holder=process_holder,
            process_key=process_key,
            cancelled=cancelled,
        )
    else:
        payload = openai_fallback.build_payload(
            system_prompt, prompt, model, max_tokens
        )
        text = openai_fallback.extract_text(
            openai_fallback.call_openai(
                payload, api_key, base_url, timeout, user_agent
            )
        )
    return text.strip(), model


def run_auxiliary_model(
    prompt: str,
    *,
    root: str | Path,
    system_prompt: str = "",
    model: str = "opus",
    timeout: int = 120,
    allow_tools: bool = False,
    session_args: tuple[str, ...] = (),
    session_registrar: SessionRegistrar | None = None,
    runner: Runner | None = None,
    process_holder: dict[str, Any] | None = None,
    process_key: str = "model",
    claude_bin: str = "",
    cancelled: Callable[[], bool] | None = None,
    work_dir: str | Path | None = None,
    task_id: str = "",
    matter_id: str = "",
    effect_authority: str = "",
) -> AuxiliaryModelResult:
    """Run one attributed auxiliary call through the shared Model Runtime."""
    from core.config import Config
    from core.model_control import model_routes
    from core.model_runtime import AdapterResult, RuntimeRequest, execute
    from core.provider_health import reason_code_for_error

    root_path = Path(root)
    try:
        gate_state = gate(root_path, probe=False)
    except Exception:
        gate_state = "primary"

    prompt_file = ""
    if system_prompt:
        handle = tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix="jarvis-aux-prompt-",
            suffix=".txt",
            delete=False,
        )
        try:
            handle.write(system_prompt)
            prompt_file = handle.name
        finally:
            handle.close()

    attempt_index = [0]

    def claude_adapter(route, request, current_model, attempt_timeout):
        if cancelled is not None and cancelled():
            return AdapterResult(
                status="cancelled", reason="cancelled", effects_started=False,
            )
        provider_env = without_model_credentials(
            keep=frozenset({"ANTHROPIC_API_KEY"}),
        )
        if route.id != "primary":
            provider_env = without_model_credentials()
            provider_env["ANTHROPIC_AUTH_TOKEN"] = route.credential
            provider_env["ANTHROPIC_BASE_URL"] = route.base_url
        command = [
            resolve_claude_bin(claude_bin),
            "-p",
            "--model",
            current_model,
            "--disable-slash-commands",
        ]
        if prompt_file:
            command.extend(["--append-system-prompt-file", prompt_file])
        try:
            command.extend(_session_args(
                session_args,
                attempt_index[0],
                session_registrar,
            ))
        except RuntimeError:
            return AdapterResult(
                status="rejected",
                reason="unregistered_provider_session",
                effects_started=False,
            )
        attempt_index[0] += 1
        if request.allow_tools:
            command.append("--dangerously-skip-permissions")
        else:
            command.extend([
                "--permission-mode", "dontAsk", "--tools", "",
                "--mcp-config", '{"mcpServers":{}}', "--strict-mcp-config",
            ])
        try:
            completed = _invoke(
                command,
                request.prompt,
                timeout=attempt_timeout,
                cwd=str(work_dir or root_path),
                env=provider_env,
                runner=runner,
                process_holder=process_holder,
                process_key=process_key,
                cancelled=cancelled,
            )
        except subprocess.TimeoutExpired:
            return AdapterResult(
                status="transport_failure",
                reason="timeout",
                effects_started=(None if request.allow_tools else False),
            )
        except OSError:
            return AdapterResult(
                status="preexecution_failure",
                reason="cli_unavailable",
                effects_started=False,
            )
        if cancelled is not None and cancelled():
            return AdapterResult(status="cancelled", reason="cancelled")
        text = (completed.stdout or "").strip()
        error_text = "\n".join(
            part for part in (completed.stderr or "", completed.stdout or "")
            if part
        )
        if (
            completed.returncode == 0
            and text
            and not looks_like_error(text, proactive=True)
        ):
            return AdapterResult(
                status="succeeded", text=text, observed_model=current_model,
            )
        account_reason = limit_reason(error_text)
        if route.id == "primary" and account_reason:
            try:
                trip(account_reason, root_path)
            except Exception:
                pass
        next_model = fallback_for_stderr(current_model, error_text) or ""
        reason = reason_code_for_error(error_text)
        if completed.returncode in {137, 143}:
            status = "cancelled" if (cancelled and cancelled()) else (
                "ambiguous_failure"
            )
        elif request.allow_tools:
            # Once a tool-capable CLI process has run, stderr cannot prove
            # whether an earlier model round already caused a local effect.
            status = "ambiguous_failure"
        elif is_preexecution_error(error_text) or reason in {
            "account_limit", "auth_error", "rate_limited", "server_overloaded",
        }:
            status = "preexecution_failure"
        elif reason in {"network_error", "server_error", "timeout"}:
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

    def openai_adapter(route, request, current_model, attempt_timeout):
        if cancelled is not None and cancelled():
            return AdapterResult(
                status="cancelled", reason="cancelled", effects_started=False,
            )
        try:
            text, observed = _openai_result(
                request.system_prompt,
                request.prompt,
                allow_tools=request.allow_tools,
                timeout=max(1, int(attempt_timeout)),
                root=root_path,
                process_holder=process_holder,
                process_key=process_key,
                cancelled=cancelled,
                route=route,
                model_override=current_model,
            )
        except Exception as exc:
            reason = reason_code_for_error(str(exc))
            if request.allow_tools:
                # The agentic loop may fail on a later API round after one or
                # more local tools completed. The exception text alone cannot
                # certify a pre-execution failure.
                status = "ambiguous_failure"
            elif is_preexecution_error(str(exc)) or reason in {
                "account_limit", "auth_error", "rate_limited", "server_overloaded",
            }:
                status = "preexecution_failure"
            elif reason in {"network_error", "server_error", "timeout"}:
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
        if cancelled is not None and cancelled():
            return AdapterResult(status="cancelled", reason="cancelled")
        if text and not looks_like_error(text, proactive=True):
            return AdapterResult(
                status="succeeded", text=text, observed_model=observed,
            )
        return AdapterResult(
            status="ambiguous_failure",
            reason="empty_or_error_output",
            effects_started=(None if request.allow_tools else False),
        )

    derived_task_id = str(task_id or "").strip() or (
        f"auxiliary:{process_key}:"
        + hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:12]
    )
    requested_model = model if model in {"haiku", "sonnet"} else ""
    authority = effect_authority or (
        "workspace_write" if allow_tools else "none"
    )
    try:
        runtime = execute(
            RuntimeRequest(
                task_id=derived_task_id,
                matter_id=matter_id,
                prompt=prompt,
                system_prompt=system_prompt,
                context="auxiliary_trusted",
                requested_model=requested_model,
                gate_state=gate_state,
                effect_authority=authority,
                allow_tools=allow_tools,
                timeout_seconds=timeout,
                workspace=str(work_dir or root_path),
            ),
            {
                "claude_cli": claude_adapter,
                "openai_responses": openai_adapter,
            },
            root=root_path,
            config=Config(root_path / "jarvis.yaml"),
            health_rows=_provider_health_rows(root_path),
        )
        if runtime.terminal_reason == "unregistered_provider_session":
            raise RuntimeError("refusing unregistered provider retry session")
        labels = {
            route.id: route.label
            for route in model_routes(Config(root_path / "jarvis.yaml"))
        }
        return AuxiliaryModelResult(
            text=runtime.text,
            provider=labels.get(runtime.route_id, ""),
            model=runtime.observed_model or runtime.requested_model,
            attempted=tuple(
                f"{labels.get(item.route_id, item.route_id)}:"
                f"{item.requested_model}"
                for item in runtime.attempts
            ),
            call_id=runtime.call_id,
            status=runtime.status,
            terminal_reason=runtime.terminal_reason,
        )
    finally:
        if prompt_file:
            try:
                Path(prompt_file).unlink()
            except OSError:
                pass


def _read_optional(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=os.environ.get("JARVIS_DIR", ""))
    parser.add_argument("--system-prompt-file", default="")
    parser.add_argument("--consume-system-prompt-file", action="store_true")
    parser.add_argument("--model", default="opus")
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--allow-tools", action="store_true")
    parser.add_argument("--session-id", default="")
    parser.add_argument("--resume", default="")
    parser.add_argument("--fork-session", action="store_true")
    parser.add_argument("--metadata-file", default="")
    parser.add_argument("--managed-job-id", default="")
    parser.add_argument("--jobs-dir", default=os.environ.get("JV_JOBS_DIR", ""))
    parser.add_argument("--task-id", default="")
    parser.add_argument("--matter-id", default="")
    args = parser.parse_args(argv)

    root = args.root or Path(__file__).resolve().parent.parent
    session: tuple[str, ...] = ()
    if args.resume:
        values = ["--resume", args.resume]
        if args.fork_session:
            values.append("--fork-session")
        if args.session_id:
            values.extend(("--session-id", args.session_id))
        session = tuple(values)
    elif args.session_id:
        session = ("--session-id", args.session_id)

    system_prompt = _read_optional(args.system_prompt_file)
    if args.consume_system_prompt_file and args.system_prompt_file:
        try:
            Path(args.system_prompt_file).unlink()
        except OSError:
            pass

    session_registrar = None
    if args.managed_job_id:
        from core.jobs import JobManager

        jobs_dir = args.jobs_dir or str(Path(root) / "jobs")
        manager = JobManager(jobs_dir)

        def register_session(session_id: str) -> bool:
            return manager.add_session_id(args.managed_job_id, session_id)

        session_registrar = register_session

    process_holder: dict[str, Any] = {
        "model": None,
        "model:spawning": False,
    }
    termination_signal = [0]

    def _terminate(signum, _frame):
        termination_signal[0] = signum
        _terminate_process_group(process_holder.get("model"))
        if not process_holder.get("model:spawning"):
            raise SystemExit(128 + signum)

    previous_handlers = {}
    for signum in (signal.SIGTERM, signal.SIGINT):
        previous_handlers[signum] = signal.signal(signum, _terminate)
    try:
        result = run_auxiliary_model(
            sys.stdin.read(),
            root=root,
            system_prompt=system_prompt,
            model=args.model,
            timeout=args.timeout,
            allow_tools=args.allow_tools,
            session_args=session,
            session_registrar=session_registrar,
            process_holder=process_holder,
            cancelled=lambda: bool(termination_signal[0]),
            task_id=(
                args.task_id
                or (f"job:{args.managed_job_id}" if args.managed_job_id else "")
                or "auxiliary:cli"
            ),
            matter_id=args.matter_id,
        )
    finally:
        _terminate_process_group(process_holder.get("model"))
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
    if termination_signal[0]:
        return 128 + termination_signal[0]
    if not result.text:
        return 1
    if args.metadata_file:
        path = Path(args.metadata_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(asdict(result), ensure_ascii=False),
            encoding="utf-8",
        )
    sys.stdout.write(result.text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
