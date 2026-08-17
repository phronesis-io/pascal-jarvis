"""Low-level Lark transport for memorial cards and chat openers."""

from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Callable, Sequence

from core.log import log
from core.lark_bot_transport import send_from_cli_args


def _ops_log(message: str, **fields) -> None:
    try:
        log("memorial", message, **fields)
    except Exception:
        pass


def send(
    args: Sequence[str],
    *,
    retries: bool = True,
    retry_delays: Sequence[float] = (2, 5),
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    sleeper: Callable[[float], None] = time.sleep,
) -> str:
    """Send through ``lark-cli`` and return its message id or a truthy marker.

    Every failed attempt emits a structured event without copying provider
    stderr, which can contain private payloads or credentials.
    """
    delays = (0, *retry_delays) if retries else (0,)
    for attempt, delay in enumerate(delays, start=1):
        if delay:
            sleeper(delay)
        direct = send_from_cli_args(args)
        if direct.attempted:
            if direct.ok:
                return direct.message_id
            _ops_log(
                "lark_bot_api_rejected",
                level="error",
                attempt=attempt,
                attempts=len(delays),
                reason=direct.error,
            )
            continue
        try:
            result = runner(
                ["lark-cli", "im", "+messages-send", *args,
                 "--as", "bot", "--json"],
                capture_output=True,
                text=True,
                timeout=15,
            )
        except subprocess.TimeoutExpired:
            _ops_log(
                "lark_send_timeout",
                level="error",
                attempt=attempt,
                attempts=len(delays),
            )
            continue
        except Exception as exc:
            _ops_log(
                "lark_send_exception",
                level="error",
                attempt=attempt,
                attempts=len(delays),
                error_type=type(exc).__name__,
            )
            continue
        if result.returncode != 0:
            _ops_log(
                "lark_send_rejected",
                level="error",
                attempt=attempt,
                attempts=len(delays),
                returncode=int(result.returncode),
            )
            continue
        try:
            data = json.loads(result.stdout).get("data") or {}
            message_id = (
                data.get("message_id")
                or (data.get("message") or {}).get("message_id")
                or next(
                    (item.get("message_id") for item in data.get("messages", [])
                     if isinstance(item, dict) and item.get("message_id")),
                    "",
                )
            )
        except (AttributeError, json.JSONDecodeError, TypeError, ValueError):
            message_id = ""
        return str(message_id) or "sent"
    return ""
