"""Matter workspace — one durable thread across Jarvis entry points."""

from __future__ import annotations

import json
from datetime import datetime

from nicegui import run, ui

from core.matters import (
    MatterConflict,
    get_matter,
    link_entity,
    unlink_entity,
    update_matter,
)
from core.continuity import (
    claim_handoff,
    complete_entity_handoffs,
    list_handoffs,
)
from core.matter_bridge import bindings_for_matter, lark_deep_link
from core.work_sessions import discover_sessions

from ..uiutil import add_dashboard_head, client_surface, dashboard_header


STATUS_LABELS = {
    "active": "推进中",
    "waiting": "等待中",
    "blocked": "受阻",
    "done": "已完成",
    "archived": "已归档",
}
KIND_LABELS = {
    "project": "项目",
    "decision": "决策",
    "research": "研究",
    "personal": "个人",
    "incident": "故障",
}
PROVIDER_LABELS = {
    "claude": "Claude Code",
    "codex": "Codex",
    "lark": "飞书",
    "eigenflux": "EigenFlux",
    "file": "文件",
    "git": "Git",
    "github": "GitHub",
    "url": "链接",
    "jarvis": "Jarvis",
    "user": "我",
    "api": "Jarvis API",
    "": "记录",
}
PROVIDER_ICONS = {
    "claude": "terminal",
    "codex": "code",
    "lark": "chat_bubble_outline",
    "eigenflux": "hub",
    "file": "description",
    "git": "account_tree",
    "github": "code",
    "url": "link",
    "jarvis": "memory",
    "user": "person_outline",
    "api": "sync_alt",
    "": "notes",
}
FIELD_LABELS = {
    "title": "名称",
    "summary": "当前共识",
    "next_action": "下一步",
    "outcome": "完成结果",
    "kind": "类型",
    "status": "状态",
    "priority": "优先级",
    "source": "来源",
    "closed_at": "完成时间",
}


def _short_time(value: str) -> str:
    if not value:
        return ""
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.astimezone().strftime("%m月%d日 %H:%M")
    except (TypeError, ValueError):
        return str(value)[:16].replace("T", " ")


def _status_class(status: str) -> str:
    return f"matter-status is-{status}"


@ui.page("/matters")
def matters_page():
    ui.navigate.to("/items")


def _timeline_nodes(matter: dict) -> list[dict]:
    nodes = []
    for link in matter.get("links", []):
        nodes.append({
            "kind": "link",
            "created_at": link.get("created_at", ""),
            "provider": link.get("provider", ""),
            "title": link.get("title") or link.get("entity_id", ""),
            "detail": link.get("metadata", {}),
            "link": link,
        })
    for event in matter.get("events", []):
        if event.get("event_type") == "link_added":
            continue
        summary = event.get("summary") or "事项有变化"
        if event.get("event_type") == "matter_updated" and event.get("payload"):
            fields = [FIELD_LABELS.get(field, field) for field in event["payload"]]
            summary = "更新了" + "、".join(fields)
        nodes.append({
            "kind": "event",
            "created_at": event.get("created_at", ""),
            "provider": event.get("actor", "jarvis"),
            "title": summary,
            "detail": event.get("payload", {}),
            "event": event,
        })
    return sorted(nodes, key=lambda item: item.get("created_at", ""), reverse=True)


def _session_detail(session: dict) -> str:
    parts = []
    workspace = session.get("workspace", "")
    if workspace:
        parts.append(workspace.rstrip("/").rsplit("/", 1)[-1])
    if session.get("model"):
        parts.append(session["model"])
    parts.append(_short_time(session.get("updated_at", "")))
    return " · ".join(part for part in parts if part)


