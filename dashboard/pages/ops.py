"""运行板 — logs, scheduler events, and invisible queues (REQ-55).

This page is deliberately read-only. Admin (:3456) owns destructive controls;
dashboard (:3457) gives a human-friendly live view of failure signatures and
queue depth when Jarvis feels "quiet" or "stuck".
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from nicegui import ui

from ..uiutil import (ROUTINE_FINISH_STATUSES, ROUTINE_SKIP_REASONS,
                      finish_status_label, guarded_refresh_timer,
                      intent_event_label, jarvis_page, skip_reason_label)

from ..telemetry import read_json, read_sched_events

JARVIS_DIR = Path(__file__).parent.parent.parent

LOG_FAILURE_SIGNATURES = (
    "timed out (60s)",
    "JSON parse failed",
    "parse failed",
    "exited with code 1",
    "stderr:",
    "Claude failed",
    "Claude CLI not found",
)

# 事件键 → 人话（intent_* 走 uiutil.intent_event_label）
_TASK_EVENT_LABELS = {
    "task_spawn": "任务启动",
    "task_finish": "任务跑完",
    "task_skip": "任务跳过",
    "task_timeout": "任务超时",
    "sleep_gap": "机器睡了一觉",
    "batch_flush": "批处理下发",
    "circuit_tripped": "熔断触发",
    "shared_call_backoff": "上游繁忙退避",
}


def _event_label(event: str) -> str:
    event = str(event or "")
    if event in _TASK_EVENT_LABELS:
        return _TASK_EVENT_LABELS[event]
    return intent_event_label(event)


def _event_detail(e: dict) -> str:
    """一行人话说明这条事件的结果/原因。"""
    event = str(e.get("event", ""))
    if event == "task_finish":
        return finish_status_label(str(e.get("status", "") or "ok"))
    if event == "task_skip":
        return skip_reason_label(str(e.get("reason", "")))
    if event == "task_timeout":
        return "超时被掐掉"
    if event == "batch_flush":
        return f"下发 {e.get('count', '')} 条".strip()
    detail = e.get("status") or e.get("reason") or ""
    return str(detail)


def tail_log(path: Path, lines: int = 120, grep: str = "") -> dict:
    """Bounded tail-reader for plain-text logs with failure flagging."""
    lines = max(1, min(int(lines), 1000))
    if not path.exists():
        return {"path": str(path), "lines": [], "flagged_count": 0, "missing": True}
    try:
        size = path.stat().st_size
        chunk = min(size, max(128 * 1024, lines * 500))
        with open(path, "rb") as f:
            f.seek(size - chunk)
            raw = f.read().decode("utf-8", errors="ignore")
    except OSError as exc:
        return {"path": str(path), "lines": [], "flagged_count": 0, "error": str(exc)}
    all_lines = raw.splitlines()
    if size > chunk and all_lines:
        all_lines = all_lines[1:]
    if grep:
        all_lines = [line for line in all_lines if grep in line]
    out = []
    for line in all_lines[-lines:]:
        # '"expected": true' = emitter-marked by-design event (elected probe,
        # warm squeeze) — never painted red (REQ-96).
        flagged = (any(sig in line for sig in LOG_FAILURE_SIGNATURES)
                   and '"expected": true' not in line)
        out.append({"text": line, "flagged": flagged})
    return {
        "path": str(path),
        "lines": out,
        "flagged_count": sum(1 for row in out if row["flagged"]),
        "missing": False,
    }


def _read_jsonl(path: Path, limit: int = 500) -> list[dict]:
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return []
    out = []
    for line in lines[-limit:]:
        try:
            row = json.loads(line)
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def _age_minutes(ts: str, now: datetime | None = None) -> int | None:
    now = now or datetime.now()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            return round((now - datetime.strptime(ts, fmt)).total_seconds() / 60)
        except (TypeError, ValueError):
            continue
    return None


def queue_overview(jarvis_dir: Path | None = None) -> dict:
    jd = jarvis_dir or JARVIS_DIR
    night = []
    for row in _read_jsonl(jd / "night_queue.jsonl"):
        night.append({
            "ts": row.get("ts", ""),
            "age_minutes": _age_minutes(str(row.get("ts", ""))),
            "source": row.get("source", ""),
            "text": str(row.get("text", ""))[:180],
        })

    breach = _read_jsonl(jd / "data" / ".intent_breach_queue.jsonl")

    jobs = {"counts": {}, "running": []}
    registry = read_json(jd / "jobs" / "registry.json", ttl=0, default={}) or {}
    if isinstance(registry, dict):
        for job_id, info in registry.items():
            if not isinstance(info, dict):
                continue
            status = info.get("status", "unknown")
            jobs["counts"][status] = jobs["counts"].get(status, 0) + 1
            if status == "running":
                jobs["running"].append({
                    "id": job_id,
                    "description": str(info.get("description", ""))[:140],
                    "started_at": info.get("started_at", ""),
                    "age_minutes": _age_minutes(str(info.get("started_at", ""))),
                    "pid": info.get("pid"),
                })

    delivery = read_json(jd / ".delivery_state.json", ttl=0, default={}) or {}
    if not isinstance(delivery, dict):
        delivery = {}

    return {
        "night_queue": night,
        "breach_queue": breach,
        "jobs": jobs,
        "delivery_state": delivery,
    }


def ops_snapshot(jarvis_dir: Path | None = None, event_limit: int = 80) -> dict:
    jd = jarvis_dir or JARVIS_DIR
    jarvis_log = tail_log(jd / "jarvis.log", lines=160)
    daemon_log = tail_log(jd / "daemon.log", lines=80)
    events = read_sched_events(jd)[-event_limit:]
    failed_events = []
    for e in events:
        event = e.get("event")
        if event == "task_timeout":
            failed_events.append(e)
        elif (event == "task_finish"
                and str(e.get("status") or "ok") not in ROUTINE_FINISH_STATUSES):
            failed_events.append(e)
        elif event == "task_skip":
            # 正常节律的跳过（没新内容/还没到点/夜间排队…）不是故障 —
            # 把它们计成红色数字曾让这页天天喊狼来了。
            if str(e.get("reason", "")) not in ROUTINE_SKIP_REASONS:
                failed_events.append(e)
    queues = queue_overview(jd)
    return {
        "logs": {"jarvis": jarvis_log, "daemon": daemon_log},
        "events": events,
        "failed_events": failed_events,
        "queues": queues,
        "flagged_count": jarvis_log["flagged_count"] + daemon_log["flagged_count"],
    }


def _fmt_age(minutes: int | None) -> str:
    if minutes is None:
        return "?"
    if abs(minutes) < 60:
        return f"{minutes} 分钟"
    return f"{minutes / 60:.1f} 小时"


@ui.page("/ops")
def ops_page():
    with jarvis_page("/ops", "运行", "机器后台的原样记录；红色才需要动手。"):

        @ui.refreshable
        def content():
            snap = ops_snapshot(JARVIS_DIR)
            queues = snap["queues"]
            delivery = queues["delivery_state"]
            metrics = (
                ("异常日志", snap["flagged_count"], True),
                ("异常事件", len(snap["failed_events"]), True),
                ("夜间队列", len(queues["night_queue"]), False),
                ("意图违约", len(queues["breach_queue"]), True),
                ("送达失败", delivery.get("consec_fails", 0), True),
            )
            with ui.row().classes("w-full gap-3"):
                for label, value, alertable in metrics:
                    alert = alertable and value not in (0, "0", None)
                    with ui.element("div").classes("metric-cell flex-1"):
                        ui.label(str(value)).classes(
                            "metric-value" + (" is-alert" if alert else ""))
                        ui.label(label).classes("metric-label")

            ui.label("壹 · 调度").classes("section-kicker mt-2")
            ui.label("最近的调度事件").classes("section-title")
            event_rows = list(reversed(snap["events"][-40:]))
            if event_rows:
                columns = [
                    {"name": "ts", "label": "时间", "field": "ts", "align": "left"},
                    {"name": "event", "label": "发生了什么", "field": "event",
                     "align": "left"},
                    {"name": "task", "label": "任务", "field": "task", "align": "left"},
                    {"name": "detail", "label": "结果 / 原因", "field": "detail",
                     "align": "left"},
                    # 原始键留给排错用，弱化显示，不当主标签。
                    {"name": "raw", "label": "原始记录", "field": "raw",
                     "align": "left", "classes": "text-grey-6",
                     "headerClasses": "text-grey-6"},
                ]
                rows = []
                for i, e in enumerate(event_rows):
                    raw_detail = e.get("status") or e.get("reason") or ""
                    # 意图事件的 task 字段是内部 id（int_…）——人话列显示
                    # 意图名，原始 id 归"原始记录"列。
                    task_label = str(e.get("task", "") or "")
                    if task_label.startswith("int_"):
                        raw_detail = f"{task_label} {raw_detail}".strip()
                        task_label = str(e.get("name", "") or "意图任务")
                    rows.append({
                        "_id": f"{e.get('ts', '')}-{i}",
                        "ts": e.get("ts", ""),
                        "event": _event_label(e.get("event", "")),
                        "task": task_label,
                        "detail": _event_detail(e),
                        "raw": " ".join(str(x) for x in
                                        (e.get("event", ""), raw_detail) if x),
                    })
                with ui.element("div").classes("table-scroll"):
                    ui.table(columns=columns, rows=rows, row_key="_id").classes(
                        "jarvis-table")
            else:
                ui.label("还没有调度记录。").classes("empty-guidance")

            ui.label("贰 · 队列").classes("section-kicker mt-2")
            ui.label("排着队的事").classes("section-title")
            with ui.row().classes("w-full gap-3"):
                with ui.card().classes("flex-1 p-3"):
                    ui.label("夜间队列").classes("font-semibold text-sm")
                    ui.label("夜里攒着、白天再送的消息").classes("text-xs text-gray-400")
                    for item in queues["night_queue"][:5]:
                        ui.label(
                            f"{item.get('ts', '')} · 等了 {_fmt_age(item.get('age_minutes'))} · "
                            f"{item.get('text', '')}"
                        ).classes("text-xs text-gray-600")
                    if not queues["night_queue"]:
                        ui.label("空").classes("text-xs text-gray-400")
                with ui.card().classes("flex-1 p-3"):
                    ui.label("正在跑的后台工作").classes("font-semibold text-sm")
                    for job in queues["jobs"]["running"][:5]:
                        ui.label(
                            f"{job['id']} · 跑了 {_fmt_age(job.get('age_minutes'))} · "
                            f"{job.get('description', '')}"
                        ).classes("text-xs text-gray-600")
                    if not queues["jobs"]["running"]:
                        ui.label("没有").classes("text-xs text-gray-400")
                with ui.card().classes("flex-1 p-3"):
                    ui.label("意图违约").classes("font-semibold text-sm")
                    ui.label("说好要办、到点没办成的").classes("text-xs text-gray-400")
                    for item in queues["breach_queue"][:5]:
                        ui.label(str(item)[:160]).classes("text-xs text-gray-600")
                    if not queues["breach_queue"]:
                        ui.label("空").classes("text-xs text-gray-400")

            # 读不到日志必须明说——静默显示 0 就是在把故障涂成正常。
            unreadable = [name for name, log in snap["logs"].items()
                          if log.get("missing") or log.get("error")]
            if unreadable:
                ui.label("读不到日志文件：" + "、".join(unreadable)
                         + "。上面的「异常日志」计数不完整。").classes(
                    "empty-guidance")

            # 工程师抽屉：原始日志行只在需要排错时展开看。
            flagged = []
            for name, log in snap["logs"].items():
                for row in log["lines"]:
                    if row["flagged"]:
                        flagged.append({"file": name, "text": row["text"]})
            drawer_title = ("原始日志底稿" +
                            (f"（{len(flagged)} 行异常）" if flagged else ""))
            with ui.expansion(drawer_title, icon="receipt_long").classes(
                    "system-drawer w-full"):
                if flagged:
                    for row in flagged[-20:]:
                        with ui.card().classes("w-full p-2"):
                            ui.badge(row["file"], color="red").classes("text-xs")
                            ui.label(row["text"]).classes(
                                "text-xs font-mono text-red-700")
                else:
                    ui.label("最近的日志里没有故障签名。").classes(
                        "text-gray-400 text-sm italic")

        content()
        guarded_refresh_timer(15, content.refresh)
