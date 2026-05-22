"""Tasks page — view and manage scheduled tasks.

Two layers:
- "Assistant's Day" view: natural language, grouped by time of day
- "Power User" view: cron details, execution history, enable/disable
"""

import json
import time
from datetime import datetime
from pathlib import Path

from nicegui import ui

from ..db import get_db, task_list, task_update, task_delete, task_register
from ..scheduler import cron_matches

JARVIS_DIR = Path(__file__).parent.parent.parent


def _load_static_tasks() -> list[dict]:
    """Load tasks from HEARTBEAT.md for display (read-only)."""
    try:
        from core.heartbeat import parse_heartbeat
        hb_path = JARVIS_DIR / "HEARTBEAT.md"
        if hb_path.exists():
            return parse_heartbeat(hb_path)
    except ImportError:
        pass
    return []


def _load_heartbeat_state() -> dict:
    """Load heartbeat state."""
    state_file = JARVIS_DIR / "heartbeat_state.json"
    if state_file.exists():
        try:
            return json.loads(state_file.read_text())
        except Exception:
            pass
    return {}


def _format_interval(seconds: int) -> str:
    """Format seconds as human-readable interval."""
    if seconds < 60:
        return f"{seconds}s"
    elif seconds < 3600:
        return f"{seconds // 60}m"
    elif seconds < 86400:
        return f"{seconds // 3600}h"
    else:
        return f"{seconds // 86400}d"


@ui.page("/tasks")
def tasks_page():
    """Tasks management page."""
    ui.add_head_html("""
    <style>
        .task-card { border: 1px solid #e5e7eb; border-radius: 8px; padding: 0.75rem;
                     transition: all 0.15s; }
        .task-card:hover { border-color: #3b82f6; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }
        .task-static { border-left: 3px solid #6b7280; }
        .task-dynamic { border-left: 3px solid #3b82f6; }
        .task-disabled { opacity: 0.5; }
    </style>
    """)

    with ui.column().classes("w-full max-w-4xl mx-auto p-4 gap-4"):
        with ui.row().classes("w-full items-center justify-between"):
            ui.link("← Home", "/").classes("text-gray-500 text-sm")
            ui.label("Task Scheduler").classes("text-2xl font-bold")

        # View toggle
        view_mode = ui.toggle(["Assistant's Day", "Power User"], value="Assistant's Day")

        # Dynamic tasks section
        ui.label("Dynamic Tasks").classes("text-lg font-semibold mt-4")
        ui.label("Tasks registered by the agent (LLM-writable)").classes(
            "text-xs text-gray-500"
        )

        dynamic_tasks = task_list()
        dynamic_container = ui.column().classes("w-full gap-2")

        with dynamic_container:
            if dynamic_tasks:
                for task in dynamic_tasks:
                    trigger_config = json.loads(task["trigger_config"]) if isinstance(task["trigger_config"], str) else task["trigger_config"]
                    disabled = not task.get("enabled", True)
                    classes = f"task-card task-dynamic w-full {'task-disabled' if disabled else ''}"
                    with ui.card().classes(classes):
                        with ui.row().classes("w-full items-center justify-between"):
                            with ui.column().classes("gap-0"):
                                ui.label(task["name"]).classes("font-medium")
                                trigger_desc = ""
                                if task["trigger_type"] == "cron":
                                    trigger_desc = f"cron: {trigger_config.get('expression', '')}"
                                elif task["trigger_type"] == "interval":
                                    trigger_desc = f"every {_format_interval(trigger_config.get('seconds', 0))}"
                                elif task["trigger_type"] == "date":
                                    trigger_desc = f"at {trigger_config.get('datetime', '')}"
                                ui.label(trigger_desc).classes("text-xs text-gray-500")
                            with ui.row().classes("gap-2"):
                                ui.label(f"runs: {task.get('run_count', 0)}").classes("text-xs text-gray-400")
                                if task.get("last_run_at"):
                                    ui.label(f"last: {task['last_run_at'][:16]}").classes("text-xs text-gray-400")
            else:
                ui.label("No dynamic tasks yet. The agent can register tasks via [ACTION:schedule_task|...]").classes(
                    "text-gray-400 text-sm italic"
                )

        # Static tasks (from HEARTBEAT.md)
        ui.separator()
        ui.label("Static Tasks (HEARTBEAT.md)").classes("text-lg font-semibold mt-2")
        ui.label("Read-only — edit HEARTBEAT.md to change").classes("text-xs text-gray-500")

        state = _load_heartbeat_state()
        static_tasks = _load_static_tasks()

        for task in static_tasks:
            task_state = state.get(task["name"], {})
            last_run = task_state.get("last_run", 0)
            now = int(time.time())
            ago = now - last_run if last_run else None

            with ui.card().classes("task-card task-static w-full"):
                with ui.row().classes("w-full items-center justify-between"):
                    with ui.column().classes("gap-0"):
                        ui.label(task["name"]).classes("font-medium text-sm")
                        ui.label(f"interval: {_format_interval(task['interval'])}").classes(
                            "text-xs text-gray-500"
                        )
                    with ui.row().classes("gap-2 items-center"):
                        if ago is not None:
                            if ago < task["interval"]:
                                ui.badge("on time", color="green").classes("text-xs")
                            elif ago < task["interval"] * 2:
                                ui.badge("due", color="amber").classes("text-xs")
                            else:
                                ui.badge("overdue", color="red").classes("text-xs")
                            ui.label(f"{_format_interval(ago)} ago").classes("text-xs text-gray-400")
                        else:
                            ui.badge("never run", color="gray").classes("text-xs")

        # Add task form
        ui.separator()
        ui.label("Register New Task").classes("text-lg font-semibold mt-2")

        with ui.card().classes("w-full p-4"):
            name_input = ui.input("Task name", placeholder="morning_alarm").classes("w-full")
            with ui.row().classes("w-full gap-4"):
                trigger_type = ui.select(
                    ["cron", "interval", "date"],
                    value="cron", label="Trigger type"
                )
                trigger_value = ui.input(
                    "Trigger config",
                    placeholder="0 6 * * * (cron) or 600 (interval seconds) or 2026-05-22T06:00"
                ).classes("flex-1")
            action_type = ui.select(
                ["prompt", "notify", "script"],
                value="notify", label="Action type"
            )
            action_value = ui.textarea(
                "Action config (JSON or text)",
                placeholder='{"message": "起床了！"}'
            ).classes("w-full")

            async def register_task():
                import uuid
                name = name_input.value.strip()
                if not name:
                    ui.notify("Name is required", type="warning")
                    return
                tt = trigger_type.value
                tv = trigger_value.value.strip()
                # Parse trigger config
                if tt == "cron":
                    tc = {"expression": tv}
                elif tt == "interval":
                    tc = {"seconds": int(tv) if tv.isdigit() else 600}
                else:
                    tc = {"datetime": tv}
                # Parse action config
                av = action_value.value.strip()
                try:
                    ac = json.loads(av) if av.startswith("{") else {"message": av}
                except json.JSONDecodeError:
                    ac = {"message": av}

                task_id = f"user_{uuid.uuid4().hex[:8]}"
                task_register(
                    task_id=task_id, name=name,
                    trigger_type=tt, trigger_config=tc,
                    action_type=action_type.value, action_config=ac,
                    category="user", priority=5,
                )
                ui.notify(f"Task '{name}' registered!", type="positive")
                # Clear form
                name_input.value = ""
                trigger_value.value = ""
                action_value.value = ""

            ui.button("Register Task", on_click=register_task).classes("mt-2")
