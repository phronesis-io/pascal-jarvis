"""OpenAI Responses API fallback for main-chat outages.

This is not a Claude Code replacement: it has no local tools, no session JSONL,
and no ability to edit files. It is a last-resort text responder for account /
model-limit failures so Jarvis can still answer ordinary messages while Claude
recovers.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-5.2"
DEFAULT_TIMEOUT = 120
DEFAULT_MAX_OUTPUT_TOKENS = 4096

FALLBACK_NOTICE = (
    "You are Jarvis running through an OpenAI fallback because the primary "
    "Claude Code session failed with a model/account limit. You do not have "
    "Claude Code tools, local filesystem access, shell access, or persistent "
    "Claude session state in this fallback call. Answer conversational and "
    "reasoning requests normally. If the user asks you to inspect or modify "
    "local files, run commands, use tools, or continue a tool-heavy coding "
    "task, say briefly that the GPT fallback is active, has no local tools, "
    "and ask them to retry when the Claude Code path is available."
)


class OpenAIFallbackError(RuntimeError):
    pass


def _read_optional(path: str | None) -> str:
    if not path:
        return ""
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def build_payload(system_prompt: str, user_input: str, model: str,
                  max_output_tokens: int) -> dict[str, Any]:
    instructions = FALLBACK_NOTICE
    if system_prompt.strip():
        instructions = f"{instructions}\n\nPrimary Jarvis system prompt:\n{system_prompt}"
    return {
        "model": model,
        "instructions": instructions,
        "input": user_input,
        "max_output_tokens": max_output_tokens,
    }


def extract_text(response: dict[str, Any]) -> str:
    """Extract text from a Responses API response body."""
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


def call_openai(payload: dict[str, Any], api_key: str, base_url: str,
                timeout: int, user_agent: str = "") -> dict[str, Any]:
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--system-prompt-file",
                        default=os.environ.get("JV_SYSTEM_PROMPT_FILE", ""))
    parser.add_argument("--model",
                        default=os.environ.get("OPENAI_FALLBACK_MODEL")
                        or os.environ.get("OPENAI_MODEL")
                        or DEFAULT_MODEL)
    parser.add_argument("--base-url",
                        default=os.environ.get("OPENAI_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--user-agent",
                        default=os.environ.get("OPENAI_USER_AGENT", ""))
    parser.add_argument("--timeout", type=int,
                        default=int(os.environ.get("OPENAI_FALLBACK_TIMEOUT",
                                                   DEFAULT_TIMEOUT)))
    parser.add_argument("--max-output-tokens", type=int,
                        default=int(os.environ.get("OPENAI_FALLBACK_MAX_OUTPUT_TOKENS",
                                                   DEFAULT_MAX_OUTPUT_TOKENS)))
    args = parser.parse_args(argv)

    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        print("OPENAI_API_KEY is not set", file=sys.stderr)
        return 2

    user_input = sys.stdin.read()
    payload = build_payload(
        _read_optional(args.system_prompt_file),
        user_input,
        args.model,
        args.max_output_tokens,
    )
    try:
        response = call_openai(payload, api_key, args.base_url, args.timeout,
                               args.user_agent)
        text = extract_text(response)
    except OpenAIFallbackError as e:
        print(str(e), file=sys.stderr)
        return 1

    if not text:
        print("OpenAI fallback returned no text", file=sys.stderr)
        return 1
    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
