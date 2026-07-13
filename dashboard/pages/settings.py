"""Settings page — honest, read-only view of REAL configuration.

The previous page was a placebo: it wrote 7 keys into kv_store (0 rows ever
written in production, zero consumers anywhere in the codebase) while
claiming "changes take effect immediately". Those controls are gone
(REQ-43/46). What remains is the truth: where each behavior is actually
configured, and its current live value.
"""

import json
import time
from pathlib import Path

from nicegui import ui

JARVIS_DIR = Path(__file__).parent.parent.parent


def _quiet_hours() -> tuple[str, str]:
    """Live quiet-hours window from the code constants that enforce it."""
    from core.heartbeat_loop import QUIET_START_MIN, QUIET_END_MIN
    fmt = lambda m: f"{m // 60:02d}:{m % 60:02d}"  # noqa: E731
    return fmt(QUIET_START_MIN), fmt(QUIET_END_MIN)


def _interval_overrides(jarvis_dir: Path | None = None) -> list[dict]:
    """interval_overrides.json + applied-at metadata, joined with base intervals."""
    jd = jarvis_dir or JARVIS_DIR
    try:
        overrides = json.loads((jd / "interval_overrides.json").read_text())
    except (OSError, json.JSONDecodeError, ValueError):
        overrides = {}
    try:
        meta = json.loads((jd / "interval_overrides_meta.json").read_text())
    except (OSError, json.JSONDecodeError, ValueError):
        meta = {}
    base = {}
    try:
        from core.heartbeat import parse_heartbeat
        hb = jd / "HEARTBEAT.md"
        if hb.exists():
            base = {t["name"]: t["interval"] for t in parse_heartbeat(hb)}
    except ImportError:
        pass
    out = []
    for task, secs in sorted(overrides.items()):
        applied = meta.get(task)
        out.append({
            "task": task,
            "override_s": secs,
            "base_s": base.get(task),
            "applied_at": time.strftime("%Y-%m-%d %H:%M", time.localtime(applied)) if applied else "?",
        })
    return out


def _fmt_secs(secs) -> str:
    if secs is None:
        return "?"
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs / 3600:g}h"
    return f"{secs / 86400:g}d"


@ui.page("/settings")
def settings_page():
    """Honest configuration view (read-only)."""
    with ui.column().classes("w-full max-w-4xl mx-auto p-4 gap-4"):
        with ui.row().classes("w-full items-center justify-between"):
            ui.link("← Home", "/").classes("text-gray-500 text-sm")
            ui.label("Settings").classes("text-2xl font-bold")

        ui.label(
            "这里只展示真实生效的配置和它的来源。这一页改不了任何东西——"
            "每项配置在哪里改，下面如实写明。"
        ).classes("text-sm text-gray-600")

        # ── Quiet hours — enforced by core/heartbeat_loop.py constants ──
        ui.label("Quiet Hours").classes("text-lg font-semibold mt-2")
        start, end = _quiet_hours()
        with ui.card().classes("w-full p-3"):
            ui.label(f"{start} → {end}（非紧急消息排队到次日）").classes("font-medium text-sm")
            ui.label(
                "生效机制：core/heartbeat_loop.py 的 QUIET_START_MIN / QUIET_END_MIN "
                "常量。要改就改代码并重启 bot——没有运行时开关。"
            ).classes("text-xs text-gray-500")

        # ── Interval overrides — written by engagement-analyze auto-tuning ──
        ui.label("Interval Overrides（自动调频）").classes("text-lg font-semibold mt-4")
        overrides = _interval_overrides()
        with ui.card().classes("w-full p-3"):
            if overrides:
                for o in overrides:
                    with ui.row().classes("w-full items-center justify-between"):
                        ui.label(o["task"]).classes("font-medium text-sm")
                        ui.label(
                            f"基准 {_fmt_secs(o['base_s'])} → 现在 {_fmt_secs(o['override_s'])}"
                            f" · 调整于 {o['applied_at']}"
                        ).classes("text-xs text-gray-500")
            else:
                ui.label("当前无覆盖——所有任务按 HEARTBEAT.md 基准间隔跑。").classes(
                    "text-xs text-gray-400")
            ui.label(
                "生效机制：engagement-analyze 任务根据互动数据写 interval_overrides.json"
                "（interval_overrides_meta.json 记录调整时间，3 天内不重复加码），"
                "心跳每轮读取。手工调整：直接编辑 interval_overrides.json。"
            ).classes("text-xs text-gray-500 mt-2")

        # ── Task schedule source of truth ──
        ui.label("任务节奏").classes("text-lg font-semibold mt-4")
        with ui.card().classes("w-full p-3"):
            ui.label("HEARTBEAT.md 是所有心跳任务的间隔/prompt 事实源。").classes("text-sm")
            ui.label(
                "查看运行健康：Task Health 页。编辑：admin (:3456) 的心跳编辑器，"
                "或直接改仓库里的 HEARTBEAT.md。"
            ).classes("text-xs text-gray-500")
            ui.link("→ Task Health", "/agent-calendar").classes("text-blue-600 text-sm")

        # System info（jarvis.yaml 只读摘要）
        ui.separator()
        ui.label("System Info").classes("text-lg font-semibold mt-4")

        config_path = JARVIS_DIR / "jarvis.yaml"
        if config_path.exists():
            try:
                import yaml
                with open(config_path) as f:
                    config = yaml.safe_load(f)
                with ui.card().classes("w-full p-3"):
                    ui.label(f"Work dir: {config.get('work_dir', 'N/A')}").classes("text-xs font-mono")
                    ui.label(f"Heartbeat model: {config.get('claude', {}).get('heartbeat_model', 'N/A')}").classes("text-xs font-mono")
                    ui.label(f"Check interval: {config.get('heartbeat', {}).get('check_interval', 'N/A')}s").classes("text-xs font-mono")
                    ui.label(f"Admin port: {config.get('admin', {}).get('port', 'N/A')}").classes("text-xs font-mono")
                    ui.label("生效机制：jarvis.yaml，bot 启动时读取。").classes("text-xs text-gray-500 mt-1")
            except ImportError:
                ui.label("PyYAML not installed — skipping config display").classes("text-xs text-gray-400")

        # Data management — real action, kept
        ui.separator()
        ui.label("Data").classes("text-lg font-semibold mt-4")

        async def migrate_watchlater():
            from ..bookmark_pipeline import migrate_from_jsonl
            wl_path = JARVIS_DIR.parent / "memory" / "system" / "watchlater.jsonl"
            if not wl_path.exists():
                # Try alternative path
                from core.claude_projects import heartbeat_memory_dir
                wl_path = heartbeat_memory_dir() / "system" / "watchlater.jsonl"
            count = migrate_from_jsonl(str(wl_path))
            ui.notify(f"Migrated {count} bookmarks from watchlater.jsonl", type="positive")

        ui.button("Migrate watchlater.jsonl → SQLite", on_click=migrate_watchlater)
