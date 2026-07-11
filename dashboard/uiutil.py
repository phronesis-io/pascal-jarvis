"""Shared UI helpers for dashboard pages."""

from __future__ import annotations

import inspect
import re
from contextlib import nullcontext

from nicegui import ui


_NAV = [
    ("今日", "/"),
    ("奏折", "/memorials"),
    ("意图", "/intentions"),
    ("任务", "/tasks"),
    ("收藏", "/bookmarks"),
    ("运行", "/ops"),
    ("更多", "/settings"),
]

_GENERIC_MEMORIAL_TITLES = {
    "", "EigenFlux", "eigenflux", "heartbeat", "pgc-improvement", "一件事",
}


def memorial_display_title(state: dict) -> str:
    """Turn legacy transport titles into a useful one-line subject."""
    title = str(state.get("title", "") or "").strip()
    if title not in _GENERIC_MEMORIAL_TITLES:
        return title
    body = str(state.get("body", "") or "").strip()
    first = next((line.strip() for line in body.splitlines() if line.strip()), "一件事")
    first = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", first)
    first = re.sub(r"[*_`#]+", "", first)
    first = re.sub(r"^[\s📡📬🩺🎯🧠🫀🌿📅💡⏰📺📊🪞🧭📋🌙]+", "", first)
    first = first.split("。", 1)[0].split("！", 1)[0].strip(" ·|-：:")
    return first if len(first) <= 30 else first[:30].rstrip() + "…"


def memorial_option_label(label: str) -> str:
    """Correct legacy promise-heavy copy without rewriting the audit ledger."""
    return {"重要，持续盯": "标为重点"}.get(str(label), str(label))


def memorial_attention_rank(state: dict) -> tuple[int, float]:
    """Direct asks and personal reflection outrank ambient network signals."""
    source = str(state.get("source", ""))
    if source in {"selfmon", "mail", "mail-triage", "intent", "intentions",
                  "intention-check", "checkin"}:
        tier = 3
    elif source in {"daily-reflect", "weekly-review", "cross-session-sync",
                    "memory", "calendar-sync"}:
        tier = 2
    elif source.startswith("eigenflux"):
        tier = 0
    else:
        tier = 1
    return tier, float(state.get("epoch", 0) or 0)


def add_dashboard_head() -> None:
    """Load the shared visual system for user-facing dashboard pages."""
    ui.add_head_html(
        '<link rel="stylesheet" href="/static/style.css">'
        '<meta name="theme-color" content="#f5f7f8">'
    )


def dashboard_header(active: str, title: str, subtitle: str = "") -> None:
    """Persistent decision-first navigation, shared by primary surfaces."""
    with ui.element("header").classes("jarvis-masthead"):
        with ui.row().classes("w-full items-start justify-between gap-4"):
            with ui.column().classes("gap-0"):
                ui.label("JARVIS · 御前").classes("jarvis-eyebrow")
                ui.label(title).classes("jarvis-display")
                if subtitle:
                    ui.label(subtitle).classes("jarvis-subtitle")
        with ui.element("nav").classes("jarvis-nav"):
            for label, href in _NAV:
                classes = "jarvis-nav-link"
                if href == active:
                    classes += " is-active"
                ui.link(label, href).classes(classes)


class _ClientBoundTimer(ui.timer):
    """ui.timer that dies quietly with its client instead of crash-looping.

    The daemon's liveness probe hits '/' every few minutes; each probe is a
    throwaway NiceGUI client that never opens a websocket. When the client is
    pruned, nicegui 3.12's Timer._run_in_loop evaluates `self._get_context()`
    (→ the `parent_slot` Element property) BEFORE its first _should_stop()
    check, and that property raises `RuntimeError: The parent slot of the
    element has been deleted` — one full traceback per pruned client, which
    was 100% of dashboard stderr (316 in one day). Catching inside the
    callback cannot help: the raise happens in the timer machinery before the
    callback ever runs. Intercept at the actual raise point instead.
    """

    def _get_context(self):
        try:
            return super()._get_context()
        except RuntimeError:
            self.cancel()
            return nullcontext()


def guarded_refresh_timer(interval: float, refresh) -> ui.timer:
    """Periodic refresh timer for dashboard pages, safe on pruned clients.

    `refresh` may be sync (refreshable.refresh) or an async callable. The
    in-callback guard below covers slot-deletion raised *during* a refresh
    (element updates racing a disconnect); _ClientBoundTimer covers the
    machinery-level raise before the callback runs.
    """
    timer: ui.timer | None = None

    async def _tick():
        try:
            result = refresh()
            if inspect.isawaitable(result):
                await result
        except RuntimeError as e:
            if "slot" in str(e) and "deleted" in str(e):
                if timer is not None:
                    timer.cancel()
            else:
                raise

    timer = _ClientBoundTimer(interval, _tick)
    return timer
