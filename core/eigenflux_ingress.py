"""Polling and cache reconciliation for EigenFlux private messages.

The WebSocket stream is the low-latency path. This module is the deterministic
five-minute safety net required by the EigenFlux client contract: fetch unread
messages, scan the CLI's durable cache, deduplicate against local receipts, and
repair only terminal failures that prove no message left the machine.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from core import memorial
from core.ef_stream import load_seen, mark_seen
from core.ef_stream_loop import _safe_analysis_text, handle_pm_event
from core.log import log
from core.runtime_paths import database_path

HEALTH_FILE = "data/ef_ingress_health.json"
MAX_CACHE_AGE_S = 4 * 24 * 60 * 60
MAX_RECOVERIES_PER_RUN = 2
MAX_DELIVERIES_PER_RUN = 3
RECOVERY_COOLDOWN_S = 60 * 60
SAFE_NO_SEND_ERRORS = (
    "keychain access blocked",
    "keychain get failed",
)


@dataclass
class ReconcileResult:
    status: str = "ok"
    fetched: int = 0
    inspected: int = 0
    accepted: int = 0
    recovered: int = 0
    already_receipted: int = 0
    unresolved_failures: int = 0
    detail: str = ""


def _write_health(
    root: Path,
    result: ReconcileResult,
    *,
    now_epoch: float | None = None,
) -> dict:
    path = root / HEALTH_FILE
    try:
        previous = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        previous = {}
    observed = float(time.time() if now_epoch is None else now_epoch)
    payload = {
        "version": 1,
        "updated_epoch": observed,
        "last_success_epoch": (
            observed
            if result.status == "ok"
            else float(previous.get("last_success_epoch") or 0)
        ),
        **asdict(result),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    temporary.replace(path)
    return payload


def _messages(payload: dict) -> list[dict]:
    values = payload.get("messages")
    if not isinstance(values, list):
        data = payload.get("data")
        values = data.get("messages") if isinstance(data, dict) else []
    return [value for value in values or [] if isinstance(value, dict)]


def _normalize(message: dict) -> dict | None:
    msg_id = str(message.get("msg_id") or message.get("item_id") or "")
    content = str(message.get("content") or "").strip()
    if not msg_id or not content:
        return None
    return {
        **message,
        "msg_id": msg_id,
        "conv_id": str(message.get("conv_id") or ""),
        "sender_id": str(message.get("sender_id") or ""),
        "receiver_id": str(message.get("receiver_id") or ""),
        "sender_name": str(message.get("sender_name") or "Unknown agent"),
        "content": content,
        "created_at": int(message.get("created_at") or 0),
    }


def _event(message: dict) -> str:
    return json.dumps(
        {
            "type": "pm_push",
            "data": {
                "messages": [message],
                "next_cursor": str(message.get("msg_id") or ""),
            },
        },
        ensure_ascii=False,
    )


def _profile_agent_id(home: Path) -> str:
    for path in sorted((home / "servers").glob("*/profile.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            continue
        agent_id = payload.get("agent_id") or payload.get("id")
        if agent_id:
            return str(agent_id)
    return ""


def _cached_inbound(home: Path, *, now_epoch: float) -> list[dict]:
    own_id = _profile_agent_id(home)
    if not own_id:
        return []
    cutoff_ms = int((now_epoch - MAX_CACHE_AGE_S) * 1000)
    found: dict[str, dict] = {}
    for path in (home / "servers").glob("*/data/messages/*/agent-*.json"):
        try:
            values = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(values, list):
            continue
        for value in values:
            if not isinstance(value, dict):
                continue
            message = _normalize(value)
            if not message:
                continue
            if str(message.get("receiver_id") or "") != own_id:
                continue
            if int(message.get("created_at") or 0) < cutoff_ms:
                continue
            found[message["msg_id"]] = message
    return sorted(
        found.values(),
        key=lambda value: (int(value.get("created_at") or 0), value["msg_id"]),
    )


def _existing_memorial(states: list[dict], message: dict) -> dict | None:
    receipt = f"eigenflux:{message['msg_id']}"
    conv_id = str(message.get("conv_id") or "")
    sender_id = str(message.get("sender_id") or "")
    content = str(message.get("content") or "")
    content_probe = content[:160]
    candidates = []
    for state in states:
        if state.get("source") != "eigenflux":
            continue
        if str(state.get("dedup_key") or "") == receipt:
            return state
        try:
            context = json.loads(str(state.get("context") or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            context = {}
        external_ids = {
            str(value) for value in context.get("external_event_ids", [])
            if str(value or "")
        }
        if message["msg_id"] in external_ids:
            return state
        if conv_id and str(context.get("conv_id") or "") != conv_id:
            continue
        if sender_id and str(context.get("sender_id") or "") != sender_id:
            continue
        if content_probe and content_probe not in str(state.get("body") or ""):
            continue
        candidates.append(state)
    if not candidates:
        return None
    return max(candidates, key=lambda value: int(value.get("epoch") or 0))


def _delivery_rows(root: Path, memorial_id: str) -> list[dict]:
    path = database_path(root)
    if not path.exists() or not memorial_id:
        return []
    try:
        db = sqlite3.connect(path, timeout=5)
        db.row_factory = sqlite3.Row
        rows = db.execute(
            "SELECT * FROM delivery_envelopes WHERE memorial_id=? "
            "ORDER BY created_epoch",
            (memorial_id,),
        ).fetchall()
        return [dict(row) for row in rows]
    except sqlite3.Error:
        return []
    finally:
        if "db" in locals():
            db.close()


def _safe_no_send(row: dict) -> bool:
    error = str(row.get("last_error") or "").lower()
    return any(marker in error for marker in SAFE_NO_SEND_ERRORS)


def _recovery_blocked(rows: list[dict], now_epoch: float) -> bool:
    recovery_rows = []
    for row in rows:
        try:
            metadata = json.loads(str(row.get("metadata") or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            metadata = {}
        if metadata.get("recovery_reason"):
            recovery_rows.append(row)
    if len(recovery_rows) >= 3:
        return True
    if recovery_rows:
        latest = max(float(row.get("updated_epoch") or 0) for row in recovery_rows)
        return now_epoch - latest < RECOVERY_COOLDOWN_S
    return False


def _unsafe_legacy_card(state: dict) -> bool:
    return not _safe_analysis_text(str(state.get("body") or ""))


def _fetch_unread(runner, *, timeout: float = 30) -> list[dict]:
    from core.eigenflux_publish import resolve_eigenflux_bin

    binary = resolve_eigenflux_bin()
    if not binary:
        raise RuntimeError("EigenFlux CLI is not installed")
    completed = runner(
        [binary, "msg", "fetch", "--limit", "50", "-f", "json"],
        capture_output=True,
        text=True,
        timeout=timeout,
        stdin=subprocess.DEVNULL,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "fetch failed").strip()
        raise RuntimeError(detail[:240])
    try:
        payload = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError("EigenFlux fetch returned invalid JSON") from exc
    return [message for value in _messages(payload)
            if (message := _normalize(value)) is not None]


def reconcile_once(
    root: str | Path,
    *,
    runner=subprocess.run,
    eigenflux_home: str | Path | None = None,
    now_epoch: float | None = None,
    user_id: str = "",
) -> ReconcileResult:
    root = Path(root)
    now_epoch = float(time.time() if now_epoch is None else now_epoch)
    home = Path(
        eigenflux_home
        or os.environ.get("EIGENFLUX_HOME")
        or Path.home() / ".eigenflux"
    )
    result = ReconcileResult()
    try:
        fetched = _fetch_unread(runner)
    except Exception as exc:
        result.status = "error"
        result.detail = f"fetch failed: {type(exc).__name__}: {exc}"[:240]
        _write_health(root, result, now_epoch=now_epoch)
        log("ef-ingress", result.detail, level="warn")
        return result

    result.fetched = len(fetched)
    fetched_ids = {message["msg_id"] for message in fetched}
    combined = {message["msg_id"]: message for message in fetched}
    for message in _cached_inbound(home, now_epoch=now_epoch):
        combined.setdefault(message["msg_id"], message)
    messages = sorted(
        combined.values(),
        key=lambda value: (
            0 if value["msg_id"] in fetched_ids else 1,
            int(value.get("created_at") or 0),
            value["msg_id"],
        ),
    )
    states = memorial.list_memorials()
    seen_file = root / "eigenflux" / ".ef-seen"
    recoveries = 0
    delivery_attempts = 0

    for message in messages:
        result.inspected += 1
        existing = _existing_memorial(states, message)
        rows = _delivery_rows(root, str((existing or {}).get("id") or ""))
        latest = rows[-1] if rows else {}
        latest_state = str(latest.get("state") or "")

        if existing and existing.get("status") != "pending":
            mark_seen(seen_file, [message["msg_id"]])
            result.already_receipted += 1
            continue
        if existing and not rows and memorial.delivery_accepted(existing):
            mark_seen(seen_file, [message["msg_id"]])
            result.already_receipted += 1
            continue
        if latest_state in {
            "queued", "attempting", "delivered", "read", "acted", "suppressed",
        }:
            mark_seen(seen_file, [message["msg_id"]])
            result.already_receipted += 1
            continue
        if latest_state == "failed":
            if not _safe_no_send(latest) or _recovery_blocked(rows, now_epoch):
                result.unresolved_failures += 1
                continue
            if recoveries >= MAX_RECOVERIES_PER_RUN:
                result.unresolved_failures += 1
                continue
            if delivery_attempts >= MAX_DELIVERIES_PER_RUN:
                result.unresolved_failures += 1
                continue
            delivery_attempts += 1
            if _unsafe_legacy_card(existing):
                # Retire the failed transcript-bearing card before creating
                # the safe replacement. Otherwise memorial.create() reuses
                # the still-pending dedup key and redelivers the unsafe body.
                retired = memorial.lapse(
                    str(existing["id"]),
                    "EigenFlux 投递恢复已改用安全原文",
                )
                if not retired:
                    # The user may have acted between the snapshot and this
                    # recovery attempt. Treat that closure as authoritative.
                    mark_seen(seen_file, [message["msg_id"]])
                    result.already_receipted += 1
                    continue
                accepted = handle_pm_event(
                    _event(message),
                    user_id=user_id,
                    seen_file=seen_file,
                    jarvis_dir=root,
                    analyze=False,
                    force=True,
                )
            else:
                accepted = memorial.redeliver(
                    str(existing["id"]),
                    "keychain no-send incident recovered",
                )
                if accepted:
                    mark_seen(seen_file, [message["msg_id"]])
            if accepted:
                result.accepted += 1
                result.recovered += 1
                recoveries += 1
            else:
                result.unresolved_failures += 1
            continue

        if message["msg_id"] in set(load_seen(seen_file)):
            result.already_receipted += 1
            continue
        if delivery_attempts >= MAX_DELIVERIES_PER_RUN:
            result.unresolved_failures += 1
            continue
        delivery_attempts += 1
        accepted = handle_pm_event(
            _event(message),
            user_id=user_id,
            seen_file=seen_file,
            jarvis_dir=root,
            analyze=False,
        )
        if accepted:
            result.accepted += 1
        else:
            result.unresolved_failures += 1

    if result.unresolved_failures:
        result.status = "degraded"
        result.detail = (
            f"{result.unresolved_failures} message(s) lack a safe delivery receipt"
        )
    else:
        result.detail = (
            f"poll verified; inspected {result.inspected}, accepted "
            f"{result.accepted}, recovered {result.recovered}"
        )
    _write_health(root, result, now_epoch=now_epoch)
    return result


def main() -> int:
    root = Path(os.environ.get("JARVIS_DIR") or Path(__file__).parent.parent)
    result = reconcile_once(root, user_id=os.environ.get("USER_ID", ""))
    print(json.dumps(asdict(result), ensure_ascii=False, sort_keys=True))
    return 1 if result.status == "error" else 0


if __name__ == "__main__":
    raise SystemExit(main())
