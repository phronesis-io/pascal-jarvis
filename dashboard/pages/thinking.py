"""Thinking page — Open Questions + Personal Projects.

Two sections:
- Open Questions: things Pascal hasn't figured out yet, with hill chart maturity
- Personal Projects: ongoing life projects with progress tracking
"""

import re
from datetime import datetime
from pathlib import Path

import yaml
from nicegui import ui

# Auto-memory directory (source of truth)
AUTO_MEMORY = Path.home() / ".claude/projects/-Users-pascal-Desktop-jarvis/memory/warm"


def _load_typed_files(file_type: str) -> list[dict]:
    """Load markdown files with YAML frontmatter matching a given type."""
    results = []
    if not AUTO_MEMORY.exists():
        return results
    for f in sorted(AUTO_MEMORY.glob("*.md")):
        try:
            text = f.read_text()
            if not text.startswith("---"):
                continue
            # Parse YAML frontmatter
            parts = text.split("---", 2)
            if len(parts) < 3:
                continue
            meta = yaml.safe_load(parts[1])
            if not meta or meta.get("type") != file_type:
                continue
            body = parts[2].strip()
            meta["_body"] = body
            meta["_path"] = str(f)
            meta["_filename"] = f.name
            results.append(meta)
        except Exception:
            continue
    return results


def _status_color(status: str) -> str:
    """Map status to badge color."""
    return {
        "raw": "gray",
        "exploring": "blue",
        "crystallizing": "amber",
        "decided": "green",
        "not_started": "gray",
        "in_progress": "blue",
        "paused": "amber",
        "completed": "green",
    }.get(status, "gray")


def _status_label(status: str) -> str:
    """Human-readable status."""
    return {
        "raw": "刚提出",
        "exploring": "在探索",
        "crystallizing": "在成型",
        "decided": "已决策",
        "not_started": "未开始",
        "in_progress": "进行中",
        "paused": "暂停",
        "completed": "已完成",
    }.get(status, status)


def _hill_position(status: str) -> int:
    """Map status to hill chart position (0-100). 50 = peak."""
    return {
        "raw": 15,
        "exploring": 35,
        "crystallizing": 65,
        "decided": 85,
    }.get(status, 50)


def _extract_section(body: str, heading: str) -> str:
    """Extract content under a markdown heading."""
    pattern = rf"## {re.escape(heading)}\s*\n(.*?)(?=\n## |\Z)"
    match = re.search(pattern, body, re.DOTALL)
    return match.group(1).strip() if match else ""


def _render_hill_chart(questions: list[dict]):
    """Render an SVG hill chart showing question maturity."""
    if not questions:
        return

    width, height = 600, 200
    peak_x, peak_y = 300, 40
    base_y = 170

    # Build SVG
    svg_parts = [
        f'<svg viewBox="0 0 {width} {height}" '
        f'style="width:100%;max-width:{width}px;height:auto">',
        # Hill curve
        f'<path d="M 20 {base_y} Q {peak_x} {peak_y - 60} {width - 20} {base_y}" '
        f'fill="none" stroke="#e5e7eb" stroke-width="2"/>',
        # Labels
        f'<text x="60" y="{base_y + 20}" font-size="11" fill="#9ca3af">还在想</text>',
        f'<text x="{peak_x - 15}" y="{peak_y - 10}" font-size="11" fill="#9ca3af">⛰️</text>',
        f'<text x="{width - 110}" y="{base_y + 20}" font-size="11" fill="#9ca3af">快想清楚了</text>',
    ]

    colors = {"raw": "#9ca3af", "exploring": "#3b82f6",
              "crystallizing": "#f59e0b", "decided": "#22c55e"}

    for i, q in enumerate(questions):
        pos = _hill_position(q.get("status", "raw"))
        # Map position to x coordinate along the curve
        t = pos / 100.0
        x = 20 + t * (width - 40)
        # Parabolic y: peaks at t=0.5
        y = base_y - 4 * (base_y - peak_y) * t * (1 - t)
        color = colors.get(q.get("status", "raw"), "#9ca3af")
        name = q.get("name", "?")
        if len(name) > 12:
            name = name[:11] + "…"
        svg_parts.append(
            f'<circle cx="{x:.0f}" cy="{y:.0f}" r="6" fill="{color}"/>'
        )
        svg_parts.append(
            f'<text x="{x:.0f}" y="{y - 12:.0f}" font-size="10" fill="#374151" '
            f'text-anchor="middle">{name}</text>'
        )

    svg_parts.append("</svg>")
    ui.html("\n".join(svg_parts))


