"""更多 — 次级面板入口与真实配置。

The previous incarnation was a placebo settings page: it wrote 7 keys into
kv_store (0 rows ever written in production, zero consumers anywhere in the
codebase) while claiming "changes take effect immediately". Those controls
are gone (REQ-43/46). What remains is the truth: where each behavior is
actually configured, and its current live value.

This page also adopts the four secondary surfaces (用量/互动/开放问题/任务健康)
— /usage and /engagement previously had zero inbound links anywhere.

The 手机接入 section (pairing, Web Push, tailnet entry) is gone with the
mobile gateway (REQ-120, 2026-08-11): buttons that can no longer lead
anywhere are not rendered.
"""

import json
import time
from pathlib import Path

from nicegui import ui

from ..uiutil import jarvis_page

JARVIS_DIR = Path(__file__).parent.parent.parent

# 次级面板入口：名称、路由、一句人话说明。
_PANELS = [
    ("EigenFlux 网络", "/eigenflux", "看待批广播、已发记录、网络链路和官方后台。"),
    ("用量", "/usage", "这台电脑上 Claude 的使用量：会话、消耗、什么时段最活跃。"),
    ("互动", "/engagement", "Jarvis 主动发的消息，你看了没、回了没，哪些来源在打扰你。"),
    ("开放问题", "/thinking", "还没想明白的事，和进行中的个人项目。"),
    ("任务健康", "/agent-calendar", "每个后台任务实际跑没跑、失败了几次、卡在哪。"),
]


def _quiet_hours() -> tuple[str, str]:
    """Live quiet-hours window from the shared policy that enforces it."""
    from core.attention_policy import quiet_window_labels
    return quiet_window_labels()


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
            "applied_at": time.strftime("%Y-%m-%d %H:%M", time.localtime(applied)) if applied else "未记录",
        })
    return out


def _fmt_secs(secs) -> str:
    if secs is None:
        return "未知"
    if secs < 3600:
        return f"{secs // 60} 分钟"
    if secs < 86400:
        return f"{secs / 3600:g} 小时"
    return f"{secs / 86400:g} 天"


def _section(kicker: str, title: str, note: str = "") -> None:
    ui.label(kicker).classes("section-kicker")
    ui.label(title).classes("section-title")
    if note:
        ui.label(note).classes("section-note")


