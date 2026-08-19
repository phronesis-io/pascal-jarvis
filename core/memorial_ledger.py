"""Storage primitives for the memorial append-only event ledger.

This module deliberately owns no repository-root global.  The compatibility
facade in :mod:`core.memorial` resolves its runtime root on every call and
passes concrete paths here, so tests and alternate runtimes cannot leak
writes into the production repository.
"""

from __future__ import annotations

import fcntl
import json
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

from core.jsonl import read_jsonl


def ledger_path(root: Path) -> Path:
    return Path(root) / "memorials.jsonl"


def pending_merge_path(root: Path) -> Path:
    return Path(root) / "jobs" / "pending_merge.jsonl"


def outbox_path(root: Path) -> Path:
    return Path(root) / "heartbeat_outbox.jsonl"


@contextmanager
def ledger_lock(ledger: Path) -> Iterator[None]:
    """Exclude appenders while a ledger rotation replaces the file."""
    lock_path = ledger.parent / (ledger.name + ".lock")
    with open(lock_path, "a", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def append_line(path: Path, entry: dict) -> None:
    """Append one compact JSON event with cross-process serialization."""
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(entry, ensure_ascii=False) + "\n"
    if path.name == "memorials.jsonl":
        with ledger_lock(path):
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(line)
        return
    with open(path, "a", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        handle.write(line)


def fold(
    events: list[dict],
    *,
    default_attention: Callable[[str, list[dict], list[dict]], str],
    lapsed_status: str,
) -> dict[str, dict]:
    """Fold the event stream into ``{memorial_id: current_state}``."""
    states: dict[str, dict] = {}
    for event in events:
        memorial_id = str(event.get("id", ""))
        if not memorial_id:
            continue
        event_type = event.get("ev", "")
        if event_type == "create":
            options = event.get("options") or []
            extra_buttons = event.get("extra_buttons") or []
            states[memorial_id] = {
                "id": memorial_id,
                "ts": event.get("ts", ""),
                "epoch": event.get("epoch", 0),
                "source": str(event.get("source", "")),
                "title": str(event.get("title", "")),
                "body": str(event.get("body", "")),
                "options": options,
                "extra_buttons": extra_buttons,
                "recommend": event.get("recommend") or None,
                "authoring_protocol": bool(
                    event.get("authoring_protocol", False)),
                "work_receipt": str(event.get("work_receipt", "")),
                "authoring_audit_text": (
                    str(event.get("authoring_audit_text", ""))
                    if "authoring_audit_text" in event else None),
                "attention": str(
                    event.get("attention")
                    or default_attention(
                        str(event.get("source", "")), options, extra_buttons)
                ),
                "review_surface": str(event.get("review_surface", "")),
                "context": str(event.get("context", "")),
                "dedup_key": str(event.get("dedup_key", "")),
                "chat_id": str(event.get("chat_id", "")),
                "matter_id": str(event.get("matter_id", "")),
                "status": "pending",
                "lapsed_ts": "",
                "lapse_reason": "",
                "decided_opt": "",
                "decided_label": "",
                "decided_ts": "",
                "action_result": "",
                "resolved_label": "",
                "resolved_ts": "",
                "chat_ts": "",
                "confused_ts": "",
                "chat_epoch": 0,
                "delivery_status": "not_sent",
                "delivery_ts": "",
            }
        elif event_type == "decide":
            state = states.get(memorial_id)
            if state is not None and state["status"] in (
                    "pending", lapsed_status):
                state["status"] = "decided"
                state["lapsed_ts"] = ""
                state["lapse_reason"] = ""
                state["decided_opt"] = str(event.get("opt", ""))
                state["decided_label"] = str(event.get("label", ""))
                state["decided_ts"] = str(event.get("ts", ""))
                state["action_result"] = str(event.get("action_result", ""))
        elif event_type == "action_result":
            state = states.get(memorial_id)
            if state is not None:
                state["action_result"] = str(event.get("result", ""))
        elif event_type == "resolve":
            state = states.get(memorial_id)
            if state is not None:
                state["status"] = "decided"
                state["decided_opt"] = "__external__"
                state["decided_label"] = str(event.get("label", "已处理"))
                state["decided_ts"] = str(event.get("ts", ""))
                state["action_result"] = str(event.get("result", ""))
                state["resolved_label"] = str(event.get("label", "已处理"))
                state["resolved_ts"] = str(event.get("ts", ""))
        elif event_type == "confused":
            state = states.get(memorial_id)
            if state is not None:
                state["confused_ts"] = str(event.get("ts", ""))
        elif event_type == "lapse":
            state = states.get(memorial_id)
            if state is not None and state["status"] == "pending":
                state["status"] = lapsed_status
                state["lapsed_ts"] = str(event.get("ts", ""))
                state["lapse_reason"] = str(event.get("reason", ""))
        elif event_type == "chat":
            state = states.get(memorial_id)
            if state is not None:
                state["chat_ts"] = str(event.get("ts", ""))
                state["chat_epoch"] = event.get("epoch", 0)
        elif event_type == "delivery":
            state = states.get(memorial_id)
            if state is not None:
                state["delivery_status"] = str(event.get("status", "unknown"))
                state["delivery_ts"] = str(event.get("ts", ""))
        elif event_type == "reclassify":
            state = states.get(memorial_id)
            if state is not None:
                if event.get("attention"):
                    state["attention"] = str(event["attention"])
                if "review_surface" in event:
                    state["review_surface"] = str(event["review_surface"])
    return states


def get(
    root: Path,
    memorial_id: str,
    *,
    default_attention: Callable[[str, list[dict], list[dict]], str],
    lapsed_status: str,
) -> dict | None:
    return fold(
        read_jsonl(ledger_path(root)),
        default_attention=default_attention,
        lapsed_status=lapsed_status,
    ).get(str(memorial_id))


def current_status(root: Path, memorial_id: str) -> str:
    """Read only the folded lifecycle status without importing the facade.

    Infrastructure consumers such as delivery recovery need to know whether
    an Item is still pending, but importing ``core.memorial`` would reverse the
    dependency back into card rendering and transport. Attention is irrelevant
    to lifecycle folding, so a neutral default keeps this reader at the ledger
    boundary.
    """
    state = get(
        Path(root),
        memorial_id,
        default_attention=lambda _source, _options, _extra: "notice",
        lapsed_status="lapsed",
    )
    return str(state.get("status") or "") if state else ""


def list_all(
    root: Path,
    *,
    pending_only: bool,
    default_attention: Callable[[str, list[dict], list[dict]], str],
    lapsed_status: str,
) -> list[dict]:
    states = list(fold(
        read_jsonl(ledger_path(root)),
        default_attention=default_attention,
        lapsed_status=lapsed_status,
    ).values())
    if pending_only:
        states = [state for state in states if state["status"] == "pending"]
    return states
