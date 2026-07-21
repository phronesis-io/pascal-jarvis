"""任务 — 排程定义（HEARTBEAT.md）+ 动态任务的登记与查看。

REQ-44: the permanently-empty "Dynamic Tasks" listing and the fictional
on-time/due/overdue badges are gone; run health lives on 任务健康
(/agent-calendar), which reads sched_events.jsonl.

This revision closes the silent-death UX of the register form: a registered
dynamic task now shows up in a list right here (scheduled_tasks table), with
a delete action — before, a registration was write-only and appeared nowhere.
"""

import json
from datetime import datetime
from pathlib import Path

from nicegui import ui

from ..db import task_delete, task_list, task_register
from ..uiutil import jarvis_page

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
    """秒数 → 人话频率。"""
    if seconds < 60:
        return f"{seconds} 秒"
    elif seconds < 3600:
        return f"{seconds // 60} 分钟"
    elif seconds < 86400:
        return f"{seconds // 3600} 小时"
    else:
        return f"{seconds // 86400} 天"


def _humanize_trigger(task: dict) -> str:
    """scheduled_tasks 行 → 人话的触发描述。不猜数据，读不出就如实说。"""
    tt = str(task.get("trigger_type", "") or "")
    raw = task.get("trigger_config") or "{}"
    try:
        cfg = json.loads(raw) if isinstance(raw, str) else dict(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        cfg = {}
    if tt == "cron":
        expr = cfg.get("expression", "")
        return f"按时间表 {expr}" if expr else "按时间表（配置缺失）"
    if tt == "interval":
        secs = cfg.get("seconds")
        try:
            return f"每 {_format_interval(int(secs))}"
        except (TypeError, ValueError):
            return "固定间隔（配置缺失）"
    if tt == "date":
        dt = cfg.get("datetime", "")
        return f"{dt} 提醒一次" if dt else "指定时间（配置缺失）"
    return tt or "未知触发方式"


def _validate_trigger(trigger_type: str, value: str) -> str | None:
    """Client-side sanity check before hitting the DB. Returns 人话错误或 None。"""
    if not value:
        return "触发配置不能为空"
    if trigger_type == "cron":
        if len(value.split()) != 5:
            return "cron 表达式要 5 段（分 时 日 月 周），例如 0 6 * * *"
    elif trigger_type == "interval":
        if not value.isdigit() or int(value) <= 0:
            return "间隔要填正整数秒数，例如 600"
    elif trigger_type == "date":
        try:
            datetime.fromisoformat(value)
        except ValueError:
            return "时间格式看不懂，写成 2026-05-22T06:00 这样"
    return None


@ui.page("/tasks")
def tasks_page():
    """排程定义 + 动态任务登记。"""
    with jarvis_page("/tasks", "任务",
                     "这页是排程的定义。任务实际跑没跑、失败了没——看任务健康。"):

        # ── 我登记的动态任务（scheduled_tasks 表） ──
        with ui.column().classes("w-full gap-3"):
            ui.label("壹 · 动态任务").classes("section-kicker")
            ui.label("我登记的任务").classes("section-title")
            ui.label("在下面表单登记的任务都在这里，随时可以删。").classes("section-note")

            @ui.refreshable
            def dynamic_tasks():
                tasks = task_list()
                if not tasks:
                    ui.label("还没有登记任何动态任务。用下面的表单登记一个，"
                             "它会立刻出现在这里。").classes("empty-guidance")
                    return
                for task in tasks:
                    with ui.card().classes("w-full p-4"):
                        with ui.row().classes("w-full items-center justify-between gap-3"):
                            with ui.column().classes("gap-0 flex-1"):
                                ui.label(task.get("name", "（未命名）")).classes(
                                    "font-medium")
                                last_run = task.get("last_run_at") or "还没跑过"
                                run_count = task.get("run_count") or 0
                                ui.label(
                                    f"{_humanize_trigger(task)} · 上次运行：{last_run}"
                                    f" · 已运行 {run_count} 次"
                                ).classes("text-xs text-gray-500")

                            async def delete_click(task_id=task["id"],
                                                   name=task.get("name", "")):
                                task_delete(task_id)
                                ui.notify(f"已删除「{name}」", type="positive")
                                dynamic_tasks.refresh()

                            ui.button("删除", on_click=delete_click).props(
                                "flat dense no-caps color=negative")

            dynamic_tasks()

        # ── 固定任务（HEARTBEAT.md） ──
        with ui.column().classes("w-full gap-3"):
            ui.label("贰 · 固定任务").classes("section-kicker")
            ui.label("固定任务（HEARTBEAT.md）").classes("section-title")
            ui.label("只读——要改就编辑 HEARTBEAT.md。实际运行情况看任务健康。").classes(
                "section-note")

            static_tasks = _load_static_tasks()
            if not static_tasks:
                ui.label("读不到 HEARTBEAT.md 里的任务清单。").classes("empty-guidance")
            for task in static_tasks:
                with ui.card().classes("w-full p-3"):
                    with ui.row().classes("w-full items-center justify-between"):
                        with ui.column().classes("gap-0"):
                            ui.label(task["name"]).classes("font-medium text-sm")
                            ui.label(f"每 {_format_interval(task['interval'])} 跑一次").classes(
                                "text-xs text-gray-500")
                        ui.link("任务健康 →", "/agent-calendar").classes("text-xs")

        # ── 登记新任务 ──
        with ui.column().classes("w-full gap-3"):
            ui.label("叁 · 登记").classes("section-kicker")
            ui.label("登记新任务").classes("section-title")
            ui.label("目前只支持「通知」这一种动作：到点给你发一条消息。").classes(
                "section-note")

            with ui.card().classes("w-full p-4"):
                name_input = ui.input("任务名", placeholder="比如：早起闹钟").classes(
                    "w-full")
                with ui.row().classes("w-full gap-4"):
                    trigger_type = ui.select(
                        {"cron": "按时间表（cron）", "interval": "每隔一段时间",
                         "date": "某个时间点一次"},
                        value="cron", label="什么时候触发",
                    )
                    trigger_value = ui.input(
                        "触发配置",
                        placeholder="按时间表：0 6 * * * ｜ 间隔：600（秒）｜"
                                    "时间点：2026-05-22T06:00",
                    ).classes("flex-1")
                # prompt/script 没有执行器（后端也会拒绝），只留 notify。
                action_type = ui.select(
                    {"notify": "通知（给你发一条消息）"},
                    value="notify", label="触发后做什么",
                )
                action_value = ui.textarea(
                    "消息内容", placeholder="起床了！",
                ).classes("w-full")

                async def register_task():
                    import uuid
                    name = name_input.value.strip()
                    if not name:
                        ui.notify("任务名不能为空", type="warning")
                        return
                    tt = trigger_type.value
                    tv = trigger_value.value.strip()
                    err = _validate_trigger(tt, tv)
                    if err:
                        ui.notify(err, type="warning")
                        return
                    # Parse trigger config
                    if tt == "cron":
                        tc = {"expression": tv}
                    elif tt == "interval":
                        tc = {"seconds": int(tv)}
                    else:
                        tc = {"datetime": tv}
                    # Parse action config
                    av = action_value.value.strip()
                    if not av:
                        ui.notify("消息内容不能为空——到点总得说点什么", type="warning")
                        return
                    try:
                        ac = json.loads(av) if av.startswith("{") else {"message": av}
                    except json.JSONDecodeError:
                        ac = {"message": av}

                    task_id = f"user_{uuid.uuid4().hex[:8]}"
                    try:
                        task_register(
                            task_id=task_id, name=name,
                            trigger_type=tt, trigger_config=tc,
                            action_type=action_type.value, action_config=ac,
                            category="user", priority=5,
                        )
                    except ValueError as e:
                        # 服务端校验比页面严（如 cron 值越界）——失败必须让
                        # 人看见，不能只在服务端日志里叹气。
                        ui.notify(f"没登记上：{e}", type="warning")
                        return
                    ui.notify(f"「{name}」已登记，上面的列表里能看到", type="positive")
                    # Clear form + make the new task visible immediately
                    name_input.value = ""
                    trigger_value.value = ""
                    action_value.value = ""
                    dynamic_tasks.refresh()

                ui.button("登记任务", on_click=register_task).classes(
                    "memorial-primary mt-2").props("no-caps")
