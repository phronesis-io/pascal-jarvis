#!/usr/bin/env python3
"""Full-event Lark sidecar — replaces `lark-cli event +subscribe` entirely.

Why: card.action.trigger needs a consumer on the app's long connection;
lark-cli (≤1.0.52, larksuite/cli#1051) doesn't consume it, and a SECOND
connection can't run alongside lark-cli (Feishu splits events randomly
across same-app connections, so im.message would be stolen). The only safe
shape is ONE connection that handles everything — this process.

Behavior:
- im.message.receive_v1 / message_read_v1 / reaction created+deleted →
  marshalled to NDJSON on stdout, same envelope shape bot.sh already parses
  ({schema, header.event_type, event...}).
- card.action.trigger → handled INLINE (3s-ACK by returning from the
  handler): feedback → engagement_log.jsonl; watchlater → watchlater_save;
  returns a toast. Not forwarded — bot.sh's card branch stays dormant.

Enable (after Pascal provides the App Secret from the dev console):
  1. put the secret in the environment:  export LARK_APP_SECRET=...
     (app id auto-read from `lark-cli config show`, or LARK_APP_ID)
  2. export JARVIS_EVENT_BACKEND=sidecar   (read by plugins/lark/client.sh)
  3. ./restart.sh --yes
Rollback: unset JARVIS_EVENT_BACKEND, restart — lark-cli path is untouched.
"""

from __future__ import annotations

import fcntl
import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

JARVIS_DIR = Path(os.environ.get("JARVIS_DIR",
                                 Path(__file__).resolve().parent.parent))

# Script execution puts scripts/ (not the repo root) at sys.path[0], and the
# cwd is never added — so the lazy `import core.*` below (memorial, and the
# long-latent core.intentions in _intent_close_payload) raises
# ModuleNotFoundError in production while pytest masks it (conftest inserts
# the rootdir). Bootstrap the repo root explicitly, same idiom as
# tasks/mail_triage_post.py.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ── Disconnect watchdog (audit 2026-07-10) ──────────────────────────
# The SDK's reconnect has known zombie paths — the endpoint-discovery POST
# has no timeout and can freeze the whole asyncio loop; a ClientException
# kills the orphaned receive task; a server-pushed finite ReconnectCount can
# run out — while the main coroutine (_select) keeps the process alive
# forever. Every supervisor (bot.sh 5s loop, components.yaml pgrep, daemon
# ps-match) only checks process EXISTENCE, so a dead link means Pascal's
# messages silently drop with zero alerts. Translate "link down" into the one
# signal supervision understands: process exit (bot.sh respawns within 5s).
#
# Calibration (verified 7/10): the server actually hands out
# ReconnectCount=-1 (infinite retry) and websockets pings every 20s, so SDK
# self-healing is the PRIMARY path — this watchdog is a backstop. Hence the
# generous ~10min threshold, and it only arms after the first successful
# connection: a cold start that can't connect (offline flight) must ride the
# SDK's infinite local retry instead of turning bot.sh's 5s loop into a
# restart storm.
DISCONNECT_EXIT_AFTER_S = 600
WATCHDOG_POLL_S = 15


class DisconnectWatchdog:
    """Decides when a once-connected process that lost its link should die."""

    def __init__(self, is_connected, exit_after_s: float = DISCONNECT_EXIT_AFTER_S,
                 clock=time.monotonic):
        self._is_connected = is_connected
        self._exit_after_s = exit_after_s
        self._clock = clock
        self._connected_once = False
        self._down_since: float | None = None

    def should_exit(self) -> bool:
        """One poll tick → True when the process should be replaced."""
        if self._is_connected():
            self._connected_once = True
            self._down_since = None
            return False
        if not self._connected_once:
            return False  # never connected: SDK's infinite retry owns this
        if self._down_since is None:
            self._down_since = self._clock()
            return False
        return self._clock() - self._down_since >= self._exit_after_s


def _redirect_sdk_logs_to_stderr():
    """Move lark_oapi's logger off stdout (audit 2026-07-10).

    The SDK logs to stdout (lark_oapi/core/log.py: StreamHandler(sys.stdout)),
    which (a) pollutes the NDJSON event pipe bot.sh consumes — jq drops the
    lines — and (b) hid every connect/reconnect message from jarvis.log, so a
    dead connection left zero forensics. client.sh already routes our stderr
    into LOG_FILE; pointing the SDK handlers there fixes both.
    """
    import logging
    try:
        from lark_oapi.core.log import logger as sdk_logger
    except ImportError:
        sdk_logger = logging.getLogger("Lark")
    for h in sdk_logger.handlers:
        if isinstance(h, logging.StreamHandler) \
                and getattr(h, "stream", None) is sys.stdout:
            h.setStream(sys.stderr)


