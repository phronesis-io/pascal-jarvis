"""Tasks page — schedule definitions (HEARTBEAT.md) + dynamic-task register.

REQ-44: the permanently-empty "Dynamic Tasks" listing and the fictional
on-time/due/overdue badges (derived from heartbeat_state watermarks, not
real runs) are gone. This page now shows the SCHEDULE (what is supposed to
run and how often); actual run health lives on the Task Health board, which
reads sched_events.jsonl.
"""

import json
from pathlib import Path

from nicegui import ui

from ..db import task_register

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
    """Tasks schedule page."""
    ui.add_head_html("""
    <style>
        .task-card { border: 1px solid #e5e7eb; border-radius: 8px; padding: 0.75rem;
                     transition: all 0.15s; }
        .task-card:hover { border-color: #3b82f6; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }
        .task-static { border-left: 3px solid #6b7280; }
    </style>
    """)

    with ui.column().classes("w-full max-w-4xl mx-auto p-4 gap-4"):
        with ui.row().classes("w-full items-center justify-between"):
            ui.link("← Home", "/").classes("text-gray-500 text-sm")
            ui.label("Task Scheduler").classes("text-2xl font-bold")

        # 真实运行数据（最后一次真实运行/失败率/skip 原因）在健康板
        with ui.card().classes("w-full p-3"):
            ui.label("这页是排程定义。任务实际跑没跑、失败率、skip 原因——看健康板：").classes(
                "text-sm text-gray-600")
            ui.link("→ Task Health (sched_events 真实运行视图)", "/agent-calendar").classes(
                "text-blue-600 font-medium")

        # Static tasks (from HEARTBEAT.md)
        ui.label("Static Tasks (HEARTBEAT.md)").classes("text-lg font-semibold mt-2")
        ui.label("Read-only — edit HEARTBEAT.md to change").classes("text-xs text-gray-500")

        static_tasks = _load_static_tasks()

        for task in static_tasks:
            with ui.card().classes("task-card task-static w-full"):
                with ui.row().classes("w-full items-center justify-between"):
                    with ui.column().classes("gap-0"):
                        ui.label(task["name"]).classes("font-medium text-sm")
                        ui.label(f"interval: {_format_interval(task['interval'])}").classes(
                            "text-xs text-gray-500"
                        )
                    ui.link("health →", "/agent-calendar").classes("text-xs text-blue-500")

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
