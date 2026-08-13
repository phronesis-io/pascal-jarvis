"""Codex CLI execution rung for owner-initiated Lark conversations.

This route reuses the local ChatGPT/Codex login rather than an API key. Each
Lark conversation keeps one Codex thread so switching providers does not turn
every message into a context-free one-shot call.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.timeutil import now_local_str


DEFAULT_MODEL = "gpt-5.5"
DEFAULT_TIMEOUT = 300
_APP_BINARY = "/Applications/ChatGPT.app/Contents/Resources/codex"
_FALLBACK_BINARIES = (
    "~/.local/bin/codex",
    "/opt/homebrew/bin/codex",
    "/usr/local/bin/codex",
    _APP_BINARY,
)
_STALE_SESSION_MARKERS = (
    "session not found",
    "thread not found",
    "could not find thread",
    "no rollout found",
    "unknown session",
)
_AUTH_FAILURE_MARKERS = (
    "not logged in",
    "401 unauthorized",
    "authentication required",
    "login required",
    "missing bearer or basic authentication",
    "please run codex login",
)
_PRETURN_UNAVAILABLE_RE = re.compile(
    r"usage limit|purchase more credits|insufficient credits"
    r"|rate limit|too many requests"
    r"|model (?:is )?(?:currently )?unavailable|model not found|invalid model",
    re.IGNORECASE,
)
_SECRET_RE = re.compile(
    r"(sk-[A-Za-z0-9_\-]{8,}|Bearer\s+\S+"
    r"|\b(?:token|secret|api[_-]?key|password)\b\s*[=:]\s*\S+)",
    re.IGNORECASE,
)

CODEX_NOTICE = (
    "You are the Codex execution provider inside Jarvis. The user's request "
    "arrived through their private Lark conversation. Follow the current "
    "Jarvis context below. You may use local tools when needed, but keep tool "
    "use focused, preserve deterministic authority and confirmation rules, "
    "and return only the user-facing answer. Do not expose chain-of-thought."
)


class CodexFallbackError(RuntimeError):
    pass


class CodexUnavailableError(CodexFallbackError):
    """No executable item started, so another provider may run safely."""


class StaleSessionError(CodexFallbackError):
    pass


@dataclass(frozen=True)
class CliResult:
    text: str
    thread_id: str


def _db():
    from dashboard.db import get_db
    return get_db()


def resolve_codex_bin(configured: str = "") -> str:
    candidates = []
    explicit = configured or os.environ.get("CODEX_BIN", "")
    if explicit:
        candidates.append(os.path.expanduser(explicit))
    found = shutil.which("codex")
    if found:
        candidates.append(found)
    candidates.extend(os.path.expanduser(path) for path in _FALLBACK_BINARIES)
    for candidate in candidates:
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return ""


def build_command(
    binary: str,
    *,
    model: str,
    work_dir: str | Path,
    output_file: str | Path,
    thread_id: str = "",
    allow_tools: bool = True,
) -> list[str]:
    common = [
        "--json",
        "--skip-git-repo-check",
        "--ignore-user-config",
        "--ignore-rules",
        "-m",
        model,
        "-o",
        str(output_file),
    ]
    if thread_id:
        # Resume inherits the sandbox and approval policy from the original
        # thread. The subcommand intentionally exposes no -C/sandbox options.
        return [binary, "exec", "resume", *common, thread_id, "-"]
    command = [binary, "exec", "--color", "never"]
    if allow_tools:
        # Automatic approval review stays inside Codex's workspace-write
        # sandbox. Never use the dangerous bypass flag in the resident bot.
        command.append("--approve-for-me")
    else:
        command.extend(["--sandbox", "read-only"])
    command.extend([*common, "-C", str(work_dir), "-"])
    return command


def parse_thread_id(stdout: str) -> str:
    for line in str(stdout or "").splitlines():
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if event.get("type") == "thread.started" and event.get("thread_id"):
            return str(event["thread_id"])
    return ""


def parse_terminal_error(stdout: str) -> str:
    """Extract the authoritative terminal error from Codex NDJSON output."""
    message = ""
    for line in str(stdout or "").splitlines():
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "error":
            message = str(event.get("message") or message)
        elif event.get("type") == "turn.failed":
            error = event.get("error")
            if isinstance(error, dict):
                message = str(error.get("message") or message)
            elif error:
                message = str(error)
    return message


def _safe_error(value: object, limit: int = 320) -> str:
    parts = [line.strip() for line in str(value or "").splitlines() if line.strip()]
    line = parts[-1] if parts else ""
    return _SECRET_RE.sub("[redacted]", line)[:limit]


def _has_auth_failure(value: object) -> bool:
    lowered = str(value or "").lower()
    return any(marker in lowered for marker in _AUTH_FAILURE_MARKERS)


def _has_only_preturn_events(stdout: str) -> bool:
    """Return true when Codex emitted no event that could have changed state."""
    safe_events = {"thread.started", "turn.started", "error", "turn.failed"}
    for line in str(stdout or "").splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            # JSON mode should emit only structured events. Unknown text means
            # we cannot prove that the event stream is complete.
            return False
        if not isinstance(event, dict):
            return False
        event_type = str(event.get("type") or "")
        if event_type in safe_events:
            continue
        item = event.get("item") or {}
        if event_type == "item.completed" and item.get("type") == "error":
            continue
        # Unknown and executable item events are treated as possible side
        # effects. The caller must not replay the turn on another provider.
        return False
    return True


def _is_preturn_auth_failure(stdout: str, stderr: str) -> bool:
    """Backward-compatible narrow predicate used by boundary regressions."""
    return _has_auth_failure(f"{stdout}\n{stderr}") and _has_only_preturn_events(
        stdout
    )


def _is_preturn_unavailability(stdout: str, stderr: str) -> bool:
    """Prove another provider may retry because no executable item started."""
    terminal_error = parse_terminal_error(stdout)
    combined = f"{terminal_error}\n{stdout}\n{stderr}"
    if _has_auth_failure(combined):
        return _has_only_preturn_events(stdout)
    # Authentication can fail before the JSON stream exists. Other provider
    # failures need an authoritative terminal event, not an incidental stderr
    # warning, before replaying the request elsewhere.
    if not terminal_error or not _PRETURN_UNAVAILABLE_RE.search(terminal_error):
        return False
    return _has_only_preturn_events(stdout)


def ensure_codex_authenticated(binary: str, timeout: int = 5) -> None:
    """Fail safely before a model turn when the local Codex login is absent."""
    try:
        result = subprocess.run(
            [binary, "login", "status"],
            capture_output=True,
            text=True,
            timeout=max(1, int(timeout)),
            check=False,
        )
    except OSError as exc:
        raise CodexUnavailableError(
            f"Codex login status could not start: {type(exc).__name__}"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise CodexUnavailableError("Codex login status timed out") from exc
    if result.returncode != 0 and _has_auth_failure(
        f"{result.stdout}\n{result.stderr}"
    ):
        raise CodexUnavailableError("Codex CLI is not logged in")


def _terminate_process_group(process: subprocess.Popen[str] | None) -> None:
    if process is None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=0.5)
        return
    except (subprocess.TimeoutExpired, AttributeError):
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=0.5)
    except (subprocess.TimeoutExpired, AttributeError):
        pass


def invoke_codex(
    *,
    prompt: str,
    thread_id: str,
    model: str,
    timeout: int,
    work_dir: str | Path,
    binary: str,
    allow_tools: bool,
    process_holder: dict[str, Any] | None = None,
) -> CliResult:
    output_handle = tempfile.NamedTemporaryFile(
        prefix="jarvis-codex-", suffix=".txt", delete=False
    )
    output_path = Path(output_handle.name)
    output_handle.close()
    command = build_command(
        binary,
        model=model,
        work_dir=work_dir,
        output_file=output_path,
        thread_id=thread_id,
        allow_tools=allow_tools,
    )
    process: subprocess.Popen[str] | None = None
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(work_dir),
            start_new_session=True,
        )
        if process_holder is not None:
            process_holder["process"] = process
        try:
            stdout, stderr = process.communicate(
                input=prompt, timeout=max(1, int(timeout))
            )
        except subprocess.TimeoutExpired as exc:
            _terminate_process_group(process)
            raise CodexFallbackError(
                f"Codex CLI timed out after {max(1, int(timeout))}s"
            ) from exc
        try:
            text = output_path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            text = ""
        if process.returncode != 0:
            detail = (
                _safe_error(parse_terminal_error(stdout))
                or _safe_error(stderr)
                or _safe_error(stdout)
            )
            combined = f"{stdout}\n{stderr}"
            if any(marker in combined.lower() for marker in _STALE_SESSION_MARKERS):
                error_type = StaleSessionError
            elif _is_preturn_unavailability(stdout, stderr):
                error_type = CodexUnavailableError
            else:
                error_type = CodexFallbackError
            raise error_type(detail or f"Codex CLI exited {process.returncode}")
        if not text:
            raise CodexFallbackError("Codex CLI returned no final answer")
        return CliResult(text=text, thread_id=parse_thread_id(stdout) or thread_id)
    except OSError as exc:
        _terminate_process_group(process)
        raise CodexUnavailableError(
            f"Codex CLI could not start: {type(exc).__name__}"
        ) from exc
    finally:
        if process_holder is not None and process_holder.get("process") is process:
            process_holder["process"] = None
        output_path.unlink(missing_ok=True)


def load_session(conv_key: str) -> dict[str, str] | None:
    row = _db().execute(
        "SELECT * FROM codex_conversation_sessions WHERE conv_key = ?",
        (str(conv_key),),
    ).fetchone()
    return dict(row) if row else None


def save_session(conv_key: str, thread_id: str, model: str, work_dir: str) -> None:
    if not thread_id:
        return
    db = _db()
    db.execute(
        """INSERT INTO codex_conversation_sessions
           (conv_key, thread_id, model, work_dir, updated_at)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(conv_key) DO UPDATE SET
             thread_id=excluded.thread_id, model=excluded.model,
             work_dir=excluded.work_dir, updated_at=excluded.updated_at""",
        (
            str(conv_key), thread_id, model, work_dir,
            now_local_str("%Y-%m-%dT%H:%M:%S"),
        ),
    )
    db.commit()


def clear_session(conv_key: str) -> None:
    db = _db()
    db.execute(
        "DELETE FROM codex_conversation_sessions WHERE conv_key = ?",
        (str(conv_key),),
    )
    db.commit()


def compose_prompt(system_prompt: str, content: str) -> str:
    return (
        f"{CODEX_NOTICE}\n\n"
        "=== CURRENT JARVIS CONTEXT (authoritative for this turn) ===\n"
        f"{system_prompt.strip()}\n\n"
        "=== USER MESSAGE ===\n"
        f"{content.strip()}"
    )


def run_fallback(
    *,
    content: str,
    conv_key: str,
    system_prompt: str,
    model: str = DEFAULT_MODEL,
    timeout: int = DEFAULT_TIMEOUT,
    work_dir: str | Path,
    binary: str = "",
    allow_tools: bool = True,
    process_holder: dict[str, Any] | None = None,
) -> str:
    key = str(conv_key or "").strip()
    if not key:
        raise CodexFallbackError("conv-key is required")
    resolved = resolve_codex_bin(binary)
    if not resolved:
        raise CodexUnavailableError("Codex CLI not found")
    ensure_codex_authenticated(resolved)
    work = str(Path(work_dir).expanduser().resolve())
    saved = load_session(key)
    thread_id = ""
    if saved:
        if saved.get("model") == model and saved.get("work_dir") == work:
            thread_id = str(saved.get("thread_id") or "")
        else:
            clear_session(key)
    kwargs = {
        "prompt": compose_prompt(system_prompt, content),
        "thread_id": thread_id,
        "model": model,
        "timeout": timeout,
        "work_dir": work,
        "binary": resolved,
        "allow_tools": allow_tools,
        "process_holder": process_holder,
    }
    try:
        result = invoke_codex(**kwargs)
    except StaleSessionError:
        # A definitely missing local thread has performed no model turn, so a
        # single fresh retry is safe. All ambiguous failures remain fail-closed.
        clear_session(key)
        kwargs["thread_id"] = ""
        result = invoke_codex(**kwargs)
    save_session(key, result.thread_id, model, work)
    return result.text


def _read_optional(path: str) -> str:
    if not path:
        return ""
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def main(argv: list[str] | None = None) -> int:
    from core.config import Config

    config = Config()
    parser = argparse.ArgumentParser()
    parser.add_argument("--conv-key", required=True)
    parser.add_argument("--system-prompt-file", default=os.environ.get(
        "JV_SYSTEM_PROMPT_FILE", ""))
    parser.add_argument("--model", default=str(
        config.codex.get("fallback_model") or DEFAULT_MODEL))
    parser.add_argument("--timeout", type=int, default=int(
        config.codex.get("timeout") or DEFAULT_TIMEOUT))
    parser.add_argument("--work-dir", default=str(config.work_dir))
    parser.add_argument("--binary", default=str(config.codex.get("binary") or ""))
    parser.add_argument("--no-tools", action="store_true")
    args = parser.parse_args(argv)
    if not str(args.conv_key).strip():
        print("codex fallback error: conv-key is required", file=sys.stderr)
        return 2
    content = sys.stdin.read()
    holder: dict[str, Any] = {}
    previous_handlers: dict[int, Any] = {}

    def stop(signum, _frame):
        _terminate_process_group(holder.get("process"))
        raise SystemExit(128 + signum)

    for sig in (signal.SIGTERM, signal.SIGINT):
        previous_handlers[sig] = signal.signal(sig, stop)
    try:
        answer = run_fallback(
            content=content,
            conv_key=args.conv_key,
            system_prompt=_read_optional(args.system_prompt_file),
            model=args.model,
            timeout=max(1, args.timeout),
            work_dir=args.work_dir,
            binary=args.binary,
            allow_tools=not args.no_tools,
            process_holder=holder,
        )
    except CodexUnavailableError as exc:
        print(f"codex fallback error: {exc}", file=sys.stderr)
        return 75
    except CodexFallbackError as exc:
        print(f"codex fallback error: {exc}", file=sys.stderr)
        # The CLI may have executed tools before an interruption. A distinct
        # exit code tells bot.sh not to replay the request on another agent.
        return 74
    finally:
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)
    print(answer)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
