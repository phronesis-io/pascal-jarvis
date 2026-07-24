"""Unified user-facing item desk.

A visible item is a Memorial. Matter is an optional topic and Intent is a
timer attribute; neither competes for a separate top-level user concept.
"""

from __future__ import annotations

import time
from pathlib import Path

from nicegui import run, ui

from core import memorial
from core.continuity import (
    claim_handoff,
    complete_entity_handoffs,
    complete_handoff,
    create_handoff,
    list_handoffs,
)

from ..telemetry import memorial_states
from ..uiutil import (
    add_dashboard_head,
    client_surface,
    dashboard_header,
    guarded_refresh_timer,
    memorial_attention_rank,
    memorial_display_body,
    memorial_display_title,
    memorial_is_notice,
    memorial_is_pending,
    memorial_option_label,
    memorial_review_surface,
    memorial_surface_label,
    memorial_visible_options,
    source_label,
    surface_from_headers,
)

JARVIS_DIR = Path(__file__).parent.parent.parent


def _entity_url(handoff: dict) -> str:
    if handoff.get("entity_type") == "matter":
        return f"/matters/{handoff.get('entity_id', '')}"
    if handoff.get("entity_type") == "delegation":
        return f"/delegations/{handoff.get('entity_id', '')}"
    return f"/items/{handoff.get('entity_id', '')}"


def _live_intent(intent: dict) -> bool:
    return (
        str(intent.get("status", "")) in {"pending", "triggered"}
        or str(intent.get("closure_status", "")) == "awaiting"
    )


def _option_intent_ids(state: dict) -> set[str]:
    ids: set[str] = set()
    for option in state.get("options", []) or []:
        action = option.get("action") or {}
        if action.get("type") != "intent_close":
            continue
        params = action.get("params") or {}
        value = str(params.get("id") or action.get("id") or "").strip()
        if value:
            ids.add(value)
    for button in state.get("extra_buttons", []) or []:
        value = button.get("value") or {}
        if value.get("action") == "intent_close" and value.get("id"):
            ids.add(str(value["id"]))
    return ids


def _item_source_label(source: str) -> str:
    label = source_label(source)
    return "提醒" if label == "意图" else label


def enrich_items(states: list[dict], *, matters: list[dict] | None = None,
                 intents: list[dict] | None = None,
                 intent_topics: dict[str, str] | None = None) -> list[dict]:
    """Attach user-facing topic/timer attributes to folded Memorial states."""
    topic_names = {
        str(row.get("id", "")): str(row.get("title", "") or "")
        for row in (matters or [])
    }
    live_ids = {
        str(row.get("id", "")) for row in (intents or []) if _live_intent(row)
    }
    live_by_topic: dict[str, set[str]] = {}
    for intent_id, matter_id in dict(intent_topics or {}).items():
        if intent_id in live_ids:
            live_by_topic.setdefault(matter_id, set()).add(intent_id)

    enriched = []
    for source_state in states:
        state = dict(source_state)
        topic_id = str(state.get("matter_id", "") or "")
        timer_ids = _option_intent_ids(state) & live_ids
        timer_ids |= live_by_topic.get(topic_id, set())
        state["_topic_id"] = topic_id
        state["_topic_label"] = (
            topic_names.get(topic_id)
            or _item_source_label(state.get("source", ""))
        )
        state["_timer_ids"] = sorted(timer_ids)
        state["_has_timer"] = bool(timer_ids)
        enriched.append(state)
    enriched.sort(key=memorial_attention_rank, reverse=True)
    return enriched


def filter_items(items: list[dict], *, mode: str = "pending",
                 topic_id: str = "", time_window: str = "7d",
                 surface: str = "", now: float | None = None) -> list[dict]:
    now = time.time() if now is None else now
    cutoff = {
        "24h": now - 86400,
        "7d": now - 7 * 86400,
        "30d": now - 30 * 86400,
        "all": 0,
    }.get(time_window, now - 7 * 86400)
    selected = [
        state for state in items
        if float(state.get("epoch", 0) or 0) >= cutoff
    ]
    if mode == "pending":
        selected = [state for state in selected if memorial_is_pending(state)]
    elif mode == "notice":
        selected = [state for state in selected if memorial_is_notice(state)]
    elif mode == "decided":
        selected = [
            state for state in selected if state.get("status") == "decided"
        ]
    if topic_id:
        selected = [
            state for state in selected if state.get("_topic_id") == topic_id
        ]
    if surface:
        selected = [
            state for state in selected
            if memorial_review_surface(state) == surface
        ]
    return selected


