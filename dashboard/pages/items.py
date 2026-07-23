"""Unified user-facing item desk.

A visible item is a Memorial. Matter is an optional topic and Intent is a
timer attribute; neither competes for a separate top-level user concept.
"""

from __future__ import annotations

import time
from pathlib import Path

from nicegui import ui

from core import memorial

from ..telemetry import memorial_states
from ..uiutil import (
    add_dashboard_head,
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
)

JARVIS_DIR = Path(__file__).parent.parent.parent


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


@ui.page("/items")
def items_page():
    add_dashboard_head()
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
