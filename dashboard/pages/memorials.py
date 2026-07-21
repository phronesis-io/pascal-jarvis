"""奏折 inbox — the durable companion to Lark's one-card interaction."""

from __future__ import annotations

import time
from pathlib import Path

from nicegui import run, ui

from core import memorial

from ..telemetry import memorial_states as _cached_states
from ..uiutil import (add_dashboard_head, dashboard_header,
                      guarded_refresh_timer, memorial_attention_rank,
                      memorial_display_title, memorial_is_pending,
                      memorial_option_label, source_label)

JARVIS_DIR = Path(__file__).parent.parent.parent

# 三天没批的信息卡（只有 已阅/标为重点 选项）允许一键清账。
_STALE_AFTER_SECONDS = 3 * 86400


def memorial_states(jarvis_dir: str | Path) -> list[dict]:
    states = _cached_states(jarvis_dir)
    states.sort(key=lambda s: (s.get("epoch", 0), s.get("ts", "")), reverse=True)
    return states


def _compact(text: str, limit: int = 520) -> str:
    text = str(text or "").strip()
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def stale_info_cards(pending: list[dict],
                     now_ts: float | None = None) -> list[dict]:
    """Old FYI cards (read-option) safe to acknowledge in bulk.

    Cards that ask for a real decision (做了/还没做…) are never included —
    bulk touches only the 已阅-class backlog.
    """
    now_ts = now_ts if now_ts is not None else time.time()
    out = []
    for s in pending:
        try:
            epoch = float(s.get("epoch", 0) or 0)
        except (TypeError, ValueError):
            continue
        if not epoch or now_ts - epoch < _STALE_AFTER_SECONDS:
            continue
        keys = {o.get("key") for o in s.get("options", [])}
        if "read" in keys and not keys & {"done", "later", "approve", "publish"}:
            out.append(s)
    return out