def _load_context() -> tuple[list[dict], list[dict], dict[str, str]]:
    matters, intents, intent_topics = [], [], {}
    try:
        from core.matters import list_matters
        matters = list_matters(limit=300)
    except Exception:
        pass
    try:
        from core.intent_lifecycle import list_intents
        intents = list_intents(limit=500)
    except Exception:
        pass
    try:
        from dashboard.db import get_db
        rows = get_db().execute(
            "SELECT entity_id,matter_id FROM matter_links "
            "WHERE entity_type='intent' AND provider='jarvis'"
        ).fetchall()
        intent_topics = {
            str(row["entity_id"]): str(row["matter_id"]) for row in rows
        }
    except Exception:
        pass
    return matters, intents, intent_topics


def _load_item(memorial_id: str) -> dict | None:
    state = memorial.get_memorial(memorial_id)
    if state is None:
        return None
    matters, intents, intent_topics = _load_context()
    enriched = enrich_items(
        [state], matters=matters, intents=intents,
        intent_topics=intent_topics,
    )
    return enriched[0] if enriched else None


async def _send_handoff(state: dict, surface: str, actor: str) -> dict:
    target = "desktop" if surface == "mobile" else "mobile"
    return await run.io_bound(
        create_handoff,
        "memorial",
        state["id"],
        from_surface=surface,
        to_surface=target,
        title=memorial_display_title(state),
        matter_id=str(state.get("matter_id", "") or ""),
        created_by=actor,
    )


def _handoff_notice(result: dict, target: str) -> tuple[str, str]:
    if not result.get("created"):
        return (
            "已经在电脑接力区" if target == "desktop" else "已经发到手机",
            "info",
        )
    if target == "desktop":
        return "已放到电脑接力区", "positive"
    if result.get("delivery_state") == "delivered":
        return "已发到手机", "positive"
    return "已放到手机接力区；通知暂未送达", "warning"


def _render_handoff_band(surface: str, refresh) -> None:
    try:
        handoffs = list_handoffs(target_surface=surface, limit=8)
    except Exception:
        return
    if not handoffs:
        return

    def resume(handoff: dict):
        claim_handoff(handoff["id"], surface=surface)
        ui.navigate.to(_entity_url(handoff))

    def dismiss(handoff: dict):
        complete_handoff(handoff["id"])
        refresh()

    origin = "手机" if surface == "desktop" else "电脑"
    with ui.element("section").classes("continuity-band"):
        with ui.row().classes("w-full items-end justify-between gap-3"):
            with ui.column().classes("gap-1"):
                ui.label("跨端接力").classes("section-kicker")
                ui.label(f"从{origin}带过来的下一步").classes(
                    "continuity-title")
            ui.label(f"{len(handoffs)} 项").classes("continuity-count")
        with ui.column().classes("continuity-list"):
            for handoff in handoffs:
                with ui.element("div").classes("continuity-row"):
                    with ui.column().classes("continuity-copy"):
                        ui.label(
                            handoff.get("title") or "未命名事项"
                        ).classes("continuity-item-title")
                        ui.label(
                            "正在接着处理"
                            if handoff.get("status") == "claimed"
                            else "等待继续"
                        ).classes("continuity-meta")
                    ui.button(
                        "接着处理", icon="arrow_forward",
                        on_click=lambda item=handoff: resume(item),
                    ).props("flat no-caps").classes("memorial-chat")
                    ui.button(
                        icon="close",
                        on_click=lambda item=handoff: dismiss(item),
                    ).props("flat round dense").tooltip("移出接力区")


