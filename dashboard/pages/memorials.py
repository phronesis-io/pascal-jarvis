"""奏折 inbox — the durable companion to Lark's one-card interaction."""

from __future__ import annotations

from pathlib import Path

from nicegui import ui

from core import memorial

from ..telemetry import memorial_states as _cached_states
from ..uiutil import (add_dashboard_head, dashboard_header,
                      guarded_refresh_timer, memorial_attention_rank,
                      memorial_display_body, memorial_display_title,
                      memorial_is_notice,
                      memorial_is_pending,
                      memorial_option_label, source_label)

JARVIS_DIR = Path(__file__).parent.parent.parent

def memorial_states(jarvis_dir: str | Path) -> list[dict]:
    states = _cached_states(jarvis_dir)
    states.sort(key=lambda s: (s.get("epoch", 0), s.get("ts", "")), reverse=True)
    return states


def _compact(text: str, limit: int = 520) -> str:
    text = str(text or "").strip()
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


@ui.page("/memorials")
def memorials_page():
    add_dashboard_head()
    mode = {"value": "pending"}
    source_filter = {"value": None}  # None = 全部来源（按 source_label 分组）
    visible_limit = {"value": 8}

    with ui.column().classes("jarvis-page"):
        dashboard_header("/memorials", "奏折", "一张卡说清一件事；批红之后仍然可以继续聊。")

        @ui.refreshable
        def board():
            states = memorial_states(JARVIS_DIR)
            pending = [s for s in states if memorial_is_pending(s)]
            pending.sort(key=memorial_attention_rank, reverse=True)
            notices = [s for s in states if memorial_is_notice(s)]
            notices.sort(key=lambda s: (s.get("epoch", 0), s.get("ts", "")),
                         reverse=True)
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
                                           ("notice", "知会", len(notices)),
                                           ("decided", "已批", len(decided)),
                                           ("all", "全部", len(states))):
                    active = mode["value"] == value
                    ui.button(f"{text} {count}",
                              on_click=lambda v=value: set_mode(v)).props(
                        ("unelevated" if active else "outline") + " no-caps").classes(
                        "filter-chip " + ("memorial-primary" if active
                                          else "memorial-secondary"))

            selected = ({"pending": pending, "notice": notices,
                         "decided": decided}.get(
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

            shown = selected[:visible_limit["value"]]
            if not shown:
                message = {
                    "pending": "没有待批事项。需要明确选择时，奏折会同时到飞书和这里。",
                    "notice": "暂时没有新的知会。",
                }.get(mode["value"], "这里还没有记录。")
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
                        ui.markdown(_compact(memorial_display_body(state))).classes(
                            "memorial-body")
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
