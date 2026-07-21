"""开放问题 — 还没想明白的事 + 进行中的个人项目。

Two sections:
- 开放问题: things the user hasn't figured out yet, with hill chart maturity
- 个人项目: ongoing life projects with progress tracking
"""

import html as html_mod
import re

import yaml
from nicegui import ui

from core.claude_projects import auto_memory_dir

from ..uiutil import jarvis_page

AUTO_MEMORY = auto_memory_dir() / "warm"

# 山丘图配色引用 CSS 变量——SVG 的 fill/stroke 认 var()，且跟暗色一起翻转。
_HILL_COLORS = {
    "raw": "#9aa9ae",            # 中性灰（status-dot 同源，两种模式都可读）
    "exploring": "var(--focus)",
    "crystallizing": "var(--gold)",
    "decided": "var(--jade)",
}


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
        f'fill="none" stroke="var(--mist)" stroke-width="2"/>',
        # Labels
        f'<text x="60" y="{base_y + 20}" font-size="11" fill="var(--ink-soft)">还在想</text>',
        f'<text x="{peak_x - 15}" y="{peak_y - 10}" font-size="11" fill="var(--ink-soft)">⛰️</text>',
        f'<text x="{width - 110}" y="{base_y + 20}" font-size="11" fill="var(--ink-soft)">快想清楚了</text>',
    ]

    for i, q in enumerate(questions):
        pos = _hill_position(q.get("status", "raw"))
        # Map position to x coordinate along the curve
        t = pos / 100.0
        x = 20 + t * (width - 40)
        # Parabolic y: peaks at t=0.5
        y = base_y - 4 * (base_y - peak_y) * t * (1 - t)
        color = _HILL_COLORS.get(q.get("status", "raw"), "#9aa9ae")
        name = str(q.get("name", "?"))
        if len(name) > 12:
            name = name[:11] + "…"
        # name comes from user-editable YAML — escape before it enters raw SVG.
        name = html_mod.escape(name)
        svg_parts.append(
            f'<circle cx="{x:.0f}" cy="{y:.0f}" r="6" fill="{color}"/>'
        )
        svg_parts.append(
            f'<text x="{x:.0f}" y="{y - 12:.0f}" font-size="10" fill="var(--ink-soft)" '
            f'text-anchor="middle">{name}</text>'
        )

    svg_parts.append("</svg>")
    ui.html("\n".join(svg_parts))


@ui.page("/thinking")
def thinking_page():
    """开放问题 + 个人项目。"""
    with jarvis_page("/thinking", "开放问题",
                     "还没想明白的事，慢慢想；在做的个人项目，看得见进展。"):

        # --- 开放问题 ---
        with ui.column().classes("w-full gap-3"):
            ui.label("壹 · 问题").classes("section-kicker")
            ui.label("还没想明白的事").classes("section-title")

            questions = _load_typed_files("question")

            # 山丘图：越过山顶=想清楚了大半
            if questions:
                with ui.card().classes("w-full p-4"):
                    ui.label("成熟度地形图——左坡在摸索，右坡在收敛").classes(
                        "section-note mb-2")
                    _render_hill_chart(questions)

            # Question cards
            for q in questions:
                status = q.get("status", "raw")
                with ui.card().classes("w-full p-4"):
                    with ui.row().classes("w-full items-center justify-between"):
                        with ui.column().classes("gap-0 flex-1"):
                            ui.label(q.get("name", "（未命名）")).classes("font-medium")
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
                        ui.label(recent).classes(
                            "section-note whitespace-pre-wrap mt-2")

                    # Metadata
                    with ui.row().classes("gap-4 mt-2"):
                        updated = q.get("updated", "")
                        if updated:
                            ui.label(f"更新：{updated}").classes(
                                "text-xs text-gray-400")
                        related = q.get("related", [])
                        if related:
                            ui.label(f"关联 {len(related)} 个").classes(
                                "text-xs text-gray-400"
                            )

            if not questions:
                ui.label("现在没有挂着的开放问题——想到什么随时记进记忆里。").classes(
                    "empty-guidance")

        # --- 个人项目 ---
        with ui.column().classes("w-full gap-3"):
            ui.label("贰 · 项目").classes("section-kicker")
            ui.label("在做的个人项目").classes("section-title")

            projects = _load_typed_files("project")

            for p in projects:
                status = p.get("status", "not_started")
                with ui.card().classes("w-full p-4"):
                    with ui.row().classes("w-full items-center justify-between"):
                        with ui.column().classes("gap-0 flex-1"):
                            ui.label(p.get("name", "（未命名）")).classes("font-medium")
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

                    # 下一步：只要写了就展示。（旧版按「含'待'」隐藏，把
                    # “等待回复”这类真实下一步整条吞掉了——已移除。）
                    body = p.get("_body", "")
                    next_action = _extract_section(body, "Next Action")
                    if next_action:
                        with ui.row().classes("items-center gap-2 mt-2"):
                            ui.label("下一步").classes(
                                "text-xs font-bold whitespace-nowrap")
                            ui.label(next_action).classes("text-sm")

                    # Metadata
                    updated = p.get("updated", "")
                    if updated:
                        ui.label(f"更新：{updated}").classes(
                            "text-xs text-gray-400 mt-1")

            if not projects:
                ui.label("现在没有登记中的个人项目。").classes("empty-guidance")