def _render_delegation_band() -> None:
    """A compact shared-state view; decisions still live in the Item list."""
    try:
        from core.delegations import DelegationStore, TERMINAL_STATUSES
        rows = DelegationStore().list(limit=50)
    except Exception:
        return
    active = [row for row in rows if row["status"] not in TERMINAL_STATUSES]
    needs_user = [
        row
        for row in active
        if row["status"] in {"needs_user", "needs_clarification"}
    ]
    if not active:
        return
    with ui.element("section").classes(
        "w-full border-b border-grey-3 pb-4 mb-2"
    ):
        with ui.row().classes("w-full items-center justify-between gap-3"):
            with ui.column().classes("gap-1"):
                ui.label("Jarvis 正在承担").classes("section-kicker")
                ui.label(
                    f"{len(active)} 项进行中"
                    + (f" · {len(needs_user)} 项需要你" if needs_user else "")
                ).classes("text-sm font-medium")
            ui.button(
                icon="arrow_forward",
                on_click=lambda: ui.navigate.to("/delegations"),
            ).props("flat round dense").tooltip("查看委托进展")


@ui.page("/items")
def items_page():
    add_dashboard_head()
    surface, actor = client_surface()
    filters = {
        "mode": "pending",
        "topic": "",
        "time": "all",
        "surface": "",
        "limit": 12,
    }

    with ui.column().classes("jarvis-page"):
        dashboard_header(
            "/items", "事项",
            "需要你管的都在这里；主题与定时提醒只作为上下文。",
        )
        _render_delegation_band()

        @ui.refreshable
        def board():
            matters, intents, intent_topics = _load_context()
            all_items = enrich_items(
                memorial_states(JARVIS_DIR),
                matters=matters,
                intents=intents,
                intent_topics=intent_topics,
            )
            counts = {
                mode: len(filter_items(
                    all_items, mode=mode, time_window="all"))
                for mode in ("pending", "notice", "decided", "all")
            }
            _render_handoff_band(surface, board.refresh)

            def change(key: str, value: str):
                filters[key] = value
                if key == "mode":
                    filters["time"] = (
                        "all" if value == "pending" else "7d")
                filters["limit"] = 12
                board.refresh()

            with ui.row().classes("w-full items-center gap-2 item-filter-row"):
                for value, label in (
                    ("pending", "待批"),
                    ("notice", "知会"),
                    ("decided", "已批"),
                    ("all", "全部"),
                ):
                    active = filters["mode"] == value
                    ui.button(
                        f"{label} {counts[value]}",
                        on_click=lambda selected=value: change("mode", selected),
                    ).props(
                        ("unelevated" if active else "outline") + " no-caps"
                    ).classes(
                        "filter-chip "
                        + ("memorial-primary" if active
                           else "memorial-secondary")
                    )

            topic_options = {"": "全部主题"}
            topic_options.update({
                str(row.get("id", "")): str(
                    row.get("title", "") or "未命名主题")
                for row in matters if row.get("id")
            })
            with ui.row().classes("w-full items-center gap-3 item-filter-row"):
                ui.select(
                    topic_options, value=filters["topic"], label="主题",
                    on_change=lambda event: change(
                        "topic", event.value or ""),
                ).props("dense outlined options-dense").classes(
                    "item-filter-select")
                ui.select(
                    {"24h": "24 小时", "7d": "7 天", "30d": "30 天",
                     "all": "全部时间"},
                    value=filters["time"], label="时间",
                    on_change=lambda event: change("time", event.value),
                ).props("dense outlined options-dense").classes(
                    "item-filter-select")
                if filters["mode"] == "pending":
                    ui.select(
                        {"": "全部入口", "phone": "手机集中批",
                         "lark": "飞书即时批"},
                        value=filters["surface"], label="处理入口",
                        on_change=lambda event: change(
                            "surface", event.value or ""),
                    ).props("dense outlined options-dense").classes(
                        "item-filter-select")

            selected = filter_items(
                all_items, mode=filters["mode"],
                topic_id=filters["topic"],
                time_window=filters["time"],
                surface=(filters["surface"]
                         if filters["mode"] == "pending" else ""),
            )
            shown = selected[:filters["limit"]]
            if not shown:
                ui.label("当前筛选下没有需要处理的事项。").classes(
                    "empty-guidance")
                return

            def decide(mid: str, key: str):
                payload = memorial.decide(mid, key)
                toast = payload.get("toast", {})
                ui.notify(
                    toast.get("content", "已记录"),
                    type=("positive"
                          if toast.get("type") == "success" else "info"),
                )
                board.refresh()

            def chat(mid: str):
                payload = memorial.chat(mid)
                deep_link = str(payload.get("deep_link", "") or "")
                if deep_link:
                    ui.navigate.to(deep_link)
                else:
                    ui.notify("飞书入口暂不可用", type="warning")

            async def handoff(state: dict):
                result = await _send_handoff(state, surface, actor)
                target = "desktop" if surface == "mobile" else "mobile"
                message, tone = _handoff_notice(result, target)
                ui.notify(message, type=tone)
                board.refresh()

            with ui.element("div").classes("memorial-grid item-grid"):
                for state in shown:
                    decided_state = state.get("status") == "decided"
                    card_classes = (
                        "memorial-card item-card"
                        + (" is-decided" if decided_state else "")
                    )
                    with ui.card().classes(card_classes):
                        with ui.row().classes(
                                "w-full items-center justify-between gap-3"):
                            with ui.row().classes("items-center gap-2"):
                                if state.get("_topic_id"):
                                    ui.link(
                                        state["_topic_label"],
                                        f"/matters/{state['_topic_id']}",
                                    ).classes("memorial-source item-topic")
                                else:
                                    ui.label(state["_topic_label"]).classes(
                                        "memorial-source")
                                ui.label(memorial_surface_label(state)).classes(
                                    "memorial-surface")
                                if state.get("_has_timer"):
                                    with ui.element("span").classes(
                                            "item-timer"):
                                        ui.icon("schedule", size="14px")
                                        ui.label("有定时提醒")
                            ui.label(state.get("ts", "")).classes(
                                "memorial-time")
                        ui.label(memorial_display_title(state)).classes(
                            "memorial-title")
                        ui.markdown(
                            memorial_display_body(state)[:700]
                        ).classes("memorial-body")
                        if decided_state:
                            ui.label(
                                "已批 · " + memorial_option_label(
                                    state.get("decided_label", ""))
                            ).classes("section-note")
                        with ui.row().classes("memorial-actions"):
                            if not decided_state:
                                for index, option in enumerate(
                                        memorial_visible_options(state)[:4]):
                                    ui.button(
                                        memorial_option_label(
                                            option.get("label", "选择")),
                                        on_click=lambda mid=state["id"],
                                        key=option.get("key", ""):
                                            decide(mid, key),
                                    ).props(
                                        "unelevated no-caps" if index == 0
                                        else "outline no-caps"
                                    ).classes(
                                        "memorial-primary" if index == 0
                                        else "memorial-secondary")
                            for button in state.get("extra_buttons", []):
                                if button.get("url"):
                                    ui.link(
                                        button.get("text", "打开来源"),
                                        button["url"], new_tab=True,
                                    ).classes("jarvis-nav-link")
                            with ui.link(
                                    target=f"/items/{state['id']}").classes(
                                        "item-detail-link"):
                                ui.icon("open_in_full", size="16px")
                                ui.label("查看全文")
                            if not decided_state:
                                ui.button(
                                    "电脑继续" if surface == "mobile"
                                    else "发到手机",
                                    icon=("computer" if surface == "mobile"
                                          else "smartphone"),
                                    on_click=lambda item=state: handoff(item),
                                ).props("flat no-caps").classes(
                                    "memorial-chat")
                            ui.button(
                                "去飞书聊", icon="forum",
                                on_click=lambda mid=state["id"]: chat(mid),
                            ).props("flat no-caps").classes("memorial-chat")

            remaining = len(selected) - len(shown)
            if remaining > 0:
                def show_more():
                    filters["limit"] += 12
                    board.refresh()
                ui.button(
                    f"继续看剩余 {remaining} 项", icon="expand_more",
                    on_click=show_more,
                ).props("outline no-caps").classes(
                    "memorial-secondary self-center")

        board()
        guarded_refresh_timer(20, board.refresh)


