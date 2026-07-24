"""Conversation-to-Matter binding and deterministic Lark commands."""

from __future__ import annotations

import argparse
import json
import re
from urllib.parse import quote

from core.matters import (
    MatterConflict,
    add_event,
    create_matter,
    get_matter,
    link_entity,
    list_matters,
    update_matter,
)
from core.timeutil import now_local_str


def _db():
    from dashboard.db import get_db
    return get_db()


def _now() -> str:
    return now_local_str("%Y-%m-%dT%H:%M:%S")


def bind_conversation(conv_key: str, matter_id: str, channel: str = "lark",
                      destination_id: str = "", chat_type: str = "p2p",
                      thread_root_id: str = "", actor: str = "user") -> dict:
    if get_matter(matter_id, include_links=False, include_events=False) is None:
        raise KeyError(f"matter not found: {matter_id}")
    conv_key = str(conv_key or "").strip()
    if not conv_key:
        raise ValueError("conv_key is required")
    now = _now()
    db = _db()
    old = db.execute(
        "SELECT matter_id FROM matter_bindings WHERE conv_key = ?", (conv_key,)
    ).fetchone()
    db.execute(
        """INSERT INTO matter_bindings
           (conv_key, matter_id, channel, destination_id, chat_type,
            thread_root_id, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(conv_key) DO UPDATE SET
             matter_id=excluded.matter_id, channel=excluded.channel,
             destination_id=excluded.destination_id, chat_type=excluded.chat_type,
             thread_root_id=excluded.thread_root_id, updated_at=excluded.updated_at""",
        (conv_key, matter_id, channel, destination_id, chat_type,
         thread_root_id, now, now),
    )
    db.commit()
    link_entity(
        matter_id, "conversation", conv_key, provider=channel,
        title="飞书对话" if channel == "lark" else f"{channel} conversation",
        metadata={"destination_id": destination_id, "chat_type": chat_type,
                  "thread_root_id": thread_root_id},
        actor=actor, move=True,
    )
    if old and old["matter_id"] != matter_id:
        add_event(matter_id, "conversation_switched",
                  f"对话从 {old['matter_id']} 切换到此事项", actor=actor,
                  payload={"conv_key": conv_key, "from": old["matter_id"]})
    return get_binding(conv_key) or {}


def get_binding(conv_key: str) -> dict | None:
    row = _db().execute(
        "SELECT * FROM matter_bindings WHERE conv_key = ?", (str(conv_key),)
    ).fetchone()
    return dict(row) if row else None


def bindings_for_matter(matter_id: str) -> list[dict]:
    rows = _db().execute(
        "SELECT * FROM matter_bindings WHERE matter_id = ? ORDER BY updated_at DESC",
        (str(matter_id),),
    ).fetchall()
    return [dict(row) for row in rows]


def clear_binding(conv_key: str, actor: str = "user") -> bool:
    binding = get_binding(conv_key)
    if not binding:
        return False
    _db().execute("DELETE FROM matter_bindings WHERE conv_key = ?", (conv_key,))
    _db().commit()
    add_event(binding["matter_id"], "conversation_unbound", "飞书对话已退出事项",
              actor=actor, payload={"conv_key": conv_key})
    return True


def lark_deep_link(binding: dict | None) -> str:
    if not binding or binding.get("channel") != "lark":
        return ""
    destination = str(binding.get("destination_id") or "")
    if not destination:
        return ""
    field = "openChatId" if destination.startswith("oc_") else "openId"
    return f"https://applink.feishu.cn/client/chat/open?{field}={quote(destination)}"


def context_for_conversation(conv_key: str, max_chars: int = 8000) -> str:
    binding = get_binding(conv_key)
    if not binding:
        return ""
    from core.matter_context import build_context_bundle, render_context_markdown
    try:
        rendered = render_context_markdown(build_context_bundle(binding["matter_id"]))
    except KeyError:
        return ""
    return rendered[:max_chars]