def _app_id() -> str:
    if os.environ.get("LARK_APP_ID"):
        return os.environ["LARK_APP_ID"]
    try:
        r = subprocess.run(["lark-cli", "config", "show"],
                           capture_output=True, text=True, timeout=10)
        return json.loads(r.stdout).get("appId", "")
    except Exception:
        return ""


def _owner_id() -> str:
    value = os.environ.get("USER_ID", "").strip()
    if value:
        return value
    try:
        from core.config import Config
        return str(
            Config(JARVIS_DIR / "jarvis.yaml").lark.get("user_id", "") or ""
        ).strip()
    except Exception:
        return ""


def _owner_card_callback(data) -> bool:
    owner = _owner_id()
    operator = getattr(getattr(data, "event", None), "operator", None)
    if not owner or operator is None:
        return False
    return owner in {
        str(getattr(operator, field, "") or "").strip()
        for field in ("open_id", "user_id", "union_id")
    }


def _emit(obj_json: str):
    """One NDJSON line to stdout — bot.sh's while-read loop consumes it."""
    sys.stdout.write(obj_json.replace("\n", " ") + "\n")
    sys.stdout.flush()


def _record_feedback(value: dict):
    entry = {"ts": time.strftime("%Y-%m-%d %H:%M"),
             "source": str(value.get("source", "")), "type": "feedback",
             "rating": str(value.get("rating", "")), "epoch": int(time.time())}
    with open(JARVIS_DIR / "engagement_log.jsonl", "a", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _closure_confirmation_card(ok: bool, outcome: str = "done",
                               result_text: str = "") -> dict:
    """Persistent card body shown after a one-tap intent closure.

    Toasts disappear too quickly on mobile; returning a raw replacement card
    gives Pascal visible proof that the loop is closed.
    """
    title = "闭环已记录" if ok else "闭环无需重复记录"
    if ok:
        status = {
            "done": "已做了",
            "recorded": "已记录为没做/改天",
            "na": "已停止追踪",
        }.get(outcome, "已记录")
        body = f"✓ {status}"
        if result_text:
            body += f"\n\n{result_text}"
    else:
        body = "这条已经闭环过了，或者原始意图不存在。"
    return {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"content": title, "tag": "plain_text"}},
        "elements": [
            {"tag": "div", "text": {"content": body, "tag": "lark_md"}},
        ],
    }


def _intent_close_payload(value: dict) -> dict:
    """Close an intent from a card button and return a callback response dict."""
    intent_id = str(value.get("id", "")).strip()
    outcome = str(value.get("outcome", "done")).strip() or "done"
    result_text = str(value.get("result", "")).strip()
    try:
        from core.intent_closure import record_closure
        ok = record_closure(intent_id, outcome=outcome,
                            result=result_text, via="button")
    except Exception as e:
        print(f"intent_close failed: {e}", file=sys.stderr)
        ok = False
    return {
        "toast": {
            "type": "success" if ok else "info",
            "content": "闭环已记录 ✓" if ok else "已经闭环过了（或意图不存在）",
        },
        "card": {
            "type": "raw",
            "data": _closure_confirmation_card(ok, outcome, result_text),
        },
    }


def _handle_card_action(
        value: dict, *, owner_authenticated: bool = False) -> dict:
    """Dispatch one parsed card action; returns the callback response payload.

    REQ-81.3: each success path logs one stderr line — card taps previously
    left zero trace on success, so "点选无反应" reports (6/19) could neither
    be confirmed nor disproven from the sidecar log.
    """
    action = value.get("action", "")
    known_actions = {"feedback", "watchlater", "intent_close", "memorial"}
    if action not in known_actions:
        return {}
    if action and not owner_authenticated:
        print(
            f"card action rejected: unauthenticated operator action={action}",
            file=sys.stderr,
        )
        return {
            "toast": {
                "type": "info",
                "content": "这个操作只能由主人完成",
            }
        }
    if action == "feedback":
        _record_feedback(value)
        print(f"card feedback recorded: source={value.get('source', '')} "
              f"rating={value.get('rating', '')}", file=sys.stderr)
        return {"toast": {"type": "success", "content": "已记录"}}
    if action == "watchlater":
        out = subprocess.run(
            ["python3", str(JARVIS_DIR / "tasks" / "watchlater_save.py"),
             str(value.get("title", "")), str(value.get("url", "")), "button"],
            timeout=5, capture_output=True, text=True)
        dup = "已在" in (out.stdout or "")
        print(f"card watchlater saved: title={value.get('title', '')} "
              f"dup={dup}", file=sys.stderr)
        return {"toast": {"type": "success",
                          "content": "已在收藏列表里" if dup else "已收藏，空闲时提醒你"}}
    if action == "intent_close":
        payload = _intent_close_payload(value)
        print(f"card intent_close handled: id={value.get('id', '')} "
              f"outcome={value.get('outcome', 'done')}", file=sys.stderr)
        return payload
    if action == "memorial":
        # 奏折 batch route: every memorial card's buttons land here —
        # opt=="chat" opens a conversation, anything else is a 批红.
        # Lazy import like intent_close (works via the repo-root sys.path
        # bootstrap at the top of this file — cwd alone is NOT enough).
        mem_id = str(value.get("id", ""))
        opt = str(value.get("opt", ""))
        try:
            import core.memorial as memorial
            payload = (
                memorial.chat(mem_id)
                if opt == "chat"
                else memorial.decide(
                    mem_id,
                    opt,
                    owner_authenticated=owner_authenticated,
                )
            )
            print(f"card memorial handled: id={mem_id} opt={opt}", file=sys.stderr)
            return payload
        except Exception as e:
            print(f"card memorial failed: id={mem_id} opt={opt}: {e}",
                  file=sys.stderr)
            return {"toast": {"type": "info", "content": "出错了，直接在对话里告诉我"}}
    return {}