@ui.page("/items/{memorial_id}")
def item_detail_page(memorial_id: str):
    add_dashboard_head()
    surface, actor = client_surface()

    try:
        from core.delivery import DeliveryPipeline
        DeliveryPipeline(JARVIS_DIR).confirm_entity(
            memorial_id=memorial_id, state="read")
    except Exception:
        pass
    try:
        for handoff in list_handoffs(target_surface=surface, limit=100):
            if (handoff.get("entity_type") == "memorial"
                    and handoff.get("entity_id") == memorial_id):
                claim_handoff(handoff["id"], surface=surface)
    except Exception:
        pass

    with ui.column().classes("jarvis-page"):
        dashboard_header(
            "/items", "事项详情",
            "在这里读完整背景；批示、飞书和另一台设备共享同一状态。",
        )

        @ui.refreshable
        def detail():
            state = _load_item(memorial_id)
            if state is None:
                ui.label("这个事项不存在，或已经被归档。").classes(
                    "empty-guidance")
                ui.link("返回事项", "/items").classes("jarvis-nav-link")
                return
            decided_state = state.get("status") == "decided"
            if decided_state:
                complete_entity_handoffs("memorial", memorial_id)

            def decide(key: str):
                payload = memorial.decide(memorial_id, key)
                toast = payload.get("toast", {})
                ui.notify(
                    toast.get("content", "已记录"),
                    type=("positive"
                          if toast.get("type") == "success" else "info"),
                )
                detail.refresh()

            def chat():
                payload = memorial.chat(memorial_id)
                deep_link = str(payload.get("deep_link", "") or "")
                if deep_link:
                    ui.navigate.to(deep_link)
                else:
                    ui.notify("飞书入口暂不可用", type="warning")

            async def handoff():
                result = await _send_handoff(state, surface, actor)
                target = "desktop" if surface == "mobile" else "mobile"
                message, tone = _handoff_notice(result, target)
                ui.notify(message, type=tone)

            with ui.row().classes("w-full items-center justify-between gap-3"):
                ui.link("返回事项", "/items").classes("jarvis-nav-link")
                ui.label(state.get("ts", "")).classes("memorial-time")
            with ui.element("article").classes("item-detail"):
                with ui.row().classes("w-full items-center gap-2"):
                    if state.get("_topic_id"):
                        ui.link(
                            state["_topic_label"],
                            f"/matters/{state['_topic_id']}",
                        ).classes("memorial-source item-topic")
                    else:
                        ui.label(state["_topic_label"]).classes(
                            "memorial-source")
                    ui.label(memorial_surface_label(state)).classes(
                        "memorial-surface")
                    if state.get("_has_timer"):
                        with ui.element("span").classes("item-timer"):
                            ui.icon("schedule", size="14px")
                            ui.label("有定时提醒")
                ui.label(memorial_display_title(state)).classes(
                    "item-detail-title")
                ui.markdown(memorial_display_body(state)).classes(
                    "item-detail-body")
                context = str(state.get("context", "") or "").strip()
                if context:
                    with ui.expansion(
                            "相关背景", icon="subject").classes(
                                "item-context w-full"):
                        ui.markdown(context).classes("item-detail-body")
                if decided_state:
                    ui.label(
                        "已批 · " + memorial_option_label(
                            state.get("decided_label", ""))
                    ).classes("item-decision-result")
                with ui.row().classes("memorial-actions item-detail-actions"):
                    if not decided_state:
                        for index, option in enumerate(
                                memorial_visible_options(state)[:4]):
                            ui.button(
                                memorial_option_label(
                                    option.get("label", "选择")),
                                on_click=lambda key=option.get("key", ""):
                                    decide(key),
                            ).props(
                                "unelevated no-caps" if index == 0
                                else "outline no-caps"
                            ).classes(
                                "memorial-primary" if index == 0
                                else "memorial-secondary")
                    for button in state.get("extra_buttons", []):
                        if button.get("url"):
                            ui.link(
                                button.get("text", "打开来源"),
                                button["url"], new_tab=True,
                            ).classes("jarvis-nav-link")
                    if not decided_state:
                        ui.button(
                            "电脑继续" if surface == "mobile" else "发到手机",
                            icon=("computer" if surface == "mobile"
                                  else "smartphone"),
                            on_click=handoff,
                        ).props("outline no-caps").classes(
                            "memorial-secondary")
                    ui.button(
                        "去飞书聊", icon="forum", on_click=chat,
                    ).props("flat no-caps").classes("memorial-chat")

        detail()
        guarded_refresh_timer(20, detail.refresh)