def record_turn(conv_key: str, role: str, text: str, message_id: str = "",
                provider: str = "lark", model: str = "",
                session_id: str = "") -> bool:
    if role == "assistant" and (provider or model):
        db = _db()
        db.execute(
            """INSERT INTO conversation_runtime
               (conv_key, provider, model, session_id, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(conv_key) DO UPDATE SET provider=excluded.provider,
                 model=excluded.model, session_id=excluded.session_id,
                 updated_at=excluded.updated_at""",
            (conv_key, provider, model, session_id, _now()),
        )
        db.commit()
    binding = get_binding(conv_key)
    if not binding:
        return False
    clean = " ".join(str(text or "").split())[:600]
    if not clean:
        return False
    add_event(
        binding["matter_id"], f"conversation_{role}", clean,
        actor=provider, payload={"conv_key": conv_key, "message_id": message_id,
                                 "model": model, "session_id": session_id},
    )
    return True


def record_channel_message(matter_id: str, channel: str, message_id: str,
                           destination_id: str = "", role: str = "update",
                           state: str = "active", metadata: dict | None = None) -> dict:
    now = _now()
    db = _db()
    db.execute(
        """INSERT INTO matter_channel_messages
           (matter_id, channel, destination_id, message_id, role, state,
            metadata, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(channel, message_id) DO UPDATE SET
             matter_id=excluded.matter_id, destination_id=excluded.destination_id,
             role=excluded.role, state=excluded.state, metadata=excluded.metadata,
             updated_at=excluded.updated_at""",
        (matter_id, channel, destination_id, message_id, role, state,
         json.dumps(metadata or {}, ensure_ascii=False), now, now),
    )
    db.commit()
    row = db.execute(
        "SELECT * FROM matter_channel_messages WHERE channel=? AND message_id=?",
        (channel, message_id),
    ).fetchone()
    return dict(row)


def _match_command(content: str) -> tuple[str, str] | None:
    text = str(content or "").strip()
    match = re.match(r"^/(?:matter|事项)(?:\s+(.+))?$", text, re.I)
    if match:
        rest = (match.group(1) or "current").strip()
    else:
        match = re.match(r"^事项\s+(新建|切换|使用|当前|列表|退出|完成|交接)(?:\s+(.+))?$", text)
        if not match:
            return None
        rest = f"{match.group(1)} {(match.group(2) or '').strip()}".strip()
    parts = rest.split(maxsplit=1)
    return parts[0].lower(), parts[1].strip() if len(parts) > 1 else ""


