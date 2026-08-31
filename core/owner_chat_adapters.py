"""Provider process adapters for owner-private Lark model turns."""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable

from core.heartbeat_provider import drop_benign_notices, error_summary
from core.model_adapter_support import (
    PREEXECUTION_REASONS,
    TRANSPORT_REASONS,
    bounded_timeout,
    claude_env,
    provider_reason,
    safe_error_detail,
)
from core.model_control import ModelRoute
from core.model_fallback import (
    fallback_for_stderr,
    is_preexecution_error,
    limit_reason,
    trip,
)
from core.model_runtime import AdapterResult, RuntimeRequest
from core.safety import looks_like_error, parse_result_envelope


Runner = Callable[..., subprocess.CompletedProcess[str]]
CodexRunner = Callable[..., str]
OpenAIRunner = Callable[..., str]
Logger = Callable[..., None]


def terminate_process_group(process: subprocess.Popen[str] | None) -> None:
    if process is None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=0.5)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=0.5)
    except subprocess.TimeoutExpired:
        pass


def process_runner(holder: dict[str, Any]) -> Runner:
    def run(command, *, timeout, cwd, env, input=""):
        process: subprocess.Popen[str] | None = None
        holder["process:spawning"] = True
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=str(cwd),
                env=env,
                start_new_session=True,
            )
            holder["process"] = process
            holder["process:spawning"] = False
            if holder.get("cancelled"):
                terminate_process_group(process)
                return subprocess.CompletedProcess(
                    command, 143, "", "process interrupted",
                )
            stdout, stderr = process.communicate(
                input=input, timeout=max(1, int(timeout)),
            )
        except subprocess.TimeoutExpired:
            terminate_process_group(process)
            raise
        finally:
            if holder.get("process") is process:
                holder["process"] = None
            holder["process:spawning"] = False
        return subprocess.CompletedProcess(
            command, int(process.returncode or 0), stdout, stderr,
        )

    return run


def provider_label(route_id: str) -> str:
    return {
        "primary": "Claude primary",
        "backup1": "Claude backup",
        "backup2": "Claude backup2",
        "codex": "Codex",
        "openai": "GPT fallback",
    }.get(str(route_id), "")


@contextlib.contextmanager
def _working_directory(path: Path):
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


def _claude_command(
    *,
    binary: str,
    system_prompt_file: str,
    session_id: str,
    resume: bool,
    model: str,
    allow_tools: bool,
) -> list[str]:
    command = [binary, "-p"]
    command.extend(["--resume" if resume else "--session-id", session_id])
    command.extend([
        "--model", model,
        "--output-format", "json",
    ])
    if system_prompt_file:
        command.extend(["--append-system-prompt-file", system_prompt_file])
    if allow_tools:
        command.append("--dangerously-skip-permissions")
    else:
        command.extend([
            "--allowedTools", "WebSearch",
            "--disallowedTools",
            "Bash,Edit,Write,NotebookEdit,Read,Glob,Grep,Agent,Skill,"
            "WebFetch,TaskCreate,TaskUpdate",
        ])
    return command


def _write_private_prompt(value: str) -> Path | None:
    if not value:
        return None
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        prefix="jarvis-owner-prompt-",
        suffix=".txt",
        delete=False,
    )
    try:
        handle.write(value)
    finally:
        handle.close()
    path = Path(handle.name)
    path.chmod(0o600)
    return path


