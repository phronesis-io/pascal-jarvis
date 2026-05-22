"""Home page — Timeline Feed + Ambient Status.

Not a dashboard. A living timeline of "what happened while you were away".
"""

import json
import time
from datetime import datetime, timedelta
from pathlib import Path

from nicegui import ui

from ..db import get_db, log_list, engagement_stats, bookmark_list
from ..event_bus import bus

JARVIS_DIR = Path(__file__).parent.parent.parent


def _load_heartbeat_state() -> dict:
    """Load current heartbeat state."""
    state_file = JARVIS_DIR / "heartbeat_state.json"
    if state_file.exists():
        try:
            return json.loads(state_file.read_text())
        except Exception:
            pass
    return {}


def _agent_status() -> tuple[str, str]:
    """Determine agent status: (label, color)."""
    state = _load_heartbeat_state()
    if not state:
        return "offline", "red"
    # Check if any task ran in the last 2 minutes
    now = int(time.time())
    for task_name, task_state in state.items():
        last_run = task_state.get("last_run", 0)
        if now - last_run < 120:
            return "active", "green"
    # Ran recently but not in last 2 min
    latest = max((s.get("last_run", 0) for s in state.values()), default=0)
    if now - latest < 600:
        return "idle", "amber"
    return "sleeping", "gray"


def _format_relative_time(iso_str: str) -> str:
    """Format ISO time as relative (e.g., '3m ago')."""
    try:
        dt = datetime.fromisoformat(iso_str)
        delta = datetime.now() - dt
        if delta.total_seconds() < 60:
            return "just now"
        elif delta.total_seconds() < 3600:
            return f"{int(delta.total_seconds() / 60)}m ago"
        elif delta.total_seconds() < 86400:
            return f"{int(delta.total_seconds() / 3600)}h ago"
        else:
            return f"{int(delta.total_seconds() / 86400)}d ago"
    except Exception:
        return iso_str


@ui.page("/")
def home_page():
    """Main dashboard page."""
    ui.add_head_html("""
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; }
        .timeline-entry { border-left: 2px solid #e5e7eb; padding-left: 1rem; margin-bottom: 0.75rem; }
        .timeline-entry:hover { border-left-color: #3b82f6; }
        .status-dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }
        .stat-card { background: #f9fafb; border-radius: 12px; padding: 1rem; }
    </style>
    """)

    with ui.column().classes("w-full max-w-4xl mx-auto p-4 gap-4"):
        # Header
        with ui.row().classes("w-full items-center justify-between"):
            ui.label("Jarvis").classes("text-2xl font-bold")
            status, color = _agent_status()
            with ui.row().classes("items-center gap-2"):
                ui.html(f'<span class="status-dot" style="background:{color}"></span>')
                ui.label(status).classes("text-sm text-gray-500")

        # Quick stats row
        stats = engagement_stats(7)
        with ui.row().classes("w-full gap-4"):
            with ui.card().classes("stat-card flex-1"):
                ui.label(f"{stats['rate']}%").classes("text-2xl font-bold")
                ui.label("7d engagement").classes("text-xs text-gray-500")

            bookmarks_count = len(bookmark_list(status="inbox"))
            with ui.card().classes("stat-card flex-1"):
                ui.label(str(bookmarks_count)).classes("text-2xl font-bold")
                ui.label("inbox items").classes("text-xs text-gray-500")

            state = _load_heartbeat_state()
            active_tasks = sum(1 for s in state.values()
                             if int(time.time()) - s.get("last_run", 0) < 3600)
            with ui.card().classes("stat-card flex-1"):
                ui.label(str(active_tasks)).classes("text-2xl font-bold")
                ui.label("active tasks").classes("text-xs text-gray-500")

        # Activity timeline
        ui.label("Recent Activity").classes("text-lg font-semibold mt-4")

        logs = log_list(limit=30)
        if logs:
            for entry in logs:
                with ui.element("div").classes("timeline-entry"):
                    with ui.row().classes("items-center gap-2"):
                        # Source badge
                        source = entry.get("source", "system")
                        badge_colors = {
                            "heartbeat": "blue",
                            "eigenflux": "purple",
                            "lark": "green",
                            "scheduler": "amber",
                            "memory": "gray",
                            "bookmark": "pink",
                        }
                        color = badge_colors.get(source, "gray")
                        ui.badge(source, color=color).classes("text-xs")
                        ui.label(entry.get("message", "")).classes("text-sm flex-1")
                        ts = entry.get("timestamp", "")
                        ui.label(_format_relative_time(ts)).classes(
                            "text-xs text-gray-400"
                        )
        else:
            ui.label("No activity yet. Agent logs will appear here.").classes(
                "text-gray-400 text-sm italic"
            )

        # Navigation
        ui.separator()
        with ui.row().classes("gap-4"):
            ui.link("Tasks", "/tasks").classes("text-blue-600")
            ui.link("Intentions", "/intentions").classes("text-purple-600")
            ui.link("Bookmarks", "/bookmarks").classes("text-blue-600")
            ui.link("Settings", "/settings").classes("text-blue-600")