def main() -> int:
    try:
        import lark_oapi as lark
        from lark_oapi.event.callback.model.p2_card_action_trigger import (
            P2CardActionTriggerResponse,
        )
    except ImportError as e:
        print(f"lark_oapi missing/incompatible ({e}) — run ./scripts/python.sh -m pip install -U lark-oapi",
              file=sys.stderr)
        return 1

    app_id = _app_id()
    app_secret = os.environ.get("LARK_APP_SECRET", "")
    if not app_id or not app_secret:
        print("Need LARK_APP_SECRET (dev console → 凭证与基础信息) "
              "and app id (lark-cli config or LARK_APP_ID)", file=sys.stderr)
        return 1

    # Bound the SDK's synchronous, timeout-less requests.post (endpoint
    # discovery): on a middlebox that accepts TCP but never answers it would
    # otherwise block the whole asyncio loop — ping loop included — forever.
    # Only blocking sockets are affected; asyncio's non-blocking websockets
    # are untouched.
    socket.setdefaulttimeout(30)
    _redirect_sdk_logs_to_stderr()

    def forward(data):
        try:
            _emit(lark.JSON.marshal(data))
        except Exception as e:
            print(f"forward error: {e}", file=sys.stderr)

    def on_card(data) -> "P2CardActionTriggerResponse":
        try:
            value = (data.event.action.value or {}) if data.event and data.event.action else {}
            if isinstance(value, str):
                value = json.loads(value)
            return P2CardActionTriggerResponse(
                _handle_card_action(
                    value,
                    owner_authenticated=_owner_card_callback(data),
                )
            )
        except Exception as e:
            print(f"card handler error: {e}", file=sys.stderr)
        return P2CardActionTriggerResponse({})

    handler = (lark.EventDispatcherHandler.builder("", "")
               .register_p2_im_message_receive_v1(forward)
               .register_p2_im_message_message_read_v1(forward)
               .register_p2_im_message_reaction_created_v1(forward)
               .register_p2_im_message_reaction_deleted_v1(forward)
               .register_p2_card_action_trigger(on_card)
               .build())
    client = lark.ws.Client(app_id, app_secret, event_handler=handler,
                            log_level=lark.LogLevel.INFO)

    def _watchdog_loop():
        # `_conn` is set on connect and cleared to None by _disconnect() on
        # every loss — polling it from a thread survives even a frozen asyncio
        # loop (the endpoint-POST hang case). getattr keeps a future SDK
        # rename fail-safe: unknown attr → never fires.
        wd = DisconnectWatchdog(
            lambda: getattr(client, "_conn", None) is not None)
        while True:
            time.sleep(WATCHDOG_POLL_S)
            if wd.should_exit():
                print("disconnect watchdog: link down for "
                      f">{DISCONNECT_EXIT_AFTER_S}s after a successful "
                      "connect — exiting so bot.sh respawns a fresh process",
                      file=sys.stderr, flush=True)
                os._exit(1)

    threading.Thread(target=_watchdog_loop, daemon=True,
                     name="disconnect-watchdog").start()
    print("lark-event-sidecar connecting (single-connection mode)…", file=sys.stderr)
    client.start()  # blocks; SDK reconnects internally, watchdog backstops it
    return 0


if __name__ == "__main__":
    sys.exit(main())
