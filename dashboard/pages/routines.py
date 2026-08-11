"""例程页 — 他自己建的自动化，和它们每次到底干了什么。

两件事一个页面：
  上半 = 例程本身（触发、授权级别、证据源、下次什么时候跑），可暂停/恢复；
  下半 = 审计流，每次运行一行：读了哪些证据、产出了什么、发没发卡、
        自动执行了什么动作、失败了为什么。

审计流是这个页面存在的理由。一个会自己动手的东西如果不能被回看，它就不该
被允许动手 —— `observe` 级例程的产出只在这里出现，别处都看不到。
"""

from pathlib import Path

from nicegui import ui

from ..uiutil import guarded_refresh_timer, jarvis_page

JARVIS_DIR = Path(__file__).parent.parent.parent

_STATUS_STYLE = {
    "delivered": ("已发卡", "text-emerald-400"),
    "observed": ("只记录（observe）", "text-sky-400"),
    "no_output": ("无产出", "text-slate-400"),
    "failed": ("失败", "text-rose-400"),
    "running": ("进行中", "text-amber-400"),
}

_AUTONOMY_STYLE = {
    "observe": ("只看不说", "bg-sky-900 text-sky-200"),
    "propose": ("提方案等你点头", "bg-emerald-900 text-emerald-200"),
    "act": ("可自己动手", "bg-amber-900 text-amber-200"),
}


def _routines_module():
    import sys
    path = str(JARVIS_DIR)
    if path not in sys.path:
        sys.path.insert(0, path)
    from core import routines
    return routines


def _trigger_text(r: dict) -> str:
    if r["trigger_type"] == "cron":
        return f"cron {r['trigger_expr']}"
    try:
        minutes = int(r["trigger_expr"]) // 60
    except (TypeError, ValueError):
        return str(r["trigger_expr"])
    if minutes % 1440 == 0:
        return f"每 {minutes // 1440} 天"
    if minutes % 60 == 0:
        return f"每 {minutes // 60} 小时"
    return f"每 {minutes} 分钟"


@ui.refreshable
def _routine_list() -> None:
    rt = _routines_module()
    rows = rt.list_routines(status=None)
    if not rows:
        with ui.card().classes("jarvis-card w-full"):
            ui.label("还没有例程").classes("text-lg")
            ui.label("在飞书里直接说「以后每周五下午把这周的提交汇总给我」，"
                     "或者用 python3 -m core.routines create 建一条。"
                     ).classes("text-sm opacity-70")
        return

    for r in rows:
        with ui.card().classes("jarvis-card w-full"):
            with ui.row().classes("w-full items-center justify-between gap-2"):
                with ui.row().classes("items-center gap-2"):
                    ui.label(r["name"]).classes("text-lg font-medium")
                    label, style = _AUTONOMY_STYLE.get(
                        r["autonomy"], (r["autonomy"], "bg-slate-800"))
                    ui.label(label).classes(f"text-xs px-2 py-0.5 rounded {style}")
                    if r["status"] != "active":
                        ui.label(r["status"]).classes(
                            "text-xs px-2 py-0.5 rounded bg-slate-800 text-slate-300")
                with ui.row().classes("items-center gap-1"):
                    if r["status"] == "active":
                        ui.button(icon="pause",
                                  on_click=lambda _, rid=r["id"]: _set(rid, "paused")
                                  ).props("flat dense round").tooltip("暂停")
                    else:
                        ui.button(icon="play_arrow",
                                  on_click=lambda _, rid=r["id"]: _set(rid, "active")
                                  ).props("flat dense round").tooltip("恢复")
                    ui.button(icon="archive",
                              on_click=lambda _, rid=r["id"]: _set(rid, "archived")
                              ).props("flat dense round").tooltip("归档")
            ui.label(r["instruction"]).classes("text-sm opacity-90")
            detail = (f"{_trigger_text(r)}　·　下次 {r['next_fire_at'] or '—'}"
                      f"　·　跑过 {r['run_count']} 次")
            ui.label(detail).classes("text-xs opacity-60")
            ui.label("证据：" + ("、".join(r["evidence"]) or "无（只能凭记忆写，建议补上）")
                     ).classes("text-xs opacity-60")
            if r["last_error"]:
                ui.label("上次错误：" + r["last_error"]).classes("text-xs text-rose-400")


def _set(rid: str, status: str) -> None:
    rt = _routines_module()
    try:
        rt.set_status(rid, status)
    except Exception as exc:
        ui.notify(f"改不了：{exc}", type="negative")
        return
    _routine_list.refresh()
    _run_log.refresh()


@ui.refreshable
def _run_log() -> None:
    rt = _routines_module()
    runs = rt.list_runs(limit=40)
    names = {r["id"]: r["name"] for r in rt.list_routines(status=None)}
    if not runs:
        ui.label("还没有运行记录。").classes("text-sm opacity-60")
        return
    for run in runs:
        label, colour = _STATUS_STYLE.get(run["status"],
                                          (run["status"], "text-slate-400"))
        with ui.card().classes("jarvis-card w-full"):
            with ui.row().classes("w-full items-center justify-between gap-2"):
                ui.label(names.get(run["routine_id"], run["routine_id"])
                         ).classes("text-sm font-medium")
                ui.label(f"{run['started_at']}　{label}").classes(f"text-xs {colour}")
            if run["output"]:
                ui.label(run["output"][:400]).classes("text-sm opacity-90")
            meta = []
            if run["evidence_sources"]:
                meta.append("读了：" + "、".join(run["evidence_sources"]))
            if run["memorial_id"]:
                meta.append("卡 " + run["memorial_id"])
            if meta:
                ui.label("　·　".join(meta)).classes("text-xs opacity-60")
            for act in run["actions"]:
                mark = "✓" if act.get("ok") else "✗"
                tone = "text-emerald-400" if act.get("ok") else "text-rose-400"
                ui.label(f"{mark} {act.get('type')}：{act.get('detail', '')}"
                         ).classes(f"text-xs {tone}")
            if run["error"]:
                ui.label("! " + run["error"]).classes("text-xs text-rose-400")


@ui.page("/routines")
def routines_page() -> None:
    for _ in jarvis_page("routines", "例程",
                         "你自己建的自动化 —— 它们什么时候跑、看什么、能动到哪一步"):
        _routine_list()
        ui.label("运行记录").classes("text-base font-medium mt-4")
        ui.label("observe 级例程的产出只在这里出现，不会发给任何人。"
                 ).classes("text-xs opacity-60 -mt-1")
        _run_log()
        guarded_refresh_timer(30, lambda: (_routine_list.refresh(),
                                           _run_log.refresh()))
