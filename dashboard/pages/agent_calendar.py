"""Agent Calendar page — 24h timeline of heartbeat task executions.

Three layers of progressive disclosure:
1. Glanceable summary — total tasks, health status
2. Timeline — horizontal bars showing task executions across 24h
3. Detail — click to see individual task runs (future enhancement)
"""

import json
import time
from datetime import datetime, timedelta
from pathlib import Path

from core.timeutil import now_local

from nicegui import ui

JARVIS_DIR = Path(__file__).parent.parent.parent


def _load_heartbeat_state() -> dict:
    """Load heartbeat state with last_run timestamps."""
    state_file = JARVIS_DIR / "heartbeat_state.json"
    if state_file.exists():
        try:
            return json.loads(state_file.read_text())
        except Exception:
            pass
    return {}


def _load_heartbeat_tasks() -> list[dict]:
    """Load task definitions from HEARTBEAT.md."""
    try:
        import sys
        sys.path.insert(0, str(JARVIS_DIR))
        from core.heartbeat import parse_heartbeat
        hb_path = JARVIS_DIR / "HEARTBEAT.md"
        if hb_path.exists():
            return parse_heartbeat(hb_path)
    except ImportError:
        pass
    return []


# Task category colors
CATEGORY_COLORS = {
    "daily": "#22c55e",      # green
    "calendar": "#3b82f6",   # blue
    "memory": "#8b5cf6",     # purple
    "eigenflux": "#f59e0b",  # amber
    "content": "#ec4899",    # pink
    "session": "#06b6d4",    # cyan
    "monitor": "#6b7280",    # gray
    "maintenance": "#9ca3af", # light gray
}


def _categorize_task(name: str) -> str:
    """Infer category from task name."""
    if name.startswith("daily-") or name in ("free-time-nudge", "weekly-review",
                                              "checkin", "activity-log"):
        return "daily"
    if "calendar" in name or "intention" in name:
        return "calendar"
    if "memory" in name:
        return "memory"
    if "eigenflux" in name:
        return "eigenflux"
    if "content" in name or "watchlater" in name:
        return "content"
    if "session" in name or "cross-session" in name:
        return "session"
    if "monitor" in name or "diagnostic" in name:
        return "monitor"
    return "maintenance"


