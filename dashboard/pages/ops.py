"""Ops board — logs, scheduler events, and invisible queues (REQ-55).

This page is deliberately read-only. Admin (:3456) owns destructive controls;
dashboard (:3457) gives a human-friendly live view of failure signatures and
queue depth when Jarvis feels "quiet" or "stuck".
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

from nicegui import ui

from ..uiutil import guarded_refresh_timer

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
        elif event == "task_finish" and e.get("status") not in ("", "ok", None):
            failed_events.append(e)
        elif event == "task_skip" and e.get("reason") != "empty_pre":
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
        return f"{minutes}m"
    return f"{minutes / 60:.1f}h"


@ui.page("/ops")
def ops_page():
    with ui.column().classes("w-full max-w-6xl mx-auto p-4 gap-4"):
        with ui.row().classes("w-full items-center justify-between"):
            ui.link("← Home", "/").classes("text-gray-500 text-sm")
            ui.label("Ops").classes("text-2xl font-bold")

        ui.label("只读运维视图：日志故障签名、sched_events 尾巴、队列和投递状态。").classes(
            "text-sm text-gray-600")

        @ui.refreshable
        def content():
            snap = ops_snapshot(JARVIS_DIR)
            queues = snap["queues"]
            delivery = queues["delivery_state"]
            with ui.row().classes("w-full gap-3"):
                metrics = (
                    ("flagged logs", snap["flagged_count"]),
                    ("failed events", len(snap["failed_events"])),
                    ("night queue", len(queues["night_queue"])),
                    ("breaches", len(queues["breach_queue"])),
                    ("delivery fails", delivery.get("consec_fails", 0)),
                )
                for label, value in metrics:
                    alert = label != "night queue" and value not in (0, "0", None)
                    with ui.card().classes("flex-1 p-3"):
                        ui.label(str(value)).classes(
                            "text-xl font-bold" + (" text-red-600" if alert else ""))
                        ui.label(label).classes("text-xs text-gray-500")

            ui.label("Recent scheduler events").classes("text-lg font-semibold mt-2")
            event_rows = list(reversed(snap["events"][-40:]))
            if event_rows:
                columns = [
                    {"name": "ts", "label": "Time", "field": "ts", "align": "left"},
                    {"name": "event", "label": "Event", "field": "event", "align": "left"},
                    {"name": "task", "label": "Task", "field": "task", "align": "left"},
                    {"name": "detail", "label": "Detail", "field": "detail", "align": "left"},
                ]
                rows = []
                for i, e in enumerate(event_rows):
                    rows.append({
                        "_id": f"{e.get('ts', '')}-{i}",
                        "ts": e.get("ts", ""),
                        "event": e.get("event", ""),
                        "task": e.get("task", ""),
                        "detail": e.get("status") or e.get("reason") or e.get("count") or "",
                    })
                ui.table(columns=columns, rows=rows, row_key="_id").classes("w-full")
            else:
                ui.label("No sched_events yet.").classes("text-gray-400 text-sm italic")

            ui.label("Queues").classes("text-lg font-semibold mt-2")
            with ui.row().classes("w-full gap-3"):
                with ui.card().classes("flex-1 p-3"):
                    ui.label("Night queue").classes("font-semibold text-sm")
                    for item in queues["night_queue"][:5]:
                        ui.label(
                            f"{item.get('ts', '')} · age {_fmt_age(item.get('age_minutes'))} · "
                            f"{item.get('text', '')}"
                        ).classes("text-xs text-gray-600")
                    if not queues["night_queue"]:
                        ui.label("empty").classes("text-xs text-gray-400")
                with ui.card().classes("flex-1 p-3"):
                    ui.label("Running jobs").classes("font-semibold text-sm")
                    for job in queues["jobs"]["running"][:5]:
                        ui.label(
                            f"{job['id']} · pid {job.get('pid')} · age {_fmt_age(job.get('age_minutes'))} · "
                            f"{job.get('description', '')}"
                        ).classes("text-xs text-gray-600")
                    if not queues["jobs"]["running"]:
                        ui.label("none").classes("text-xs text-gray-400")
                with ui.card().classes("flex-1 p-3"):
                    ui.label("Intent breaches").classes("font-semibold text-sm")
                    for item in queues["breach_queue"][:5]:
                        ui.label(str(item)[:160]).classes("text-xs text-gray-600")
                    if not queues["breach_queue"]:
                        ui.label("empty").classes("text-xs text-gray-400")

            ui.label("Flagged log lines").classes("text-lg font-semibold mt-2")
            flagged = []
            for name, log in snap["logs"].items():
                for row in log["lines"]:
                    if row["flagged"]:
                        flagged.append({"file": name, "text": row["text"]})
            if flagged:
                for row in flagged[-20:]:
                    with ui.card().classes("w-full p-2"):
                        ui.badge(row["file"], color="red").classes("text-xs")
                        ui.label(row["text"]).classes("text-xs font-mono text-red-700")
            else:
                ui.label("No flagged failure signatures in the current log tails.").classes(
                    "text-gray-400 text-sm italic")

        content()
        guarded_refresh_timer(15, content.refresh)
