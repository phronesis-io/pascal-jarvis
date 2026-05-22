"""Intentions page — visual timeline of agent's future actions.

Shows upcoming intents on a timeline, past executions, and lets the user
create/cancel intents from the UI.
"""

import json
import time
from datetime import datetime
from pathlib import Path

from nicegui import ui

JARVIS_DIR = Path(__file__).parent.parent.parent

# Import lazily to avoid circular issues at module load
def _get_intentions_module():
    import sys
    sys.path.insert(0, str(JARVIS_DIR))
    from core import intentions
    return intentions


def _parse_trigger_when(intent: dict) -> str:
    """Human-readable trigger description."""
    tc = json.loads(intent["trigger_config"]) if isinstance(intent["trigger_config"], str) else intent["trigger_config"]
    tt = intent["trigger_type"]
    if tt == "date":
        dt_str = tc.get("datetime", "")
        if dt_str:
            try:
                dt = datetime.fromisoformat(dt_str)
                now = datetime.now()
                delta = dt - now
                if delta.total_seconds() < 0:
                    return f"已过期 ({dt.strftime('%m/%d %H:%M')})"
                elif delta.total_seconds() < 3600:
                    return f"{int(delta.total_seconds() / 60)}分钟后"
                elif delta.total_seconds() < 86400:
                    return f"{int(delta.total_seconds() / 3600)}小时后"
                else:
                    return dt.strftime("%m/%d %H:%M")
            except ValueError:
                return dt_str
    elif tt == "cron":
        return f"cron: {tc.get('expression', '')}"
    elif tt == "interval":
        secs = tc.get("seconds", 0)
        if secs < 60:
            return f"每 {secs}秒"
        elif secs < 3600:
            return f"每 {secs // 60}分钟"
        else:
            return f"每 {secs // 3600}小时"
    return "?"


def _status_badge(status: str):
    """Render a colored badge for intent status."""
    colors = {
        "pending": "blue",
        "triggered": "amber",
        "executed": "green",
        "expired": "gray",
        "cancelled": "red",
    }
    return ui.badge(status, color=colors.get(status, "gray")).classes("text-xs")