@ui.page("/thinking")
def thinking_page():
    """Open Questions + Personal Projects page."""
    ui.add_head_html("""
    <style>
        .q-card { border: 1px solid #e5e7eb; border-radius: 8px; padding: 0.75rem;
                  transition: all 0.15s; }
        .q-card:hover { border-color: #3b82f6; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }
        .q-card-question { border-left: 3px solid #3b82f6; }
        .q-card-project { border-left: 3px solid #8b5cf6; }
        .section-body { font-size: 0.85rem; color: #4b5563; white-space: pre-wrap; }
    </style>
    """)

    with ui.column().classes("w-full max-w-4xl mx-auto p-4 gap-4"):
        with ui.row().classes("w-full items-center justify-between"):
            ui.link("← Home", "/").classes("text-gray-500 text-sm")
            ui.label("Thinking").classes("text-2xl font-bold")

        # --- Open Questions ---
        ui.label("Open Questions").classes("text-lg font-semibold mt-2")
        ui.label("还没想明白的事，慢慢想").classes("text-xs text-gray-500")

        questions = _load_typed_files("question")

        # Hill Chart
        if questions:
            with ui.card().classes("w-full p-4"):
                ui.label("Hill Chart").classes("text-sm font-medium text-gray-600 mb-2")
                _render_hill_chart(questions)

        # Question cards
        for q in questions:
            status = q.get("status", "raw")
            with ui.card().classes("q-card q-card-question w-full"):
                with ui.row().classes("w-full items-center justify-between"):
                    with ui.column().classes("gap-0 flex-1"):
                        ui.label(q.get("name", "Untitled")).classes("font-medium")
                        area = q.get("area", "")
                        if area:
                            ui.label(f"#{area}").classes("text-xs text-gray-400")
                    ui.badge(
                        _status_label(status), color=_status_color(status)
                    ).classes("text-xs")

                # Show latest thinking
                body = q.get("_body", "")
                thinking = _extract_section(body, "思考记录")
                if thinking:
                    # Show last 3 lines
                    lines = [l for l in thinking.split("\n") if l.strip()]
                    recent = "\n".join(lines[-3:])
                    ui.label(recent).classes("section-body mt-2")

                # Metadata
                with ui.row().classes("gap-4 mt-2"):
                    updated = q.get("updated", "")
                    if updated:
                        ui.label(f"更新: {updated}").classes("text-xs text-gray-400")
                    related = q.get("related", [])
                    if related:
                        ui.label(f"关联: {len(related)} 个").classes(
                            "text-xs text-gray-400"
                        )

        if not questions:
            ui.label("暂无 open questions").classes("text-gray-400 text-sm italic")

        # --- Personal Projects ---
        ui.separator()
        ui.label("Personal Projects").classes("text-lg font-semibold mt-2")
        ui.label("在做的个人项目").classes("text-xs text-gray-500")

        projects = _load_typed_files("project")

        for p in projects:
            status = p.get("status", "not_started")
            with ui.card().classes("q-card q-card-project w-full"):
                with ui.row().classes("w-full items-center justify-between"):
                    with ui.column().classes("gap-0 flex-1"):
                        ui.label(p.get("name", "Untitled")).classes("font-medium")
                        goal = p.get("goal", "")
                        if goal:
                            ui.label(goal).classes("text-xs text-gray-500")
                    with ui.column().classes("items-end gap-1"):
                        ui.badge(
                            _status_label(status), color=_status_color(status)
                        ).classes("text-xs")
                        area = p.get("area", "")
                        if area:
                            ui.label(f"#{area}").classes("text-xs text-gray-400")

                # Show next action
                body = p.get("_body", "")
                next_action = _extract_section(body, "Next Action")
                if next_action and "待" not in next_action:
                    with ui.row().classes("items-center gap-2 mt-2"):
                        ui.label("→").classes("text-blue-500 font-bold")
                        ui.label(next_action).classes("text-sm")

                # Metadata
                updated = p.get("updated", "")
                if updated:
                    ui.label(f"更新: {updated}").classes("text-xs text-gray-400 mt-1")

        if not projects:
            ui.label("暂无 personal projects").classes(
                "text-gray-400 text-sm italic"
            )

        # Navigation
        ui.separator()
        with ui.row().classes("gap-4"):
            ui.link("Home", "/").classes("text-blue-600")
            ui.link("Tasks", "/tasks").classes("text-blue-600")
            ui.link("Agent Calendar", "/agent-calendar").classes("text-blue-600")
