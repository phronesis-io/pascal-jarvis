"""OpenAI Responses API fallback with tool-use for main-chat outages.

When both primary and backup Claude Code paths are exhausted, this module
provides a GPT-based agentic fallback that can execute local tools (bash,
file read/write). It uses the OpenAI Responses API tool-call loop so Jarvis
retains operational capability (EigenFlux, Lark CLI, file ops) even when
Claude is completely unavailable.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable


DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-5.5"
DEFAULT_TIMEOUT = 120
DEFAULT_MAX_OUTPUT_TOKENS = 4096
MAX_TOOL_ROUNDS = 15
BASH_TIMEOUT = 60

FALLBACK_NOTICE = (
    "You are Jarvis running through an OpenAI agentic fallback because the "
    "primary Claude Code session failed. You have local tools: bash (for shell "
    "commands, eigenflux CLI, lark-cli, python), file_read, and file_write. "
    "Use them to fulfill the user's request. Keep tool use focused — prefer "
    "one well-crafted command over many small ones. The working directory is "
    "the Jarvis repo root."
)

FALLBACK_TEXT_NOTICE = (
    "You are Jarvis running through an OpenAI text fallback because the "
    "Claude-compatible routes failed. No local tools are available in this "
    "call. Use only the supplied system instructions and user input."
)

TOOLS = [
    {
        "type": "function",
        "name": "bash",
        "description": (
            "Execute a shell command and return stdout+stderr. "
            "Use for: eigenflux CLI, lark-cli, python scripts, git, etc. "
            "Commands run in the Jarvis repo root with a 60s timeout."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The shell command to execute",
                }
            },
            "required": ["command"],
        },
    },
    {
        "type": "function",
        "name": "file_read",
        "description": "Read the contents of a file (max 100KB returned).",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute or repo-relative file path",
                }
            },
            "required": ["path"],
        },
    },
    {
        "type": "function",
        "name": "file_write",
        "description": "Write content to a file (creates parent dirs).",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute or repo-relative file path",
                },
                "content": {
                    "type": "string",
                    "description": "Content to write",
                },
            },
            "required": ["path", "content"],
        },
    },
]


class OpenAIFallbackError(RuntimeError):
    pass


def _read_optional(path: str | None) -> str:
    if not path:
        return ""
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _resolve_path(path: str) -> Path:
    """Resolve a path relative to JARVIS_DIR if not absolute."""
    p = Path(path)
    if not p.is_absolute():
        jarvis_dir = os.environ.get("JARVIS_DIR", "")
        if jarvis_dir:
            p = Path(jarvis_dir) / p
    return p


# ── Tool executors ───────────────────────────────────────────────────────────

def _terminate_process_group(
    process: subprocess.Popen[str] | None,
    *,
    grace: float = 0.25,
) -> None:
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


def _exec_bash(
    args: dict,
    *,
    timeout: float | None = None,
    process_holder: dict[str, Any] | None = None,
    process_key: str = "tool",
    cancelled: Callable[[], bool] | None = None,
) -> str:
    cmd = args.get("command", "")
    if not cmd:
        return "error: empty command"
    cwd = os.environ.get("JARVIS_DIR") or str(Path(__file__).resolve().parent.parent)
    effective_timeout = (
        BASH_TIMEOUT if timeout is None else min(BASH_TIMEOUT, max(0.05, timeout))
    )
    process: subprocess.Popen[str] | None = None
    spawning_key = f"{process_key}:spawning"
    try:
        if process_holder is not None:
            process_holder[spawning_key] = True
        process = subprocess.Popen(
            ["bash", "-c", cmd],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=cwd,
            start_new_session=True,
        )
        if process_holder is not None:
            process_holder[process_key] = process
            process_holder[spawning_key] = False
        deadline = time.monotonic() + effective_timeout
        while True:
            if cancelled is not None and cancelled():
                _terminate_process_group(process)
                return "error: command cancelled"
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _terminate_process_group(process)
                return f"error: command timed out ({effective_timeout:g}s)"
            try:
                stdout, stderr = process.communicate(
                    timeout=min(0.1, remaining)
                )
                break
            except subprocess.TimeoutExpired:
                continue
        out = stdout + stderr
        if process.returncode:
            suffix = f"\n(exit {process.returncode})"
            return (out[:50000 - len(suffix)] + suffix) if out else suffix.lstrip()
        return out[:50000] or "(exit 0, no output)"
    except Exception as e:
        _terminate_process_group(process)
        return f"error: {e}"
    finally:
        if (
            process_holder is not None
            and process_holder.get(process_key) is process
        ):
            process_holder[process_key] = None
        if process_holder is not None:
            process_holder[spawning_key] = False


def _exec_file_read(args: dict) -> str:
    path = _resolve_path(args.get("path", ""))
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
        return content[:100000]
    except Exception as e:
        return f"error: {e}"


def _exec_file_write(args: dict) -> str:
    path = _resolve_path(args.get("path", ""))
    content = args.get("content", "")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return f"ok: wrote {len(content)} chars to {path}"
    except Exception as e:
        return f"error: {e}"


_EXECUTORS = {
    "bash": _exec_bash,
    "file_read": _exec_file_read,
    "file_write": _exec_file_write,
}


def execute_tool(
    name: str,
    arguments: str | dict,
    *,
    timeout: float | None = None,
    process_holder: dict[str, Any] | None = None,
    process_key: str = "tool",
    cancelled: Callable[[], bool] | None = None,
) -> str:
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
            return f"error: invalid JSON arguments: {arguments[:200]}"
    executor = _EXECUTORS.get(name)
    if not executor:
        return f"error: unknown tool '{name}'"
    if name == "bash":
        return _exec_bash(
            arguments,
            timeout=timeout,
            process_holder=process_holder,
            process_key=process_key,
            cancelled=cancelled,
        )
    return executor(arguments)


# ── API call ─────────────────────────────────────────────────────────────────

def _api_call(payload: dict[str, Any], api_key: str, base_url: str,
              timeout: float, user_agent: str = "") -> dict[str, Any]:
    import urllib.error
    import urllib.request
    url = base_url.rstrip("/") + "/responses"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    if user_agent:
        headers["User-Agent"] = user_agent
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:1000]
        raise OpenAIFallbackError(f"OpenAI HTTP {e.code}: {body}") from e
    except Exception as e:
        raise OpenAIFallbackError(f"OpenAI fallback failed: {e}") from e


def extract_text(response: dict[str, Any]) -> str:
    """Extract final text from a Responses API response body."""
    output_text = response.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text
    parts: list[str] = []
    for item in response.get("output", []) or []:
        if not isinstance(item, dict):
            continue
        for content in item.get("content", []) or []:
            if not isinstance(content, dict):
                continue
            text = content.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "\n".join(p for p in parts if p).strip()


def _extract_tool_calls(response: dict[str, Any]) -> list[dict]:
    """Extract function_call items from the response output."""
    calls = []
    for item in response.get("output", []) or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "function_call":
            calls.append(item)
    return calls


# ── Agentic loop ─────────────────────────────────────────────────────────────

def run_agentic(system_prompt: str, user_input: str, model: str,
                max_output_tokens: int, api_key: str, base_url: str,
                timeout: float, user_agent: str = "",
                process_holder: dict[str, Any] | None = None,
                process_key: str = "tool",
                cancelled: Callable[[], bool] | None = None) -> str:
    """Run a tool-use loop under one wall-clock deadline."""
    deadline = time.monotonic() + max(0.0, float(timeout))

    def remaining() -> float:
        if cancelled is not None and cancelled():
            raise OpenAIFallbackError("OpenAI agentic fallback cancelled")
        budget = deadline - time.monotonic()
        if budget <= 0:
            raise OpenAIFallbackError("OpenAI agentic fallback timed out")
        return budget

    instructions = FALLBACK_NOTICE
    if system_prompt.strip():
        instructions = f"{instructions}\n\n{system_prompt}"

    # Keep the continuation self-contained. Some OpenAI-compatible HTTP relays
    # reject previous_response_id (micuapi supports it only on Responses
    # WebSocket v2), while the Responses API also supports stateless function
    # calling by replaying the input items, function call, and tool output.
    input_items: list[dict[str, Any]] = [
        {"role": "user", "content": user_input},
    ]
    payload: dict[str, Any] = {
        "model": model,
        "instructions": instructions,
        "input": input_items,
        "tools": TOOLS,
        "max_output_tokens": max_output_tokens,
    }

    for _round in range(MAX_TOOL_ROUNDS):
        response = _api_call(
            payload, api_key, base_url, remaining(), user_agent
        )

        tool_calls = _extract_tool_calls(response)
        if not tool_calls:
            return extract_text(response)

        tool_results = []
        for tc in tool_calls:
            call_id = tc.get("call_id", tc.get("id", ""))
            name = tc.get("name", "")
            arguments = tc.get("arguments", "{}")
            result = execute_tool(
                name,
                arguments,
                timeout=remaining(),
                process_holder=process_holder,
                process_key=process_key,
                cancelled=cancelled,
            )
            print(f"  [tool] {name}: {result[:100]}",
                  file=sys.stderr)
            tool_results.append({
                "type": "function_call_output",
                "call_id": call_id,
                "output": result[:30000],
            })

        input_items.extend(
            item for item in (response.get("output", []) or [])
            if isinstance(item, dict)
        )
        input_items.extend(tool_results)
        payload = {
            "model": model,
            "instructions": instructions,
            "input": input_items,
            "tools": TOOLS,
            "max_output_tokens": max_output_tokens,
        }

    return extract_text(response) or "(max tool rounds reached)"


# ── Legacy non-agentic entry (kept for build_payload/call_openai compat) ─────

def build_payload(system_prompt: str, user_input: str, model: str,
                  max_output_tokens: int) -> dict[str, Any]:
    instructions = FALLBACK_TEXT_NOTICE
    if system_prompt.strip():
        instructions = f"{instructions}\n\n{system_prompt}"
    return {
        "model": model,
        "instructions": instructions,
        "input": [{"role": "user", "content": user_input}],
        "max_output_tokens": max_output_tokens,
    }


call_openai = _api_call


# ── CLI entry point ──────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--system-prompt-file",
                        default=os.environ.get("JV_SYSTEM_PROMPT_FILE", ""))
    parser.add_argument("--model",
                        default=os.environ.get("OPENAI_FALLBACK_MODEL")
                        or os.environ.get("OPENAI_MODEL")
                        or DEFAULT_MODEL)
    parser.add_argument("--base-url",
                        default=os.environ.get("OPENAI_BASE_URL",
                                               DEFAULT_BASE_URL))
    parser.add_argument("--user-agent",
                        default=os.environ.get("OPENAI_USER_AGENT", ""))
    parser.add_argument("--timeout", type=int,
                        default=int(os.environ.get("OPENAI_FALLBACK_TIMEOUT",
                                                   DEFAULT_TIMEOUT)))
    parser.add_argument("--max-output-tokens", type=int,
                        default=int(os.environ.get(
                            "OPENAI_FALLBACK_MAX_OUTPUT_TOKENS",
                            DEFAULT_MAX_OUTPUT_TOKENS)))
    parser.add_argument("--no-tools", action="store_true",
                        help="Disable tool use (legacy text-only mode)")
    args = parser.parse_args(argv)

    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        print("OPENAI_API_KEY is not set", file=sys.stderr)
        return 2

    user_input = sys.stdin.read()
    system_prompt = _read_optional(args.system_prompt_file)

    process_holder: dict[str, Any] = {
        "tool": None,
        "tool:spawning": False,
    }
    termination_signal = [0]

    def _terminate(signum, _frame):
        termination_signal[0] = signum
        _terminate_process_group(process_holder.get("tool"))
        if not process_holder.get("tool:spawning"):
            raise SystemExit(128 + signum)

    previous_handlers = {}
    for signum in (signal.SIGTERM, signal.SIGINT):
        previous_handlers[signum] = signal.signal(signum, _terminate)
    try:
        if args.no_tools:
            payload = build_payload(system_prompt, user_input, args.model,
                                    args.max_output_tokens)
            response = _api_call(payload, api_key, args.base_url,
                                 args.timeout, args.user_agent)
            text = extract_text(response)
        else:
            text = run_agentic(
                system_prompt, user_input, args.model,
                args.max_output_tokens, api_key, args.base_url,
                args.timeout, args.user_agent,
                process_holder=process_holder,
                cancelled=lambda: bool(termination_signal[0]),
            )
    except OpenAIFallbackError as e:
        if not termination_signal[0]:
            print(str(e), file=sys.stderr)
        return (
            128 + termination_signal[0]
            if termination_signal[0]
            else 1
        )
    finally:
        _terminate_process_group(process_holder.get("tool"))
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)

    if termination_signal[0]:
        return 128 + termination_signal[0]
    if not text:
        print("OpenAI fallback returned no text", file=sys.stderr)
        return 1
    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