def _format_interval(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    elif seconds < 3600:
        return f"{seconds // 60}m"
    elif seconds < 86400:
        return f"{seconds // 3600}h"
    else:
        return f"{seconds // 86400}d"


def _render_timeline_svg(state: dict, tasks: list[dict]):
    """Render a 24h timeline as SVG showing task executions."""
    now = int(time.time())
    day_start = now - 24 * 3600  # 24h ago

    # Build task info lookup
    task_info = {t["name"]: t for t in tasks}

    # Group tasks by category, sorted
    categorized = {}
    for task_name, task_state in state.items():
        cat = _categorize_task(task_name)
        if cat not in categorized:
            categorized[cat] = []
        categorized[cat].append((task_name, task_state))

    # SVG dimensions
    left_margin = 160
    width = 800
    row_height = 24
    header_height = 30
    total_rows = len(state)
    svg_height = header_height + total_rows * row_height + 40

    parts = [
        f'<svg viewBox="0 0 {width} {svg_height}" '
        f'style="width:100%;max-width:{width}px;height:auto;font-family:-apple-system,sans-serif">',
        # Background
        f'<rect width="{width}" height="{svg_height}" fill="white"/>',
    ]

    # Hour markers
    timeline_width = width - left_margin - 20
    for h in range(25):
        x = left_margin + (h / 24) * timeline_width
        hour_label = (now_local() - timedelta(hours=24 - h)).strftime("%H")
        parts.append(
            f'<line x1="{x:.0f}" y1="{header_height}" '
            f'x2="{x:.0f}" y2="{svg_height - 20}" stroke="#f3f4f6" stroke-width="1"/>'
        )
        if h % 3 == 0:
            parts.append(
                f'<text x="{x:.0f}" y="{header_height - 5}" font-size="9" '
                f'fill="#9ca3af" text-anchor="middle">{hour_label}:00</text>'
            )

    # "Now" marker
    now_x = left_margin + timeline_width
    parts.append(
        f'<line x1="{now_x:.0f}" y1="{header_height}" '
        f'x2="{now_x:.0f}" y2="{svg_height - 20}" stroke="#ef4444" '
        f'stroke-width="1" stroke-dasharray="4,2"/>'
    )
    parts.append(
        f'<text x="{now_x:.0f}" y="{svg_height - 8}" font-size="9" '
        f'fill="#ef4444" text-anchor="middle">now</text>'
    )

    # Render rows
    row = 0
    for cat in sorted(categorized.keys()):
        cat_tasks = sorted(categorized[cat], key=lambda x: x[0])
        color = CATEGORY_COLORS.get(cat, "#9ca3af")

        for task_name, task_state in cat_tasks:
            y = header_height + row * row_height
            last_run = task_state.get("last_run", 0)

            # Task label
            display_name = task_name[:20]
            parts.append(
                f'<text x="{left_margin - 8}" y="{y + 16}" font-size="10" '
                f'fill="#374151" text-anchor="end">{display_name}</text>'
            )

            # Execution dot (at last_run time)
            if last_run > day_start:
                t = (last_run - day_start) / (24 * 3600)
                dot_x = left_margin + t * timeline_width

                # Estimate run duration from interval (rough: 5% of interval)
                info = task_info.get(task_name, {})
                interval = info.get("interval", 600)
                est_duration = min(interval * 0.05, 120)  # cap at 2min visual
                bar_w = max((est_duration / (24 * 3600)) * timeline_width, 6)

                parts.append(
                    f'<rect x="{dot_x:.0f}" y="{y + 6}" width="{bar_w:.0f}" '
                    f'height="12" rx="3" fill="{color}" opacity="0.8"/>'
                )

                # Show how long ago
                ago = now - last_run
                ago_str = _format_interval(ago)
                parts.append(
                    f'<text x="{dot_x + bar_w + 4:.0f}" y="{y + 16}" font-size="8" '
                    f'fill="#9ca3af">{ago_str}</text>'
                )
            else:
                # No run in 24h — show warning
                parts.append(
                    f'<text x="{left_margin + 4}" y="{y + 16}" font-size="9" '
                    f'fill="#ef4444">no run in 24h</text>'
                )

            row += 1

    # Legend
    legend_y = svg_height - 5
    legend_x = left_margin
    for cat, color in sorted(CATEGORY_COLORS.items()):
        if cat in categorized:
            parts.append(
                f'<rect x="{legend_x}" y="{legend_y - 8}" width="8" height="8" '
                f'rx="2" fill="{color}"/>'
            )
            parts.append(
                f'<text x="{legend_x + 12}" y="{legend_y}" font-size="9" '
                f'fill="#6b7280">{cat}</text>'
            )
            legend_x += len(cat) * 7 + 30

    parts.append("</svg>")
    return "\n".join(parts)


@ui.page("/agent-calendar")
def agent_calendar_page():
    """Agent activity calendar page."""
    ui.add_head_html("""
    <style>
        .summary-card { background: #f9fafb; border-radius: 12px; padding: 1rem; }
    </style>
    """)

    with ui.column().classes("w-full max-w-5xl mx-auto p-4 gap-4"):
        with ui.row().classes("w-full items-center justify-between"):
            ui.link("← Home", "/").classes("text-gray-500 text-sm")
            ui.label("Agent Calendar").classes("text-2xl font-bold")

        state = _load_heartbeat_state()
        tasks = _load_heartbeat_tasks()
        now = int(time.time())

        # --- Layer 1: Glanceable Summary ---
        total = len(state)
        ran_1h = sum(1 for s in state.values()
                     if now - s.get("last_run", 0) < 3600)
        ran_24h = sum(1 for s in state.values()
                      if now - s.get("last_run", 0) < 86400)
        overdue = sum(1 for s in state.values()
                      if now - s.get("last_run", 0) > 86400)

        with ui.row().classes("w-full gap-4"):
            with ui.card().classes("summary-card flex-1"):
                ui.label(str(total)).classes("text-2xl font-bold")
                ui.label("total tasks").classes("text-xs text-gray-500")
            with ui.card().classes("summary-card flex-1"):
                ui.label(str(ran_1h)).classes("text-2xl font-bold text-green-600")
                ui.label("ran in last 1h").classes("text-xs text-gray-500")
            with ui.card().classes("summary-card flex-1"):
                ui.label(str(ran_24h)).classes("text-2xl font-bold text-blue-600")
                ui.label("ran in last 24h").classes("text-xs text-gray-500")
            with ui.card().classes("summary-card flex-1"):
                color = "text-red-600" if overdue else "text-gray-600"
                ui.label(str(overdue)).classes(f"text-2xl font-bold {color}")
                ui.label("overdue (>24h)").classes("text-xs text-gray-500")

        # --- Layer 2: 24h Timeline ---
        ui.label("Last 24 Hours").classes("text-lg font-semibold mt-4")

        if state:
            with ui.card().classes("w-full p-4 overflow-x-auto"):
                svg = _render_timeline_svg(state, tasks)
                ui.html(svg)
        else:
            ui.label("No heartbeat state found.").classes(
                "text-gray-400 text-sm italic"
            )

        # --- Task List (sortable) ---
        ui.label("All Tasks").classes("text-lg font-semibold mt-4")

        task_info = {t["name"]: t for t in tasks}
        sorted_tasks = sorted(
            state.items(),
            key=lambda x: x[1].get("last_run", 0),
            reverse=True,
        )

        for task_name, task_state in sorted_tasks:
            last_run = task_state.get("last_run", 0)
            ago = now - last_run if last_run else None
            info = task_info.get(task_name, {})
            interval = info.get("interval", 0)
            cat = _categorize_task(task_name)
            color = CATEGORY_COLORS.get(cat, "#9ca3af")

            with ui.row().classes("w-full items-center gap-3 py-1"):
                ui.html(
                    f'<span style="display:inline-block;width:10px;height:10px;'
                    f'border-radius:50%;background:{color}"></span>'
                )
                ui.label(task_name).classes("text-sm font-medium w-48")
                if interval:
                    ui.label(f"every {_format_interval(interval)}").classes(
                        "text-xs text-gray-400 w-20"
                    )
                if ago is not None:
                    if ago < 3600:
                        badge_color = "green"
                    elif ago < 86400:
                        badge_color = "blue"
                    else:
                        badge_color = "red"
                    ui.badge(
                        f"{_format_interval(ago)} ago", color=badge_color
                    ).classes("text-xs")
                else:
                    ui.badge("never", color="gray").classes("text-xs")

        # Navigation
        ui.separator()
        with ui.row().classes("gap-4"):
            ui.link("Home", "/").classes("text-blue-600")
            ui.link("Tasks", "/tasks").classes("text-blue-600")
            ui.link("Thinking", "/thinking").classes("text-purple-600")
