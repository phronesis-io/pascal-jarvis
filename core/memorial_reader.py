"""One-tap, receipt-backed full-text delivery for clipped Memorial cards.

The public facade remains ``core.memorial.read_full``.  This module owns the
background job and delivery receipts so the already-large Memorial facade does
not also absorb transport orchestration.  The facade module is injected as an
API object at call time, avoiding an import cycle while keeping existing state
and rendering hooks authoritative.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any

from core.delivery import DeliveryEnvelope, TransportResult, deliver
from core.timeutil import now_local_str


_jobs: set[str] = set()
_jobs_lock = threading.Lock()
_last_thread: threading.Thread | None = None


def current_thread() -> threading.Thread | None:
    return _last_thread


def finish_job(memorial_id: str) -> None:
    with _jobs_lock:
        _jobs.discard(memorial_id)


def _status_payload(api: Any, state: dict, content: str) -> dict:
    return {
        "toast": {"type": "info", "content": content},
        "card": {
            "type": "raw",
            "data": api._full_text_status_card(state, "📖 全文正在发送…"),
        },
    }


def _deliver_chunk(api: Any, chunk: dict, chat_id: str) -> bool:
    """Deliver one continuation chunk with a stable, retryable receipt."""
    memorial_id = str(chunk.get("memorial_id") or "")
    offset = int(chunk.get("expected_offset") or 0)
    transfer_id = str(chunk.get("transfer_id") or "legacy")
    reply = str(chunk.get("reply") or "")

    def transport(envelope, channel):
        sent = api._send_text(
            str(envelope.payload.get("text") or ""), chat_id)
        message_id = "" if sent is True else str(sent or "")
        return TransportResult(bool(sent), message_id)

    result = deliver(
        DeliveryEnvelope(
            source="memorial-full-text",
            kind="text",
            payload={"text": reply},
            attention="reply",
            requested_channel="lark",
            conversation_bound=True,
            chat_id=chat_id,
            memorial_id=memorial_id,
            dedup_key=(
                f"memorial-full-text:{memorial_id}:{transfer_id}:{offset}"
            ),
            metadata={
                "bypass_throttle": True,
                "bypass_quiet": True,
                "retry_existing": True,
                # Delivery also deduplicates by normalized content. Scope the
                # hash to this reading transfer so retries stay idempotent but
                # an intentional later re-read sends the text again.
                "dedup_text": f"{transfer_id}\0{reply}",
            },
        ),
        root=api.runtime_root(),
        transport=transport,
    )
    return result.state in {"delivered", "read", "acted"}


def _run(api: Any, memorial_id: str, conv_key: str, chat_id: str) -> None:
    """Send every remaining chunk, advancing only after confirmed delivery."""
    sent_chunks = 0
    completed = False
    try:
        while True:
            chunk = api.continue_chat_body(
                conv_key,
                lookup_keys=[chat_id],
                memorial_id=memorial_id,
                automatic=True,
            )
            if not chunk.get("handled") or chunk.get("awaiting_opener"):
                latest = api._latest_chat_continuation(
                    [conv_key, chat_id], memorial_id=memorial_id)
                completed = bool(latest and latest.get("done"))
                break
            if not _deliver_chunk(api, chunk, chat_id):
                break
            committed = api.commit_chat_continuation(
                conv_key,
                str(chunk.get("state_conv_key") or conv_key),
                memorial_id,
                int(chunk.get("expected_offset") or 0),
                int(chunk.get("next_offset") or 0),
                record_context=False,
            )
            if not committed:
                # A duplicate callback may have advanced the same receipt in
                # another process. Accept only evidence at or past this chunk.
                latest = api._latest_chat_continuation(
                    [conv_key, chat_id], memorial_id=memorial_id)
                if not latest or int(latest.get("offset") or 0) < int(
                        chunk.get("next_offset") or 0):
                    break
            sent_chunks += 1
            if int(chunk.get("remaining_chars") or 0) == 0:
                completed = True
                break
    except Exception as exc:
        api._ops_log(
            "full_text_delivery_failed",
            level="error",
            memorial_id=memorial_id,
            sent_chunks=sent_chunks,
            error_type=type(exc).__name__,
        )
    finally:
        finish_job(memorial_id)

    current = api.get_memorial(memorial_id)
    if current is not None:
        if completed:
            status = f"📖 全文已发送 · {api._hhmm(now_local_str())}"
        else:
            status = "⚠️ 全文发送中断 · 再点「查看全文」会从断点续传"
        api._sync_lark_card(
            memorial_id,
            api._full_text_status_card(current, status),
        )
        api._write_outbox(
            f"📖 「{current['title']}」全文"
            f"{'已发送' if completed else '发送中断'}（{sent_chunks} 段）"
        )
    api._ops_log(
        "full_text_delivery_complete" if completed
        else "full_text_delivery_incomplete",
        level="info" if completed else "warn",
        memorial_id=memorial_id,
        sent_chunks=sent_chunks,
    )


def read_full(memorial_id: str, *, api: Any) -> dict:
    """One tap sends all clipped text; a failed tap resumes from its receipt."""
    global _last_thread

    state = api.get_memorial(memorial_id)
    if state is None:
        return {"toast": {"type": "info",
                          "content": "这张卡对应的事项找不到了，直接在对话里告诉我"}}
    if not api.body_was_clipped(str(state.get("body") or "")):
        return {
            "toast": {"type": "info", "content": "这张卡已经显示完整"},
            "card": {"type": "raw", "data": api._render_card(state)},
        }

    conv_key = str(
        state.get("chat_id") or api._resolve_user_id() or "").strip()
    if not conv_key:
        return {"toast": {"type": "info",
                          "content": "暂时找不到对话窗口，直接回复我“查看全文”"}}

    with _jobs_lock:
        if memorial_id in _jobs:
            return _status_payload(api, state, "全文正在发送，不用重复点")
        latest = api._latest_chat_continuation(
            [conv_key, str(state.get("chat_id") or "")],
            memorial_id=memorial_id,
        )
        if latest and latest.get("awaiting_opener"):
            return _status_payload(api, state, "全文正在发送，不用重复点")
        if not latest or latest.get("done"):
            api._append_line(api._ledger_path(), {
                "ev": "chat_continuation",
                "id": memorial_id,
                "conv_key": conv_key,
                "offset": 0,
                "done": False,
                "awaiting_opener": False,
                "transfer_id": f"{time.time_ns()}:{os.getpid()}",
                "ts": now_local_str(),
                "epoch": int(time.time()),
            })
        _jobs.add(memorial_id)

    api._append_line(api._ledger_path(), {
        "ev": "full_text",
        "id": memorial_id,
        "ts": now_local_str(),
        "epoch": int(time.time()),
    })
    api._record_engagement({
        "source": state.get("source", "memorial"),
        "type": "feedback",
        "rating": "full_text",
    })
    try:
        _last_thread = threading.Thread(
            target=_run,
            args=(api, memorial_id, conv_key,
                  str(state.get("chat_id") or "")),
            daemon=True,
            name=f"memorial-full-text-{memorial_id[:8]}",
        )
        _last_thread.start()
    except Exception as exc:
        finish_job(memorial_id)
        api._ops_log(
            "full_text_worker_start_failed",
            level="error",
            memorial_id=memorial_id,
            error_type=type(exc).__name__,
        )
        return {
            "toast": {"type": "info",
                      "content": "全文发送没有启动，再点一次即可重试"},
            "card": {
                "type": "raw",
                "data": api._full_text_status_card(
                    state, "⚠️ 全文发送未启动 · 再点「查看全文」重试"),
            },
        }
    return {
        "toast": {"type": "success", "content": "开始发送全文，一次发完"},
        "deep_link": api.conversation_deep_link(state),
        "card": {
            "type": "raw",
            "data": api._full_text_status_card(state, "📖 全文正在发送…"),
        },
    }
