"""Low-level Lark transport for memorial cards and chat openers."""

from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Callable, Sequence
from pathlib import Path

from core.card import strip_internal_fields
from core.lark_bot_transport import send_from_cli_args
from core.log import log


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
    args = list(args)
    try:
        if "--msg-type" in args and "--content" in args:
            kind_index = args.index("--msg-type") + 1
            content_index = args.index("--content") + 1
            if (kind_index < len(args) and content_index < len(args)
                    and args[kind_index] == "interactive"):
                args[content_index] = strip_internal_fields(
                    args[content_index]
                )
    except (json.JSONDecodeError, TypeError, ValueError):
        # Preserve the established invalid-payload behavior: the direct API
        # or CLI will reject it and the caller records a failed delivery.
        pass

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


def sync_card(
    memorial_id: str,
    card: dict,
    *,
    root: str | Path,
    runner: Callable[..., subprocess.CompletedProcess] | None = None,
    cli_runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    ops_log: Callable[..., None] = _ops_log,
) -> None:
    """Update every delivered copy, preferring keychain-independent bot auth."""
    try:
        from core.memorial_thread import sent_message_ids

        message_ids = sent_message_ids(memorial_id)
    except Exception as exc:
        ops_log(
            "thread_receipt_lookup_failed",
            level="warn",
            memorial_id=memorial_id,
            error_type=type(exc).__name__,
        )
        return
    if not message_ids:
        return
    data = json.dumps(
        {"content": json.dumps(card, ensure_ascii=False)}, ensure_ascii=False
    )
    for message_id in message_ids:
        if runner is None:
            from core.lark_bot_transport import update_card

            direct = update_card(
                message_id,
                json.dumps(card, ensure_ascii=False),
                root=root,
            )
            if direct.attempted:
                if not direct.ok:
                    ops_log(
                        "lark_card_sync_rejected",
                        level="warn",
                        memorial_id=memorial_id,
                        message_id=message_id,
                        error=direct.error,
                    )
                continue
        active_runner = runner or cli_runner
        try:
            result = active_runner(
                [
                    "lark-cli", "api", "PATCH",
                    f"/open-apis/im/v1/messages/{message_id}",
                    "--data", data, "--as", "bot",
                ],
                capture_output=True,
                text=True,
                timeout=12,
            )
            if result.returncode != 0:
                ops_log(
                    "lark_card_sync_rejected",
                    level="warn",
                    memorial_id=memorial_id,
                    message_id=message_id,
                    returncode=int(result.returncode),
                )
        except Exception as exc:
            ops_log(
                "lark_card_sync_failed",
                level="warn",
                memorial_id=memorial_id,
                message_id=message_id,
                error_type=type(exc).__name__,
            )
