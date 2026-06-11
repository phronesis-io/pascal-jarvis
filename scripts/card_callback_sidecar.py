#!/usr/bin/env python3
"""Card-callback sidecar — READY BUT DORMANT (REQ-17).

lark-cli cannot consume card.action.trigger (larksuite/cli#1051), so card
buttons are disabled product-wide. This sidecar is the self-hosted
alternative: a lark_oapi websocket client that handles card callbacks
(returning from the handler answers within Feishu's 3s deadline) and appends
the action to engagement_log.jsonl in the same shape bot.sh's card-action
branch produces.

⚠️ DO NOT simply run this next to lark-cli: Feishu randomly splits events
across all long-connections of the same app (lark-cli's own --force help
text), so a second connection would steal im.message.receive_v1 from the
main bot. Enabling this requires migrating the WHOLE subscription here, or
waiting for upstream lark-cli support (self-diagnostic watches for that).

Enable checklist (full detail: docs/research/card_callback_root_cause.md):
  1. pip3 install lark-oapi
  2. 开发者后台 → 事件与回调 → 「回调配置」tab → 订阅方式=长连接 →
     添加 card.action.trigger → 发布新版本（只能 Pascal 在浏览器操作）
  3. EITHER upstream lark-cli adds card support (then delete this file)
     OR migrate lark_subscribe_messages to this sidecar (add an
     im.message.receive_v1 handler emitting the NDJSON bot.sh expects)
  4. Re-add the 收藏 button in tasks/content_recommend_post.py
  5. export LARK_APP_ID / LARK_APP_SECRET (from lark-cli config) and run:
     python3 scripts/card_callback_sidecar.py

Status: UNTESTED (lark_oapi not installed; cannot go live without step 2
anyway). Written against lark-oapi >= 1.3 documented API.
"""

import json
import os
import sys
import time
from pathlib import Path

JARVIS_DIR = Path(os.environ.get("JARVIS_DIR",
                                 Path(__file__).resolve().parent.parent))


def _record_feedback(action_value: dict):
    """Append a feedback/watchlater action to engagement_log.jsonl —
    identical shape to bot.sh's card-action branch."""
    entry = {
        "ts": time.strftime("%Y-%m-%d %H:%M"),
        "source": str(action_value.get("source", "")),
        "type": "feedback",
        "rating": str(action_value.get("rating", "")),
        "epoch": int(time.time()),
    }
    with open(JARVIS_DIR / "engagement_log.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def main() -> int:
    try:
        import lark_oapi as lark
    except ImportError:
        print("lark_oapi not installed — run: pip3 install lark-oapi", file=sys.stderr)
        return 1

    app_id = os.environ.get("LARK_APP_ID", "")
    app_secret = os.environ.get("LARK_APP_SECRET", "")
    if not app_id or not app_secret:
        print("Set LARK_APP_ID / LARK_APP_SECRET (see lark-cli config)", file=sys.stderr)
        return 1

    def on_card_action(data) -> dict:
        # Returning promptly from this handler IS the 3-second ACK.
        try:
            value = (data.event.action.value or {}) if data.event and data.event.action else {}
            if isinstance(value, str):
                value = json.loads(value)
            action = value.get("action", "")
            if action == "feedback":
                _record_feedback(value)
                return {"toast": {"type": "success", "content": "已记录"}}
            if action == "watchlater":
                import subprocess
                subprocess.run(
                    ["python3", str(JARVIS_DIR / "tasks" / "watchlater_save.py"),
                     str(value.get("title", "")), str(value.get("url", "")), "button"],
                    timeout=5, capture_output=True)
                return {"toast": {"type": "success", "content": "已收藏，空闲时提醒你"}}
        except Exception as e:  # never let a handler error break the ws loop
            print(f"card action handler error: {e}", file=sys.stderr)
        return {}

    handler = (lark.EventDispatcherHandler.builder("", "")
               .register_p2_card_action_trigger(on_card_action)
               .build())
    client = lark.ws.Client(app_id, app_secret, event_handler=handler,
                            log_level=lark.LogLevel.INFO)
    print("card-callback sidecar connecting…", file=sys.stderr)
    client.start()  # blocks
    return 0


if __name__ == "__main__":
    sys.exit(main())
