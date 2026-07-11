"""奏折 inbox — the durable companion to Lark's one-card interaction."""

from __future__ import annotations

from pathlib import Path

from nicegui import ui

from core import memorial
from core.jsonl import read_jsonl

from ..uiutil import (add_dashboard_head, dashboard_header,
                      guarded_refresh_timer, memorial_attention_rank,
                      memorial_display_title,
                      memorial_option_label)

JARVIS_DIR = Path(__file__).parent.parent.parent

_SOURCE_LABELS = {
    "eigenflux-feed-triage": "EigenFlux",
    "eigenflux": "EigenFlux",
    "mail": "邮件",
    "mail-triage": "邮件",
    "selfmon": "自诊断",
    "intention-check": "意图",
    "intentions": "意图",
    "cross-session-sync": "跨 Session",
    "daily-reflect": "复盘",
    "weekly-review": "周回顾",
}


def memorial_states(jarvis_dir: str | Path) -> list[dict]:
    states = list(memorial._fold(
        read_jsonl(Path(jarvis_dir) / "memorials.jsonl")).values())
    states.sort(key=lambda s: (s.get("epoch", 0), s.get("ts", "")), reverse=True)
    return states


def _compact(text: str, limit: int = 520) -> str:
    text = str(text or "").strip()
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def _source(state: dict) -> str:
    raw = str(state.get("source", "") or "奏折")
    return _SOURCE_LABELS.get(raw, raw.replace("-", " "))


@ui.page("/memorials")
def memorials_page():
    add_dashboard_head()
    mode = {"value": "pending"}
    visible_limit = {"value": 8}

    with ui.column().classes("jarvis-page"):
        dashboard_header("/memorials", "奏折", "一张卡说清一件事；批红之后仍然可以继续聊。")

        @ui.refreshable
        def board():
            states = memorial_states(JARVIS_DIR)
            pending = [s for s in states if s.get("status") == "pending"]
            pending.sort(key=memorial_attention_rank, reverse=True)
            decided = [s for s in states if s.get("status") == "decided"]

            def set_mode(value: str):
                mode["value"] = value
                visible_limit["value"] = 8
                board.refresh()

            with ui.row().classes("w-full items-center gap-2"):
                ui.button(f"待批 {len(pending)}", on_click=lambda: set_mode("pending")).props(
                    "unelevated no-caps").classes(
                    "memorial-primary" if mode["value"] == "pending" else "memorial-secondary")
                ui.button(f"已批 {len(decided)}", on_click=lambda: set_mode("decided")).props(
                    "outline no-caps").classes(
                    "memorial-primary" if mode["value"] == "decided" else "memorial-secondary")
                ui.button(f"全部 {len(states)}", on_click=lambda: set_mode("all")).props(
                    "outline no-caps").classes(
                    "memorial-primary" if mode["value"] == "all" else "memorial-secondary")

            selected = ({"pending": pending, "decided": decided}.get(mode["value"], states))
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
                            ui.label(_source(state)).classes("memorial-source")
                            ui.label(state.get("ts", "")).classes("memorial-time")
                        ui.label(memorial_display_title(state)).classes("memorial-title")
                        ui.markdown(_compact(state.get("body", ""))).classes("memorial-body")
                        if decided_state:
                            ui.label(f"已批 · {state.get('decided_label', '')}").classes(
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