@ui.page("/matters/{matter_id}")
def matter_detail_page(matter_id: str):
    add_dashboard_head()
    surface, actor = client_surface()
    initial = get_matter(matter_id)

    with ui.column().classes("jarvis-page"):
        dashboard_header("/matters", "事项", "所有入口共享一份当前共识、一条下一步和一条工作线。")
        if initial is None:
            ui.label("这个事项不存在，或已经被删除。").classes("empty-guidance")
            ui.link("返回事项", "/items").classes("jarvis-nav-link is-active")
            return
        if initial.get("status") in {"done", "archived"}:
            try:
                complete_entity_handoffs("matter", matter_id)
            except Exception:
                pass
        try:
            for handoff in list_handoffs(target_surface=surface, limit=100):
                if (handoff.get("entity_type") == "matter"
                        and handoff.get("entity_id") == matter_id):
                    claim_handoff(handoff["id"], surface=surface)
        except Exception:
            pass

        with ui.dialog() as edit_dialog, ui.card().classes("matter-dialog"):
            ui.label("整理事项").classes("matter-dialog-title")
            title_input = ui.input("事项名称", value=initial.get("title", "")).classes("w-full")
            summary_input = ui.textarea(
                "当前共识", value=initial.get("summary", "")).classes("w-full")
            next_input = ui.textarea(
                "明确下一步", value=initial.get("next_action", "")).classes("w-full")
            outcome_input = ui.textarea(
                "完成结果", value=initial.get("outcome", "")).classes("w-full")

            def save_edits():
                try:
                    update_matter(
                        matter_id,
                        title=title_input.value,
                        summary=summary_input.value,
                        next_action=next_input.value,
                        outcome=outcome_input.value,
                        actor="user",
                    )
                except (KeyError, ValueError) as exc:
                    ui.notify(str(exc), type="warning")
                    return
                edit_dialog.close()
                detail.refresh()
                ui.notify("事项已更新", type="positive")

            with ui.row().classes("matter-dialog-actions"):
                ui.button("保存", icon="save", on_click=save_edits).props(
                    "unelevated no-caps").classes("memorial-primary")
                ui.button("取消", on_click=edit_dialog.close).props(
                    "flat no-caps").classes("memorial-secondary")

        sessions_state = {"items": [], "loading": False, "error": ""}
        move_state = {"session": None}
        close_state = {"status": "done", "items": []}
        handoff_state = {"provider": "codex", "command": ""}

        def attach_session(session: dict, move: bool = False):
            try:
                link_entity(
                    matter_id,
                    "session",
                    session["session_id"],
                    provider=session["provider"],
                    title=session["title"],
                    metadata={
                        "workspace": session.get("workspace", ""),
                        "model": session.get("model", ""),
                        "started_at": session.get("started_at", ""),
                        "updated_at": session.get("updated_at", ""),
                    },
                    actor="user",
                    move=move,
                )
            except (KeyError, ValueError) as exc:
                ui.notify(str(exc), type="warning")
                return
            session_dialog.close()
            move_dialog.close()
            detail.refresh()
            ui.notify("会话已归入这个事项", type="positive")

        def request_attach(session: dict):
            linked_to = session.get("matter_id", "")
            if linked_to and linked_to != matter_id:
                move_state["session"] = session
                move_dialog.open()
                return
            attach_session(session)

        with ui.dialog() as move_dialog, ui.card().classes("matter-dialog matter-dialog-small"):
            ui.label("移动这段会话？").classes("matter-dialog-title")
            ui.label("这段会话已经属于另一个事项。移动后，原事项仍会保留一条移出记录。").classes(
                "section-note")
            with ui.row().classes("matter-dialog-actions"):
                ui.button(
                    "移到这里", icon="drive_file_move",
                    on_click=lambda: attach_session(move_state["session"], move=True)
                    if move_state["session"] else None,
                ).props("unelevated no-caps").classes("memorial-primary")
                ui.button("取消", on_click=move_dialog.close).props(
                    "flat no-caps").classes("memorial-secondary")

        with ui.dialog() as session_dialog, ui.card().classes("matter-dialog matter-session-dialog"):
            with ui.row().classes("w-full items-center justify-between"):
                ui.label("归入最近会话").classes("matter-dialog-title")
                ui.button(icon="close", on_click=session_dialog.close).props(
                    "flat round dense").tooltip("关闭")

            @ui.refreshable
            def session_choices():
                if sessions_state["loading"]:
                    with ui.row().classes("w-full items-center gap-3 matter-loading"):
                        ui.spinner(size="24px")
                        ui.label("正在读取最近会话…").classes("section-note")
                    return
                if sessions_state["error"]:
                    ui.label(sessions_state["error"]).classes("empty-guidance")
                    return
                if not sessions_state["items"]:
                    ui.label("最近 30 天没有找到 Claude Code 或 Codex 会话。").classes(
                        "empty-guidance")
                    return
                with ui.element("div").classes("session-picker-list"):
                    for session in sessions_state["items"]:
                        linked_to = session.get("matter_id", "")
                        is_here = linked_to == matter_id
                        with ui.element("div").classes(
                                "session-picker-row" + (" is-linked" if is_here else "")):
                            ui.icon(PROVIDER_ICONS.get(session["provider"], "terminal"),
                                    size="20px").classes("session-provider-icon")
                            with ui.column().classes("session-picker-copy"):
                                ui.label(session["title"]).classes("session-picker-title")
                                ui.label(
                                    f"{PROVIDER_LABELS.get(session['provider'], session['provider'])}"
                                    f" · {_session_detail(session)}").classes("session-picker-meta")
                            if is_here:
                                ui.icon("check_circle", size="20px").classes("session-linked-icon").tooltip(
                                    "已在这个事项中")
                            else:
                                label = "移入" if linked_to else "归入"
                                ui.button(label, icon="add_link",
                                          on_click=lambda s=session: request_attach(s)).props(
                                    "flat no-caps dense").classes("memorial-chat")

            session_choices()

        with ui.dialog() as handoff_dialog, ui.card().classes(
                "matter-dialog matter-dialog-small"):
            ui.label("执行交接").classes("matter-dialog-title")

            @ui.refreshable
            def handoff_dialog_body():
                provider = handoff_state["provider"]
                ui.label(PROVIDER_LABELS.get(provider, provider)).classes(
                    "section-kicker")
                ui.code(handoff_state["command"]).classes("matter-handoff-command")

            handoff_dialog_body()
            ui.button("关闭", on_click=handoff_dialog.close).props(
                "flat no-caps").classes("memorial-secondary")

        async def open_session_dialog():
            sessions_state.update(items=[], loading=True, error="")
            session_dialog.open()
            session_choices.refresh()
            try:
                sessions_state["items"] = await run.io_bound(
                    discover_sessions, "", 30, 24)
            except Exception as exc:  # noqa: BLE001 — keep the page usable
                sessions_state["error"] = f"暂时读不到会话：{exc}"
            finally:
                sessions_state["loading"] = False
                session_choices.refresh()

        async def copy_handoff(provider: str):
            from core.matter_executor import prepare_handoff
            try:
                handoff = prepare_handoff(matter_id, provider, actor="dashboard")
            except (KeyError, ValueError) as exc:
                ui.notify(str(exc), type="warning")
                return
            handoff_state.update(provider=provider, command=handoff["command"])
            script = f"""
            return await (async () => {{
              const text = {json.dumps(handoff["command"])};
              try {{
                if (navigator.clipboard) {{
                  await navigator.clipboard.writeText(text);
                  return 'ok';
                }}
              }} catch (_) {{}}
              const area = document.createElement('textarea');
              area.value = text;
              area.style.position = 'fixed';
              area.style.opacity = '0';
              document.body.appendChild(area);
              area.select();
              const copied = document.execCommand('copy');
              area.remove();
              return copied ? 'ok' : 'failed';
            }})();
            """
            try:
                copied = await ui.run_javascript(script, timeout=10)
            except Exception:
                copied = "failed"
            if copied == "ok":
                ui.notify(
                    f"{PROVIDER_LABELS[provider]} 启动命令已复制",
                    type="positive",
                )
            else:
                handoff_dialog_body.refresh()
                handoff_dialog.open()
            detail.refresh()

        def force_close():
            update_matter(matter_id, status=close_state["status"], actor="user",
                          force=True)
            close_dialog.close()
            detail.refresh()
            ui.notify(f"已设为{STATUS_LABELS[close_state['status']]}", type="positive")

        with ui.dialog() as close_dialog, ui.card().classes(
                "matter-dialog matter-dialog-small"):
            ui.label("还有事项没有闭环").classes("matter-dialog-title")

            @ui.refreshable
            def close_warning():
                ui.label("完成后这些记录仍会保留，但 Jarvis 不再把它们当作当前推进线。").classes(
                    "section-note")
                for item in close_state["items"][:8]:
                    ui.label(f"{item.get('title', item.get('entity_id', ''))} · "
                             f"{item.get('status', '')}").classes("matter-warning-item")

            close_warning()
            with ui.row().classes("matter-dialog-actions"):
                ui.button("仍然结束", icon="check", on_click=force_close).props(
                    "unelevated no-caps").classes("memorial-primary")
                ui.button("继续处理", on_click=close_dialog.close).props(
                    "flat no-caps").classes("memorial-secondary")

        def change_status(status: str):
            try:
                update_matter(matter_id, status=status, actor="user")
            except MatterConflict as exc:
                close_state.update(status=status, items=exc.open_items)
                close_warning.refresh()
                close_dialog.open()
                return
            except (KeyError, ValueError) as exc:
                ui.notify(str(exc), type="warning")
                return
            detail.refresh()
            ui.notify(f"已设为{STATUS_LABELS[status]}", type="positive")

        def detach_link(link_id: int):
            if unlink_entity(matter_id, link_id, actor="user"):
                detail.refresh()
                ui.notify("已从事项中移除", type="positive")
            else:
                ui.notify("这条关联已经不存在", type="warning")

        @ui.refreshable
        def detail():
            matter = get_matter(matter_id)
            if matter is None:
                ui.label("这个事项已经不存在。").classes("empty-guidance")
                return

            with ui.row().classes("matter-detail-heading"):
                with ui.column().classes("matter-detail-copy"):
                    with ui.row().classes("items-center gap-2"):
                        ui.label(KIND_LABELS.get(matter.get("kind", ""), "事项")).classes(
                            "matter-kind")
                        ui.label(STATUS_LABELS.get(matter.get("status", ""), "事项")).classes(
                            _status_class(matter.get("status", "active")))
                    ui.label(matter["title"]).classes("matter-detail-title")
                    if matter.get("summary"):
                        ui.label(matter["summary"]).classes("matter-detail-summary")
                with ui.row().classes("matter-detail-actions"):
                    bindings = bindings_for_matter(matter_id)
                    lark_url = next((lark_deep_link(item) for item in bindings
                                     if lark_deep_link(item)), "")
                    if lark_url:
                        with ui.link(target=lark_url, new_tab=False).classes(
                                "matter-action-link"):
                            ui.icon("chat_bubble_outline", size="18px")
                            ui.label("在飞书继续")
                    with ui.link(
                            target=f"/api/matters/{matter_id}/context?format=markdown",
                            new_tab=True).classes("matter-action-link"):
                        ui.icon("download", size="18px")
                        ui.label("交接包")
                    ui.button("归入会话", icon="add_link", on_click=open_session_dialog).props(
                        "outline no-caps").classes("memorial-secondary")
                    ui.button("整理", icon="edit", on_click=edit_dialog.open).props(
                        "outline no-caps").classes("memorial-secondary")

            with ui.element("section").classes("matter-next-band"):
                ui.label("接下来只做这一步").classes("matter-next-kicker")
                ui.label(matter.get("next_action") or "还没有明确下一步").classes(
                    "matter-next-action")

            with ui.element("section").classes("matter-executor-band"):
                ui.label("执行入口").classes("section-kicker")
                with ui.row().classes("matter-executor-actions"):
                    ui.button(
                        "交给 Codex", icon="code",
                        on_click=lambda: copy_handoff("codex"),
                    ).props("unelevated no-caps").classes("memorial-primary")
                    ui.button(
                        "交给 Claude Code", icon="terminal",
                        on_click=lambda: copy_handoff("claude"),
                    ).props("outline no-caps").classes("memorial-secondary")

            with ui.row().classes("matter-status-actions"):
                for status, label, icon in (
                        ("active", "推进", "play_arrow"),
                        ("waiting", "等待", "schedule"),
                        ("blocked", "受阻", "block"),
                        ("done", "完成", "check"),
                        ("archived", "归档", "archive")):
                    active = matter["status"] == status
                    ui.button(label, icon=icon,
                              on_click=lambda value=status: change_status(value)).props(
                        ("unelevated" if active else "flat") + " no-caps dense").classes(
                        "matter-status-button" + (" is-active" if active else ""))

            if matter.get("outcome"):
                with ui.element("section").classes("matter-outcome"):
                    ui.label("完成结果").classes("section-kicker")
                    ui.label(matter["outcome"]).classes("matter-detail-summary")

            with ui.column().classes("w-full gap-2"):
                ui.label("工作线").classes("section-title")
                ui.label("会话、交接和决定都按时间留在这里。").classes("section-note")
                nodes = _timeline_nodes(matter)
                if not nodes:
                    ui.label("还没有工作记录。可以先归入一段最近会话。").classes(
                        "empty-guidance")
                else:
                    with ui.element("div").classes("matter-spine"):
                        for node in nodes:
                            provider = node.get("provider", "")
                            provider_key = provider if provider in PROVIDER_LABELS else "jarvis"
                            with ui.element("div").classes("matter-spine-node"):
                                with ui.element("div").classes("matter-spine-marker"):
                                    ui.icon(PROVIDER_ICONS.get(provider_key, "notes"), size="17px")
                                with ui.element("div").classes("matter-spine-content"):
                                    with ui.row().classes("w-full items-start justify-between gap-3"):
                                        with ui.column().classes("gap-1 min-w-0"):
                                            ui.label(node["title"]).classes("matter-spine-title")
                                            ui.label(
                                                f"{PROVIDER_LABELS.get(provider_key, provider)} · "
                                                f"{_short_time(node.get('created_at', ''))}"
                                            ).classes("matter-spine-meta")
                                        if node["kind"] == "link":
                                            ui.button(
                                                icon="link_off",
                                                on_click=lambda link=node["link"]:
                                                    detach_link(link["id"]),
                                            ).props("flat round dense").classes(
                                                "matter-unlink").tooltip("从事项中移除")
                                    if node["kind"] == "link":
                                        link = node["link"]
                                        target = ""
                                        if link.get("provider") == "url":
                                            target = link.get("entity_id", "")
                                        elif (link.get("provider") == "file"
                                              and link.get("entity_type") == "artifact"):
                                            target = (f"/api/matters/{matter_id}/artifacts/"
                                                      f"{link['id']}")
                                        if target:
                                            ui.link("打开产物", target, new_tab=True).classes(
                                                "matter-artifact-link")
                                    metadata = node.get("detail", {})
                                    if node["kind"] == "link" and metadata:
                                        context = " · ".join(filter(None, [
                                            str(metadata.get("workspace", "")).rstrip("/").rsplit("/", 1)[-1],
                                            str(metadata.get("model", "")),
                                        ]))
                                        if context:
                                            ui.label(context).classes("matter-spine-detail")

        detail()
