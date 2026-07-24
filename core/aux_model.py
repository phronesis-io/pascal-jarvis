"""Bounded provider routing for Jarvis auxiliary model calls.

Main Lark replies and heartbeat task execution keep their richer, stateful
runners.  This module covers the smaller call sites that previously followed
only Backup 1: background jobs, EigenFlux message analysis, and idle-noise
classification.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from core.claude_bin import resolve_claude_bin
from core.model_fallback import (
    fallback_for_stderr,
    gate,
    is_spend_limit,
    trip,
)
from core.safety import looks_like_error


Runner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class AuxiliaryModelResult:
    text: str = ""
    provider: str = ""
    model: str = ""
    attempted: tuple[str, ...] = ()


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


def _enabled(name: str, default: str = "true") -> bool:
    return os.environ.get(name, default).lower() == "true"


def _provider_candidates(
    model: str,
    gate_state: str,
) -> list[tuple[str, str, dict[str, str] | None]]:
    candidates: list[tuple[str, str, dict[str, str] | None]] = []
    if gate_state != "backup":
        candidates.append(("Claude primary", model, None))

    for suffix, label, default_enabled in (
        ("", "Claude backup", "true"),
        ("2", "Claude backup2", "false"),
    ):
        token = os.environ.get(f"CLAUDE_BACKUP{suffix}_AUTH_TOKEN", "")
        base_url = os.environ.get(f"CLAUDE_BACKUP{suffix}_BASE_URL", "")
        if not (
            _enabled(f"CLAUDE_BACKUP{suffix}_ENABLED", default_enabled)
            and token
            and base_url
        ):
            continue
        provider_model = (
            os.environ.get(f"CLAUDE_BACKUP{suffix}_MODEL") or model
        )
        env = os.environ.copy()
        env["ANTHROPIC_AUTH_TOKEN"] = token
        env["ANTHROPIC_BASE_URL"] = base_url
        candidates.append((label, provider_model, env))
    return candidates


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
) -> list[str]:
    if not base:
        return ["--no-session-persistence"]
    if "--resume" in base:
        return list(base)
    if "--session-id" in base and attempt_index:
        return ["--session-id", str(uuid.uuid4())]
    return list(base)


def _openai_result(
    system_prompt: str,
    prompt: str,
    *,
    allow_tools: bool,
    timeout: int,
    process_holder: dict[str, Any] | None = None,
    process_key: str = "model",
    cancelled: Callable[[], bool] | None = None,
) -> tuple[str, str]:
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not _enabled("OPENAI_FALLBACK_ENABLED") or not api_key:
        return "", ""

    from core import openai_fallback

    model = (
        os.environ.get("OPENAI_FALLBACK_MODEL")
        or os.environ.get("OPENAI_MODEL")
        or openai_fallback.DEFAULT_MODEL
    )
    base_url = os.environ.get(
        "OPENAI_BASE_URL", openai_fallback.DEFAULT_BASE_URL
    )
    max_tokens = int(
        os.environ.get(
            "OPENAI_FALLBACK_MAX_OUTPUT_TOKENS",
            str(openai_fallback.DEFAULT_MAX_OUTPUT_TOKENS),
        )
    )
    user_agent = os.environ.get("OPENAI_USER_AGENT", "")
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


def _openai_configured() -> bool:
    return bool(
        _enabled("OPENAI_FALLBACK_ENABLED")
        and os.environ.get("OPENAI_API_KEY")
    )


def run_auxiliary_model(
    prompt: str,
    *,
    root: str | Path,
    system_prompt: str = "",
    model: str = "opus",
    timeout: int = 120,
    allow_tools: bool = False,
    session_args: tuple[str, ...] = (),
    runner: Runner | None = None,
    process_holder: dict[str, Any] | None = None,
    process_key: str = "model",
    claude_bin: str = "",
    cancelled: Callable[[], bool] | None = None,
    work_dir: str | Path | None = None,
) -> AuxiliaryModelResult:
    """Run an auxiliary call through Claude primary/backups and then GPT.

    ``timeout`` is a total wall-clock budget. Provider failures advance without
    exposing their stdout or stderr as user content. A tripped primary gate is
    followed without taking the probe election.
    """
    root_path = Path(root)
    started = time.monotonic()
    attempted: list[str] = []
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

    try:
        attempt_index = 0
        candidates = _provider_candidates(model, gate_state)
        gpt_available = _openai_configured()
        for provider_index, (provider, initial_model, env) in enumerate(
            candidates
        ):
            if cancelled is not None and cancelled():
                return AuxiliaryModelResult(attempted=tuple(attempted))
            current_model = initial_model
            provider_models: set[str] = set()
            while current_model not in provider_models:
                if cancelled is not None and cancelled():
                    return AuxiliaryModelResult(attempted=tuple(attempted))
                remaining = timeout - (time.monotonic() - started)
                if remaining <= 0:
                    return AuxiliaryModelResult(
                        attempted=tuple(attempted)
                    )
                provider_models.add(current_model)
                attempted.append(f"{provider}:{current_model}")
                command = [
                    resolve_claude_bin(claude_bin),
                    "-p",
                    "--model",
                    current_model,
                    "--disable-slash-commands",
                ]
                if prompt_file:
                    command.extend(["--append-system-prompt-file", prompt_file])
                command.extend(_session_args(session_args, attempt_index))
                if allow_tools:
                    command.append("--dangerously-skip-permissions")
                else:
                    command.extend(
                        [
                            "--permission-mode",
                            "dontAsk",
                            "--tools",
                            "",
                            "--mcp-config",
                            '{"mcpServers":{}}',
                            "--strict-mcp-config",
                        ]
                    )
                attempt_index += 1
                later_routes = (
                    len(candidates) - provider_index - 1
                    + int(gpt_available)
                )
                attempt_timeout = (
                    remaining
                    if later_routes == 0
                    else max(0.05, remaining / (later_routes + 1))
                )
                try:
                    result = _invoke(
                        command,
                        prompt,
                        timeout=attempt_timeout,
                        cwd=str(work_dir or root_path),
                        env=env,
                        runner=runner,
                        process_holder=process_holder,
                        process_key=process_key,
                        cancelled=cancelled,
                    )
                except (subprocess.TimeoutExpired, OSError):
                    break
                if cancelled is not None and cancelled():
                    return AuxiliaryModelResult(attempted=tuple(attempted))

                text = (result.stdout or "").strip()
                error_text = "\n".join(
                    part
                    for part in (result.stderr or "", result.stdout or "")
                    if part
                )
                if (
                    result.returncode == 0
                    and text
                    and not looks_like_error(text, proactive=True)
                ):
                    return AuxiliaryModelResult(
                        text=text,
                        provider=provider,
                        model=current_model,
                        attempted=tuple(attempted),
                    )
                if provider == "Claude primary" and is_spend_limit(error_text):
                    try:
                        trip("spend_limit", root_path)
                    except Exception:
                        pass
                next_model = fallback_for_stderr(
                    current_model, error_text
                )
                if not next_model:
                    break
                current_model = next_model

        remaining = timeout - (time.monotonic() - started)
        if (
            remaining <= 0
            or not _openai_configured()
            or (cancelled is not None and cancelled())
        ):
            return AuxiliaryModelResult(attempted=tuple(attempted))
        attempted.append(
            "GPT fallback:"
            + (
                os.environ.get("OPENAI_FALLBACK_MODEL")
                or os.environ.get("OPENAI_MODEL")
                or "gpt-5.5"
            )
        )
        try:
            text, openai_model = _openai_result(
                system_prompt,
                prompt,
                allow_tools=allow_tools,
                timeout=max(1, int(remaining)),
                process_holder=process_holder,
                process_key=process_key,
                cancelled=cancelled,
            )
        except Exception:
            text, openai_model = "", ""
        if cancelled is not None and cancelled():
            return AuxiliaryModelResult(attempted=tuple(attempted))
        if text and not looks_like_error(text, proactive=True):
            return AuxiliaryModelResult(
                text=text,
                provider="GPT fallback",
                model=openai_model,
                attempted=tuple(attempted),
            )
        return AuxiliaryModelResult(attempted=tuple(attempted))
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
    args = parser.parse_args(argv)

    root = args.root or Path(__file__).resolve().parent.parent
    session: tuple[str, ...] = ()
    if args.resume:
        values = ["--resume", args.resume]
        if args.fork_session:
            values.append("--fork-session")
        session = tuple(values)
    elif args.session_id:
        session = ("--session-id", args.session_id)

    system_prompt = _read_optional(args.system_prompt_file)
    if args.consume_system_prompt_file and args.system_prompt_file:
        try:
            Path(args.system_prompt_file).unlink()
        except OSError:
            pass

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
            process_holder=process_holder,
            cancelled=lambda: bool(termination_signal[0]),
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