@ui.page("/settings")
def settings_page():
    """次级面板入口、只读配置真相和数据搬家动作。"""
    with jarvis_page("/settings", "更多",
                     "次级面板与真实生效的配置。"):

        # ── 次级面板入口 ──
        with ui.column().classes("w-full gap-3"):
            _section("壹 · 面板", "更多面板", "不常看、但要用时得找得到的几块。")
            with ui.element("div").classes("memorial-grid"):
                for name, href, desc in _PANELS:
                    with ui.link(target=href).classes("no-underline"):
                        with ui.card().classes("memorial-card w-full is-decided"
                                               " cursor-pointer"):
                            ui.label(name).classes("memorial-title")
                            ui.label(desc).classes("memorial-body")
                            ui.label("进入 →").classes("memorial-source")

        # ── 真实配置 ──
        with ui.column().classes("w-full gap-3"):
            _section("贰 · 配置", "真实生效的配置",
                     "每一项在哪里生效、要改去哪里改，下面如实写明。")

            # 免打扰时段 — core/heartbeat_loop.py 常量强制执行
            with ui.card().classes("w-full p-4"):
                start, end = _quiet_hours()
                ui.label("免打扰时段").classes("font-medium")
                ui.label(f"{start} → {end}（不着急的消息排队到次日再发）").classes(
                    "section-note")
                ui.label(
                    "生效机制：core/attention_policy.py 的统一静默时段，心跳和 "
                    "Push 共用。默认值在代码中；启动环境可用 "
                    "JARVIS_QUIET_START / JARVIS_QUIET_END 覆盖。"
                ).classes("text-xs text-gray-500")

            # 自动调频 — engagement-analyze 写 interval_overrides.json
            with ui.card().classes("w-full p-4"):
                ui.label("任务频率的自动微调").classes("font-medium")
                overrides = _interval_overrides()
                if overrides:
                    for o in overrides:
                        with ui.row().classes("w-full items-center justify-between"):
                            ui.label(o["task"]).classes("text-sm")
                            ui.label(
                                f"基准每 {_fmt_secs(o['base_s'])} → 现在每 "
                                f"{_fmt_secs(o['override_s'])} · 调整于 {o['applied_at']}"
                            ).classes("text-xs text-gray-500")
                else:
                    ui.label("当前没有微调——所有任务按 HEARTBEAT.md 的基准频率跑。").classes(
                        "section-note")
                ui.label(
                    "生效机制：engagement-analyze 任务根据互动数据写 "
                    "interval_overrides.json（3 天内不重复加码），心跳每轮读取。"
                    "手工调整：直接编辑 interval_overrides.json。"
                ).classes("text-xs text-gray-500 mt-1")

            # 任务节奏事实源
            with ui.card().classes("w-full p-4"):
                ui.label("任务节奏").classes("font-medium")
                ui.label("HEARTBEAT.md 是所有心跳任务的频率和内容的事实源。").classes(
                    "section-note")
                ui.label(
                    "查看运行情况：任务健康页。编辑：admin (:3456) 的心跳编辑器，"
                    "或直接改仓库里的 HEARTBEAT.md。"
                ).classes("text-xs text-gray-500")
                ui.link("任务健康 →", "/agent-calendar").classes("text-sm")

            # 系统信息（jarvis.yaml 只读摘要）
            config_path = JARVIS_DIR / "jarvis.yaml"
            if config_path.exists():
                with ui.card().classes("w-full p-4"):
                    ui.label("系统信息").classes("font-medium")
                    try:
                        import yaml
                    except ImportError:
                        ui.label("这台机器没装 PyYAML，读不了 jarvis.yaml——"
                                 "跳过这一块。").classes("section-note")
                    else:
                        try:
                            with open(config_path) as f:
                                config = yaml.safe_load(f)
                            if not isinstance(config, dict):
                                config = {}
                            ui.label(f"工作目录：{config.get('work_dir', '未设置')}").classes(
                                "text-xs font-mono")
                            ui.label("心跳模型："
                                     f"{config.get('claude', {}).get('heartbeat_model', '未设置')}").classes(
                                "text-xs font-mono")
                            ui.label("检查间隔："
                                     f"{config.get('heartbeat', {}).get('check_interval', '未设置')} 秒").classes(
                                "text-xs font-mono")
                            ui.label("Admin 端口："
                                     f"{config.get('admin', {}).get('port', '未设置')}").classes(
                                "text-xs font-mono")
                            ui.label("生效机制：jarvis.yaml，bot 启动时读取。").classes(
                                "text-xs text-gray-500 mt-1")
                        except (OSError, yaml.YAMLError):
                            ui.label("jarvis.yaml 存在但读不出来（文件损坏或没有读取权限）。"
                                     "修好文件后刷新这一页。").classes("section-note")

        # ── 数据操作（真实动作，保留） ──
        with ui.column().classes("w-full gap-3"):
            _section("叁 · 数据", "数据搬家")

            async def migrate_watchlater():
                from ..bookmark_pipeline import migrate_from_jsonl
                wl_path = JARVIS_DIR.parent / "memory" / "system" / "watchlater.jsonl"
                if not wl_path.exists():
                    # Try alternative path
                    from core.claude_projects import heartbeat_memory_dir
                    wl_path = heartbeat_memory_dir() / "system" / "watchlater.jsonl"
                count = migrate_from_jsonl(str(wl_path))
                ui.notify(f"已从 watchlater.jsonl 导入 {count} 条收藏", type="positive")

            ui.label("把旧的稍后看清单（watchlater.jsonl）导入收藏库。已导入过的不会重复。").classes(
                "section-note")
            ui.button("导入旧收藏", on_click=migrate_watchlater).classes(
                "memorial-secondary").props("outline no-caps")