class OwnerChatAdapters:
    """State shared by one turn's provider adapters, never across turns."""

    def __init__(
        self,
        *,
        base: Path,
        work: Path,
        sessions: Path,
        session_id: str,
        conv_key: str,
        context_key: str,
        system_prompt: str,
        backup_system_prompt: str,
        claude_bin: str,
        runner: Runner,
        process_holder: dict[str, Any],
        codex_runner: CodexRunner | None,
        openai_runner: OpenAIRunner | None,
        logger: Logger,
    ):
        self.base = base
        self.work = work
        self.sessions = sessions
        self.session_id = session_id
        self.conv_key = conv_key
        self.context_key = context_key
        self.system_prompt = system_prompt
        self.backup_system_prompt = backup_system_prompt
        self.claude_bin = claude_bin
        self.runner = runner
        self.process_holder = process_holder
        self.codex_runner = codex_runner
        self.openai_runner = openai_runner
        self.logger = logger
        self.last_error_detail = ""

    def mapping(self):
        return {
            "claude_cli": self.claude,
            "codex_cli": self.codex,
            "openai_responses": self.openai,
        }

    def _system_prompt(self, route: ModelRoute) -> str:
        if route.id in {"backup1", "backup2"} and self.backup_system_prompt:
            return self.backup_system_prompt
        return self.system_prompt

    def claude(
        self,
        route: ModelRoute,
        request: RuntimeRequest,
        current_model: str,
        attempt_timeout: float,
    ) -> AdapterResult:
        prompt_file = _write_private_prompt(self._system_prompt(route))
        command = _claude_command(
            binary=self.claude_bin,
            system_prompt_file=str(prompt_file or ""),
            session_id=self.session_id,
            resume=(self.sessions / f"{self.session_id}.jsonl").is_file(),
            model=current_model,
            allow_tools=request.allow_tools,
        )
        self.logger(f"owner chat route={route.id} model={current_model}")
        attempt_limit = bounded_timeout(route, attempt_timeout)
        try:
            completed = self.runner(
                command,
                timeout=attempt_limit,
                cwd=self.work,
                env=claude_env(route),
                input=request.prompt,
            )
        except subprocess.TimeoutExpired:
            self.last_error_detail = (
                f"claude call timed out ({int(attempt_limit)}s)"
            )
            return AdapterResult(
                status="transport_failure",
                reason="timeout",
                effects_started=(None if request.allow_tools else False),
            )
        except OSError:
            self.last_error_detail = "claude CLI not found"
            return AdapterResult(
                status="preexecution_failure",
                reason="cli_unavailable",
                effects_started=False,
            )
        finally:
            if prompt_file is not None:
                prompt_file.unlink(missing_ok=True)
        if completed.returncode == 0:
            text, _usage = parse_result_envelope(completed.stdout or "")
            text = str(text or "").strip()
            if text and not looks_like_error(text):
                return AdapterResult(
                    status="succeeded",
                    text=text,
                    observed_model=current_model,
                )

        error_text = drop_benign_notices("\n".join(
            value for value in (
                (completed.stderr or "").strip(),
                (completed.stdout or "").strip(),
            ) if value
        ))
        self.last_error_detail = safe_error_detail(
            error_text, summary=error_summary,
        )
        account_reason = limit_reason(error_text)
        if route.id == "primary" and account_reason:
            try:
                trip(account_reason, self.base)
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
            next_model=next_model,
        )

    def codex(
        self,
        route: ModelRoute,
        request: RuntimeRequest,
        current_model: str,
        attempt_timeout: float,
    ) -> AdapterResult:
        from core.codex_fallback import (
            CodexFallbackError,
            CodexUnavailableError,
            run_fallback,
        )

        call = self.codex_runner or run_fallback
        try:
            text = call(
                content=request.prompt,
                conv_key=self.conv_key,
                context_key=self.context_key,
                system_prompt=self._system_prompt(route),
                model=current_model,
                timeout=max(1, int(attempt_timeout)),
                work_dir=self.work,
                binary=route.binary,
                allow_tools=request.allow_tools,
                process_holder=self.process_holder,
            )
        except CodexUnavailableError as exc:
            self.last_error_detail = safe_error_detail(str(exc))
            return AdapterResult(
                status="preexecution_failure",
                reason="cli_unavailable",
                effects_started=False,
            )
        except CodexFallbackError as exc:
            self.last_error_detail = safe_error_detail(str(exc))
            return AdapterResult(
                status="ambiguous_failure",
                reason="request_failed",
                effects_started=None,
            )
        text = str(text or "").strip()
        if text and not looks_like_error(text):
            return AdapterResult(
                status="succeeded", text=text, observed_model=current_model,
            )
        return AdapterResult(
            status="ambiguous_failure",
            reason="empty_or_error_output",
            effects_started=(None if request.allow_tools else False),
        )

    def openai(
        self,
        route: ModelRoute,
        request: RuntimeRequest,
        current_model: str,
        attempt_timeout: float,
    ) -> AdapterResult:
        from core import openai_fallback

        model = current_model or route.model or openai_fallback.DEFAULT_MODEL
        call = self.openai_runner or openai_fallback.run_agentic
        try:
            if request.allow_tools:
                with _working_directory(self.work):
                    call_args = (
                        self._system_prompt(route),
                        request.prompt,
                        model,
                        int(os.environ.get(
                            "OPENAI_FALLBACK_MAX_OUTPUT_TOKENS",
                            str(openai_fallback.DEFAULT_MAX_OUTPUT_TOKENS),
                        )),
                        route.credential,
                        route.base_url or openai_fallback.DEFAULT_BASE_URL,
                        max(1, int(bounded_timeout(route, attempt_timeout))),
                        route.user_agent,
                    )
                    if self.openai_runner is None:
                        text = call(
                            *call_args,
                            process_holder=self.process_holder,
                            process_key="process",
                            cancelled=lambda: bool(
                                self.process_holder.get("cancelled")
                            ),
                        )
                    else:
                        text = call(*call_args)
            else:
                payload = openai_fallback.build_payload(
                    self._system_prompt(route), request.prompt, model,
                    int(os.environ.get(
                        "OPENAI_FALLBACK_MAX_OUTPUT_TOKENS",
                        str(openai_fallback.DEFAULT_MAX_OUTPUT_TOKENS),
                    )),
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
            if self.process_holder.get("cancelled"):
                self.last_error_detail = "OpenAI fallback interrupted"
                return AdapterResult(
                    status="cancelled",
                    reason="process_interrupted",
                    effects_started=(None if request.allow_tools else False),
                )
            reason = provider_reason(error_text)
            self.last_error_detail = safe_error_detail(error_text)
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
        text = str(text or "").strip()
        if text and not looks_like_error(text):
            return AdapterResult(
                status="succeeded", text=text, observed_model=model,
            )
        return AdapterResult(
            status="ambiguous_failure",
            reason="empty_or_error_output",
            effects_started=(None if request.allow_tools else False),
        )