def handle_lark_command(content: str, conv_key: str, destination_id: str = "",
                        chat_type: str = "p2p", actor: str = "user") -> dict:
    if str(content or "").strip().lower() in {
            "/model", "当前模型", "现在是什么模型", "你是什么模型"}:
        row = _db().execute(
            "SELECT * FROM conversation_runtime WHERE conv_key = ?", (conv_key,)
        ).fetchone()
        if row:
            state = dict(row)
            provider = state.get("provider") or "unknown"
            try:
                from core.provider_health import summary_text
                chain = "\n\n通道验真：\n" + summary_text()
            except Exception:
                chain = ""
            return {"handled": True, "reply": (
                f"上一条实际由 {provider} / {state.get('model') or 'unknown'} 回答。\n"
                f"记录时间：{state.get('updated_at', '')}{chain}"
            )}
        from core.config import Config
        try:
            from core.provider_health import summary_text
            chain = "\n\n通道验真：\n" + summary_text()
        except Exception:
            chain = ""
        model = Config().claude.get("main_model", "opus") or "opus"
        return {"handled": True, "reply": (
            f"这段对话还没有成功回复记录；当前首选通道是 "
            f"Claude primary / {model}。{chain}"
        )}
    parsed = _match_command(content)
    if not parsed:
        return {"handled": False}
    command, arg = parsed
    aliases = {"new": "new", "新建": "new", "use": "use", "switch": "use",
               "切换": "use", "使用": "use", "current": "current", "status": "current",
               "当前": "current", "list": "list", "列表": "list", "clear": "clear",
               "退出": "clear", "done": "done", "完成": "done", "handoff": "handoff",
               "交接": "handoff"}
    command = aliases.get(command, command)
    binding = get_binding(conv_key)
    if command == "new":
        if not arg:
            return {"handled": True, "reply": "请给事项一个名称，例如：/matter new 整理移动端入口"}
        matter = create_matter(arg, next_action="确认第一步", source="lark", actor=actor)
        bind_conversation(conv_key, matter["id"], destination_id=destination_id,
                          chat_type=chat_type, actor=actor)
        return {"handled": True, "reply": f"已建立并进入事项「{matter['title']}」\n{matter['id']}"}
    if command == "list":
        items = list_matters(status="active,waiting,blocked", limit=8)
        body = "\n".join(f"- {m['id']} · {m['title']} · {m.get('next_action') or '待定下一步'}"
                         for m in items) or "没有进行中的事项。"
        return {"handled": True, "reply": body}
    if command == "use":
        if not arg:
            return {"handled": True, "reply": "请提供 Matter ID，例如：/matter use mat_xxx"}
        matter = get_matter(arg, include_links=False, include_events=False)
        if not matter:
            return {"handled": True, "reply": f"找不到事项 {arg}"}
        bind_conversation(conv_key, arg, destination_id=destination_id,
                          chat_type=chat_type, actor=actor)
        return {"handled": True, "reply": f"已切换到「{matter['title']}」\n下一步：{matter.get('next_action') or '待定'}"}
    if command == "clear":
        clear_binding(conv_key, actor=actor)
        return {"handled": True, "reply": "这段飞书对话已退出当前事项。"}
    if not binding:
        return {"handled": True, "reply": "这段对话还没有事项。用 /matter new 名称 建立，或 /matter list 查看。"}
    matter = get_matter(binding["matter_id"])
    if not matter:
        return {"handled": True, "reply": "当前事项已不存在，请重新选择。"}
    if command == "current":
        return {"handled": True, "reply": (
            f"当前事项：{matter['title']}\n"
            f"状态：{matter['status']}\n"
            f"当前共识：{matter.get('summary') or '待整理'}\n"
            f"下一步：{matter.get('next_action') or '待定'}\n"
            f"{matter['id']}"
        )}
    if command == "done":
        try:
            update_matter(matter["id"], status="done", outcome=arg,
                          actor=actor, force=False)
        except MatterConflict as exc:
            titles = "、".join(item["title"] for item in exc.open_items[:4])
            return {"handled": True, "reply": f"还不能直接完成：{titles}。请先闭环，或在网页确认保留这些条目。"}
        return {"handled": True, "reply": f"事项「{matter['title']}」已完成。"}
    if command == "handoff":
        provider = arg.lower() if arg.lower() in {"claude", "codex"} else "codex"
        from core.matter_executor import prepare_handoff
        handoff = prepare_handoff(matter["id"], provider, actor="lark")
        return {"handled": True, "reply": (
            f"交接包已准备：{handoff['context_path']}\n"
            f"在仓库运行：{handoff['command']}"
        )}
    return {"handled": True, "reply": "支持：new / use / current / list / done / handoff / clear"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--content", default="")
    parser.add_argument("--conv-key", required=True)
    parser.add_argument("--destination-id", default="")
    parser.add_argument("--chat-type", default="p2p")
    parser.add_argument("--record-role", choices=("user", "assistant"))
    parser.add_argument("--message-id", default="")
    parser.add_argument("--model", default="")
    parser.add_argument("--provider", default="lark")
    parser.add_argument("--session-id", default="")
    args = parser.parse_args(argv)
    if args.record_role:
        result = {"recorded": record_turn(
            args.conv_key, args.record_role, args.content,
            args.message_id, provider=args.provider, model=args.model,
            session_id=args.session_id)}
    else:
        result = handle_lark_command(
            args.content, args.conv_key, args.destination_id, args.chat_type)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
