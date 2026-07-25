"""Searchable projection of proactive notices in the Memorial ledger."""

from __future__ import annotations

import time
from pathlib import Path

from nicegui import ui

from core import memorial

from ..telemetry import memorial_states, memorial_states_all
from ..uiutil import (
    guarded_refresh_timer,
    jarvis_page,
    memorial_display_body,
    memorial_display_title,
    memorial_option_label,
    memorial_visible_options,
    source_label,
)

JARVIS_DIR = Path(__file__).parent.parent.parent
EIGENFLUX_SOURCES = {
    "eigenflux",
    "eigenflux-feed-triage",
    "eigenflux-friends",
    "eigenflux-publish",
}


def _source_matches(raw_source: str, selected: str) -> bool:
    if not selected:
        return True
    if selected == "eigenflux":
        return raw_source in EIGENFLUX_SOURCES or raw_source.startswith(
            "eigenflux-")
    return raw_source == selected


def filter_signals(
    states: list[dict],
    *,
    query: str = "",
    source: str = "",
    time_window: str = "7d",
    now: float | None = None,
) -> list[dict]:
    """Select readable notices without creating a second inbox or store."""
    now = time.time() if now is None else now
    cutoff = {
        "24h": now - 86400,
        "7d": now - 7 * 86400,
        "30d": now - 30 * 86400,
        "all": 0,
    }.get(time_window, now - 7 * 86400)
    needle = " ".join(str(query or "").casefold().split())
    selected: list[tuple[float, dict]] = []
    for state in states:
        # Signals are a searchable projection, not merely an unread inbox.
        # Keep informational notices discoverable after the user marks them
        # read; explicit decision cards never belong here.
        if memorial.requires_decision(state):
            continue
        if str(state.get("status") or "") not in {"pending", "decided"}:
            continue
        try:
            epoch = float(state.get("epoch", 0) or 0)
        except (TypeError, ValueError):
            epoch = 0
        if epoch < cutoff:
            continue
        raw_source = str(state.get("source") or "")
        if not _source_matches(raw_source, source):
            continue
        haystack = " ".join((
            memorial_display_title(state),
            memorial_display_body(state),
            source_label(raw_source),
            raw_source,
        )).casefold()
        if needle and needle not in haystack:
            continue
        selected.append((epoch, state))
    selected.sort(key=lambda item: item[0], reverse=True)
    return [state for _epoch, state in selected]


def _query_params() -> dict[str, str]:
    try:
        params = ui.context.client.request.query_params
        return {str(key): str(value) for key, value in params.items()}
    except (AttributeError, RuntimeError):
        return {}


@ui.page("/signals")
def signals_page():
    params = _query_params()
    filters = {
        "query": params.get("q", ""),
        "source": params.get("source", ""),
        "time": params.get("time", "7d"),
        "limit": 16,
    }

    with jarvis_page(
        "/signals",
        "信号",
        "Jarvis 从外部网络和后台观察中带回来的新信息。",
    ):
        def change(key: str, value: str):
            filters[key] = value
            filters["limit"] = 16
            board.refresh()

        @ui.refreshable
        def board():
            states = (
                memorial_states_all(JARVIS_DIR)
                if filters["time"] == "all"
                else memorial_states(JARVIS_DIR)
            )
            selected = filter_signals(
                states,
                query=filters["query"],
                source=filters["source"],
                time_window=filters["time"],
            )

            ui.label(f"{len(selected)} 条").classes("section-note")
            shown = selected[:filters["limit"]]
            if not shown:
                ui.label("当前筛选下没有新信号。").classes("empty-guidance")
                return

            def decide(memorial_id: str, option: str):
                payload = memorial.decide(
                    memorial_id, option, owner_authenticated=True)
                toast = payload.get("toast") or {}
                ui.notify(
                    toast.get("content", "已记录"),
                    type=("positive"
                          if toast.get("type") == "success" else "info"),
                )
                board.refresh()

            with ui.element("div").classes("signal-list"):
                for state in shown:
                    archived = bool(state.get("_archived"))
                    with ui.element("article").classes("signal-row"):
                        with ui.row().classes(
                                "w-full items-center justify-between gap-3"):
                            ui.label(
                                source_label(state.get("source", ""))
                            ).classes("memorial-source")
                            ui.label(state.get("ts", "")).classes(
                                "memorial-time")
                        ui.label(memorial_display_title(state)).classes(
                            "signal-title")
                        body = memorial_display_body(state)
                        if body:
                            ui.markdown(
                                body if archived else body[:1000]
                            ).classes("signal-body")
                        with ui.row().classes("memorial-actions"):
                            if not archived:
                                for option in memorial_visible_options(state)[:2]:
                                    ui.button(
                                        memorial_option_label(
                                            option.get("label", "记录")),
                                        on_click=lambda mid=state["id"],
                                        key=option.get("key", ""):
                                            decide(mid, key),
                                    ).props("outline no-caps").classes(
                                        "memorial-secondary")
                            for button in state.get("extra_buttons", []):
                                if button.get("url"):
                                    ui.link(
                                        button.get("text", "打开来源"),
                                        button["url"],
                                        new_tab=True,
                                    ).classes("item-detail-link")
                            if archived:
                                with ui.element("span").classes(
                                        "item-detail-link"):
                                    ui.icon("history", size="16px")
                                    ui.label("历史记录 · 只读")
                            else:
                                with ui.link(
                                        target=f"/items/{state['id']}").classes(
                                            "item-detail-link"):
                                    ui.icon("open_in_full", size="16px")
                                    ui.label("查看全文")

            remaining = len(selected) - len(shown)
            if remaining > 0:
                def show_more():
                    filters["limit"] += 16
                    board.refresh()

                ui.button(
                    f"继续看剩余 {remaining} 条",
                    icon="expand_more",
                    on_click=show_more,
                ).props("outline no-caps").classes(
                    "memorial-secondary self-center")

        source_options = {"": "全部来源", "eigenflux": "EigenFlux"}
        for raw_source in sorted({
                str(state.get("source") or "")
                for state in memorial_states_all(JARVIS_DIR)
                if state.get("source")
        }):
            if raw_source not in EIGENFLUX_SOURCES:
                source_options.setdefault(
                    raw_source, source_label(raw_source))

        # Controls live outside the refreshable result subtree so typing does
        # not destroy the focused input after each server round-trip.
        with ui.row().classes(
                "w-full items-center gap-3 signal-filter-row"):
            ui.input(
                "搜索信号",
                value=filters["query"],
                on_change=lambda event: change(
                    "query", str(event.value or "")),
            ).props("dense outlined clearable").classes("signal-search")
            ui.select(
                source_options,
                value=filters["source"],
                label="来源",
                on_change=lambda event: change(
                    "source", str(event.value or "")),
            ).props("dense outlined options-dense").classes(
                "item-filter-select")
            ui.select(
                {
                    "24h": "24 小时",
                    "7d": "7 天",
                    "30d": "30 天",
                    "all": "全部时间",
                },
                value=filters["time"],
                label="时间",
                on_change=lambda event: change("time", event.value),
            ).props("dense outlined options-dense").classes(
                "item-filter-select")

        board()
        guarded_refresh_timer(30, board.refresh)