@ui.page("/memorials")
def memorials_page():
    add_dashboard_head()
    mode = {"value": "pending"}
    source_filter = {"value": None}  # None = 全部来源（按 source_label 分组）
    visible_limit = {"value": 8}

    with ui.column().classes("jarvis-page"):
        dashboard_header("/memorials", "奏折", "一张卡说清一件事；批红之后仍然可以继续聊。")

        # 批量确认框必须活在 refreshable 外面：board 每 20 秒重建一次，
        # 建在里面的对话框会在用户看着它的时候被刷新销毁。
        bulk_pending: dict = {"cards": []}

        async def bulk_read():
            cards = list(bulk_pending["cards"])
            bulk_dialog.close()
            if not cards:
                return
            ui.notify(f"正在批 {len(cards)} 张…", type="info")

            def _run():
                done = 0
                for s in cards:
                    payload = memorial.decide(s["id"], "read")
                    if payload.get("toast", {}).get("type") == "success":
                        done += 1
                return done

            done = await run.io_bound(_run)
            ui.notify(f"已批 {done}/{len(cards)} 张为已阅", type="positive")
            board.refresh()

        with ui.dialog() as bulk_dialog, ui.card().classes("memorial-card"):
            ui.label("批量已阅").classes("memorial-title")

            @ui.refreshable
            def bulk_dialog_body():
                ui.label(f"把 {len(bulk_pending['cards'])} 张三天前的信息卡记为「已阅」。"
                         "需要动手的决策卡不受影响，批过的记录都会留在台账里。").classes(
                    "section-note")

            bulk_dialog_body()
            with ui.row().classes("memorial-actions"):
                ui.button("确认批量已阅", on_click=bulk_read).props(
                    "unelevated no-caps").classes("memorial-primary")
                ui.button("先不", on_click=bulk_dialog.close).props(
                    "outline no-caps").classes("memorial-secondary")

        def open_bulk_dialog(cards: list[dict]):
            bulk_pending["cards"] = cards
            bulk_dialog_body.refresh()
            bulk_dialog.open()

        @ui.refreshable
        def board():
            states = memorial_states(JARVIS_DIR)
            pending = [s for s in states if memorial_is_pending(s)]
            pending.sort(key=memorial_attention_rank, reverse=True)
            decided = [s for s in states if s.get("status") == "decided"]

            def set_mode(value: str):
                mode["value"] = value
                visible_limit["value"] = 8
                board.refresh()

            def set_source(value: str | None):
                source_filter["value"] = value
                visible_limit["value"] = 8
                board.refresh()

            with ui.row().classes("w-full items-center gap-2"):
                for value, text, count in (("pending", "待批", len(pending)),
                                           ("decided", "已批", len(decided)),
                                           ("all", "全部", len(states))):
                    active = mode["value"] == value
                    ui.button(f"{text} {count}",
                              on_click=lambda v=value: set_mode(v)).props(
                        ("unelevated" if active else "outline") + " no-caps").classes(
                        "filter-chip " + ("memorial-primary" if active
                                          else "memorial-secondary"))

            selected = ({"pending": pending, "decided": decided}.get(
                mode["value"], states))

            # 来源过滤——按用户可读的来源标签分组，多于一组才值得展示。
            groups: dict[str, int] = {}
            for s in selected:
                label = source_label(s.get("source", ""))
                groups[label] = groups.get(label, 0) + 1
            if source_filter["value"] is not None and source_filter["value"] not in groups:
                source_filter["value"] = None
            if len(groups) > 1:
                with ui.row().classes("w-full items-center gap-2"):
                    active = source_filter["value"] is None
                    ui.button(f"全部来源 {len(selected)}",
                              on_click=lambda: set_source(None)).props(
                        ("unelevated" if active else "flat") + " no-caps dense").classes(
                        "filter-chip " + ("memorial-primary" if active
                                          else "memorial-chat"))
                    top = sorted(groups.items(), key=lambda kv: -kv[1])[:8]
                    for label, count in top:
                        active = source_filter["value"] == label
                        ui.button(f"{label} {count}",
                                  on_click=lambda v=label: set_source(v)).props(
                            ("unelevated" if active else "flat") + " no-caps dense").classes(
                            "filter-chip " + ("memorial-primary" if active
                                              else "memorial-chat"))
            if source_filter["value"] is not None:
                selected = [s for s in selected
                            if source_label(s.get("source", "")) == source_filter["value"]]

            # 积压清账：旧信息卡一键已阅（真决策卡永不进这个批量）。
            stale = stale_info_cards(pending) if mode["value"] == "pending" else []
            if source_filter["value"] is not None:
                stale = [s for s in stale
                         if source_label(s.get("source", "")) == source_filter["value"]]
            if len(stale) >= 5:
                with ui.row().classes("w-full items-center gap-3"):
                    ui.label(f"有 {len(stale)} 张三天前的信息卡还没批。").classes(
                        "section-note")
                    ui.button("一键已阅", on_click=lambda cards=stale:
                              open_bulk_dialog(cards)).props(
                        "outline no-caps dense").classes("filter-chip memorial-secondary")

            shown = selected[:visible_limit["value"]]
            if not shown:
                message = ("没有待批事项。新的奏折会先到飞书，也会留在这里。"
                           if mode["value"] == "pending" else "这里还没有记录。")
                ui.label(message).classes("empty-guidance")
                return

            def decide(mid: str, key: str):
                payload = memorial.decide(mid, key)
                toast = payload.get("toast", {})
                ui.notify(toast.get("content", "已记录"),
                          type="positive" if toast.get("type") == "success" else "info")
                board.refresh()

            def chat(mid: str):
                payload = memorial.chat(mid)
                toast = payload.get("toast", {})
                ui.notify(toast.get("content", "已切到对话"),
                          type="positive" if toast.get("type") == "success" else "info")
                board.refresh()

            with ui.element("div").classes("memorial-grid"):
                for state in shown:
                    decided_state = state.get("status") == "decided"
                    classes = "memorial-card" + (" is-decided" if decided_state else "")
                    with ui.card().classes(classes):
                        with ui.row().classes("w-full items-center justify-between gap-3"):
                            ui.label(source_label(state.get("source", ""))).classes(
                                "memorial-source")
                            ui.label(state.get("ts", "")).classes("memorial-time")
                        ui.label(memorial_display_title(state)).classes("memorial-title")
                        ui.markdown(_compact(state.get("body", ""))).classes("memorial-body")
                        if decided_state:
                            decided_label = memorial_option_label(
                                state.get("decided_label", ""))
                            ui.label(f"已批 · {decided_label}").classes(
                                "section-note")
                        with ui.row().classes("memorial-actions"):
                            if not decided_state:
                                for index, option in enumerate(state.get("options", [])):
                                    ui.button(
                                        memorial_option_label(option.get("label", "选择")),
                                        on_click=lambda mid=state["id"], key=option.get("key", ""):
                                            decide(mid, key),
                                    ).props("unelevated no-caps" if index == 0 else "outline no-caps").classes(
                                        "memorial-primary" if index == 0 else "memorial-secondary")
                            for button in state.get("extra_buttons", []):
                                if button.get("url"):
                                    ui.link(button.get("text", "打开来源"), button["url"],
                                            new_tab=True).classes("jarvis-nav-link")
                            ui.button(
                                "聊聊这个",
                                on_click=lambda mid=state["id"]: chat(mid),
                            ).props("flat no-caps").classes("memorial-chat")

            remaining = len(selected) - len(shown)
            if remaining > 0:
                def show_more():
                    visible_limit["value"] += 8
                    board.refresh()
                ui.button(f"继续看剩余 {remaining} 张", on_click=show_more).props(
                    "outline no-caps").classes("memorial-secondary self-center")

        board()
        guarded_refresh_timer(20, board.refresh)
