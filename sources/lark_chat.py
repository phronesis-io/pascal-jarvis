"""lark_chat adapter — incremental messages from a Lark group chat or DM.

Generalizes tasks/phronesis_monitor_pre.sh (whose chat id / person id were
hardcoded literals — PRD §3.2). Same lark-cli invocation, but chat_id,
exclude id and window all come from sources.yaml.
"""

from __future__ import annotations

import json
import subprocess
import time


MAX_SIGNALS_PER_RUN = 50
FIRST_RUN_LOOKBACK_MIN = 15


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(ts))


def _extract_text(msg: dict) -> str:
    """Message content is a JSON string like {"text": "..."} for text msgs."""
    raw = msg.get("body", {}).get("content") or msg.get("content") or ""
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict):
            if isinstance(obj.get("text"), str):
                return obj["text"]
            # rich text (post): concatenate text runs
            runs = []
            for block in (obj.get("content") or []):
                for piece in (block or []):
                    if isinstance(piece, dict) and piece.get("tag") == "text":
                        runs.append(piece.get("text", ""))
            if runs:
                return " ".join(runs)
    except (json.JSONDecodeError, ValueError, TypeError):
        pass
    return str(raw)[:500]


def collect(cfg: dict, state: dict) -> tuple[list[dict], dict]:
    chat_id = cfg.get("chat_id", "")
    if not chat_id:
        return [], {**state, "error_type": "crash"}
    exclude_open_id = cfg.get("exclude_open_id", "")
    now = time.time()
    start_iso = state.get("last_ts") or _iso(now - FIRST_RUN_LOOKBACK_MIN * 60)
    end_iso = _iso(now)

    try:
        r = subprocess.run(
            ["lark-cli", "im", "+chat-messages-list",
             "--chat-id", chat_id, "--start", start_iso, "--end", end_iso,
             "--sort", "asc", "--page-size", "50",
             "--as", cfg.get("as", "user")],
            capture_output=True, text=True, timeout=15,
        )
    except subprocess.TimeoutExpired:
        return [], {**state, "error_type": "timeout"}
    except Exception:
        return [], {**state, "error_type": "crash"}
    if r.returncode != 0:
        return [], {**state, "error_type": "network"}

    try:
        data = json.loads(r.stdout)
    except (json.JSONDecodeError, ValueError):
        return [], {**state, "error_type": "crash"}
    if not data.get("ok", True) and not data.get("data"):
        return [], {**state, "error_type": "auth"}

    signals = []
    messages = (data.get("data") or {}).get("messages") or data.get("messages") or []
    page_full = len(messages) >= MAX_SIGNALS_PER_RUN
    last_msg_iso = ""
    for msg in messages[:MAX_SIGNALS_PER_RUN]:
        sender = ((msg.get("sender") or {}).get("id")
                  or (msg.get("sender") or {}).get("sender_id", {}).get("open_id", ""))
        if exclude_open_id and sender == exclude_open_id:
            continue
        msg_id = msg.get("message_id") or msg.get("msg_id") or ""
        if not msg_id:
            continue
        try:
            ts = float(msg.get("create_time", 0)) / 1000.0 or now
        except (TypeError, ValueError):
            ts = now
        text = _extract_text(msg)
        last_msg_iso = _iso(ts)
        if not text.strip():
            continue
        signals.append({
            "event_id": msg_id,
            "ts": last_msg_iso,
            "title": text[:80],
            "summary": text[:200],
            "body": text[:2048],
            "url": "",
            "actor": {"raw": sender, "resolved": ""},
            "payload": {"chat_id": chat_id},
        })

    # Cursor advance: a FULL page means more messages may exist in the
    # window beyond what we fetched — advance only to the last processed
    # message's ts so the next pass resumes there (seen-store dedups the
    # boundary overlap). Unconditionally jumping to end_iso silently and
    # permanently dropped everything past the first page in a busy chat.
    next_cursor = (last_msg_iso or end_iso) if page_full else end_iso
    return signals, {"last_ts": next_cursor, "error_type": None}
