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

import json
import os
import subprocess
import sys
import time
from pathlib import Path

JARVIS_DIR = Path(os.environ.get("JARVIS_DIR",
                                 Path(__file__).resolve().parent.parent))


def _app_id() -> str:
    if os.environ.get("LARK_APP_ID"):
        return os.environ["LARK_APP_ID"]
    try:
        r = subprocess.run(["lark-cli", "config", "show"],
                           capture_output=True, text=True, timeout=10)
        return json.loads(r.stdout).get("appId", "")
    except Exception:
        return ""


def _emit(obj_json: str):
    """One NDJSON line to stdout — bot.sh's while-read loop consumes it."""
    sys.stdout.write(obj_json.replace("\n", " ") + "\n")
    sys.stdout.flush()


def _record_feedback(value: dict):
    entry = {"ts": time.strftime("%Y-%m-%d %H:%M"),
             "source": str(value.get("source", "")), "type": "feedback",
             "rating": str(value.get("rating", "")), "epoch": int(time.time())}
    with open(JARVIS_DIR / "engagement_log.jsonl", "a", encoding="utf-8") as f:
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
        from core.intentions import record_closure
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


def main() -> int:
    try:
        import lark_oapi as lark
        from lark_oapi.event.callback.model.p2_card_action_trigger import (
            P2CardActionTriggerResponse,
        )
    except ImportError as e:
        print(f"lark_oapi missing/incompatible ({e}) — pip3 install -U lark-oapi",
              file=sys.stderr)
        return 1

    app_id = _app_id()
    app_secret = os.environ.get("LARK_APP_SECRET", "")
    if not app_id or not app_secret:
        print("Need LARK_APP_SECRET (dev console → 凭证与基础信息) "
              "and app id (lark-cli config or LARK_APP_ID)", file=sys.stderr)
        return 1

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
            action = value.get("action", "")
            if action == "feedback":
                _record_feedback(value)
                return P2CardActionTriggerResponse(
                    {"toast": {"type": "success", "content": "已记录"}})
            if action == "watchlater":
                out = subprocess.run(
                    ["python3", str(JARVIS_DIR / "tasks" / "watchlater_save.py"),
                     str(value.get("title", "")), str(value.get("url", "")), "button"],
                    timeout=5, capture_output=True, text=True)
                dup = "已在" in (out.stdout or "")
                return P2CardActionTriggerResponse(
                    {"toast": {"type": "success",
                               "content": "已在收藏列表里" if dup else "已收藏，空闲时提醒你"}})
            if action == "intent_close":
                return P2CardActionTriggerResponse(_intent_close_payload(value))
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
    print("lark-event-sidecar connecting (single-connection mode)…", file=sys.stderr)
    client.start()  # blocks; reconnects internally
    return 0


if __name__ == "__main__":
    sys.exit(main())