@ui.page("/intentions")
def intentions_page():
    """Intentions timeline page."""
    mod = _get_intentions_module()

    ui.add_head_html("""
    <style>
        .intent-card { border: 1px solid #e5e7eb; border-radius: 8px; padding: 0.75rem;
                       transition: all 0.15s; position: relative; }
        .intent-card:hover { border-color: #8b5cf6; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }
        .intent-pending { border-left: 3px solid #3b82f6; }
        .intent-executed { border-left: 3px solid #22c55e; opacity: 0.7; }
        .intent-cancelled { border-left: 3px solid #ef4444; opacity: 0.5; }
        .intent-expired { border-left: 3px solid #9ca3af; opacity: 0.5; }
        .timeline-dot { width: 12px; height: 12px; border-radius: 50%;
                        display: inline-block; margin-right: 8px; }
        .dot-pending { background: #3b82f6; }
        .dot-executed { background: #22c55e; }
        .dot-cancelled { background: #ef4444; }
    </style>
    """)

    with ui.column().classes("w-full max-w-4xl mx-auto p-4 gap-4"):
        with ui.row().classes("w-full items-center justify-between"):
            ui.link("← Home", "/").classes("text-gray-500 text-sm")
            ui.label("🎯 Intentions").classes("text-2xl font-bold")

        # Stats
        stats = mod.intent_stats()
        with ui.row().classes("gap-4"):
            ui.label(f"Pending: {stats.get('pending', 0)}").classes("text-blue-600 font-medium")
            ui.label(f"Executed: {stats.get('executed', 0)}").classes("text-green-600")
            ui.label(f"Expired: {stats.get('expired', 0)}").classes("text-gray-400")
            ui.label(f"Cancelled: {stats.get('cancelled', 0)}").classes("text-red-400")

        # Active intents (pending)
        ui.label("Upcoming").classes("text-lg font-semibold mt-4")
        pending = mod.list_intents(status="pending", limit=50)

        if pending:
            for intent in pending:
                tags = json.loads(intent["tags"]) if isinstance(intent["tags"], str) else (intent["tags"] or [])
                with ui.card().classes(f"intent-card intent-pending w-full"):
                    with ui.row().classes("w-full items-start justify-between"):
                        with ui.column().classes("gap-1 flex-1"):
                            with ui.row().classes("items-center gap-2"):
                                ui.html('<span class="timeline-dot dot-pending"></span>')
                                ui.label(intent["name"]).classes("font-medium")
                            ui.label(_parse_trigger_when(intent)).classes("text-xs text-blue-500")
                            if intent.get("purpose"):
                                ui.label(intent["purpose"]).classes("text-xs text-gray-500 italic")
                            if intent.get("prompt"):
                                prompt_preview = intent["prompt"][:120] + ("..." if len(intent["prompt"]) > 120 else "")
                                ui.label(prompt_preview).classes("text-xs text-gray-400 mt-1")
                            if tags:
                                with ui.row().classes("gap-1 mt-1"):
                                    for tag in tags[:3]:
                                        ui.badge(tag, color="purple").props("outline").classes("text-xs")
                        with ui.column().classes("items-end gap-1"):
                            ui.label(f"src: {intent['source']}").classes("text-xs text-gray-400")
                            ui.label(f"id: {intent['id'][:12]}").classes("text-xs text-gray-300")

                            async def cancel_click(iid=intent["id"]):
                                mod.cancel_intent(iid, reason="cancelled from dashboard")
                                ui.notify("Intent cancelled", type="warning")
                                ui.navigate.reload()

                            ui.button("Cancel", on_click=cancel_click).props("flat dense size=xs color=red")
        else:
            ui.label("No pending intents. The agent will create them from calendar events and conversations.").classes(
                "text-gray-400 text-sm italic"
            )

        # Recently executed
        ui.separator()
        ui.label("Recently Executed").classes("text-lg font-semibold mt-2")
        executed = mod.list_intents(status="executed", limit=10)
        if executed:
            for intent in executed:
                with ui.card().classes("intent-card intent-executed w-full"):
                    with ui.row().classes("w-full items-center justify-between"):
                        with ui.column().classes("gap-0"):
                            with ui.row().classes("items-center gap-2"):
                                ui.html('<span class="timeline-dot dot-executed"></span>')
                                ui.label(intent["name"]).classes("font-medium text-sm")
                            if intent.get("executed_at"):
                                ui.label(f"executed: {intent['executed_at'][:16]}").classes("text-xs text-gray-400")
                        _status_badge("executed")
        else:
            ui.label("No executed intents yet.").classes("text-gray-400 text-sm italic")

        # Create new intent form
        ui.separator()
        ui.label("Create Intent").classes("text-lg font-semibold mt-2")

        with ui.card().classes("w-full p-4"):
            name_input = ui.input("Name", placeholder="Follow up on X").classes("w-full")
            with ui.row().classes("w-full gap-4"):
                trigger_type = ui.select(
                    ["date", "cron", "interval"],
                    value="date", label="Trigger type"
                )
                trigger_value = ui.input(
                    "When",
                    placeholder="2026-05-22T09:00 (date) or 0 9 * * * (cron) or 3600 (interval)"
                ).classes("flex-1")
            prompt_input = ui.textarea(
                "Prompt (what should the agent think about at that moment)",
                placeholder="Check in on Pascal about..."
            ).classes("w-full")
            purpose_input = ui.input("Purpose (why)", placeholder="Follow up on conversation").classes("w-full")
            tags_input = ui.input("Tags (comma separated)", placeholder="health, follow-up").classes("w-full")

            async def create_intent_click():
                name = name_input.value.strip()
                if not name:
                    ui.notify("Name is required", type="warning")
                    return
                tt = trigger_type.value
                tv = trigger_value.value.strip()
                if tt == "date":
                    tc = {"datetime": tv}
                elif tt == "cron":
                    tc = {"expression": tv}
                else:
                    tc = {"seconds": int(tv) if tv.isdigit() else 600}
                tags = [t.strip() for t in tags_input.value.split(",") if t.strip()]

                mod.create_intent(
                    name=name,
                    trigger_type=tt,
                    trigger_config=tc,
                    prompt=prompt_input.value.strip(),
                    purpose=purpose_input.value.strip(),
                    tags=tags,
                    source="dashboard",
                    action_type="notify",
                )
                ui.notify(f"Intent '{name}' created!", type="positive")
                name_input.value = ""
                trigger_value.value = ""
                prompt_input.value = ""
                purpose_input.value = ""
                tags_input.value = ""

            ui.button("Create Intent", on_click=create_intent_click).classes("mt-2")
