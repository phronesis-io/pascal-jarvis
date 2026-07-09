"""Task Health board — sched_events.jsonl 驱动的真实运行视图 (REQ-44).

The old page was a fake Gantt: bar widths invented from interval×5%, data
from synthetic heartbeat_state watermarks. This board renders REALITY:
per task the last actual run (max task_finish ts), failure rate, p50/max
duration, every skip reason with counts, and the circuit-breaker state —
plus a red-flag banner for tasks that are silently dead (interval < 6h yet
ZERO events of any kind within 3×interval, the repos-sync failure class).
Liveness counts skips too: an event-driven task (intention-check, checkin,
eigenflux-friends…) legitimately goes days without a spawn — its empty_pre
skips ARE proof the scheduler runs it. Counting only task_spawn false-flagged
7 healthy tasks on 2026-07-08; a dead scheduler emits nothing at all.

Data layer: dashboard.telemetry incremental tail-reader — each ui.timer(15)
refresh reads only the bytes appended since the last one.
"""

import json
import time
from datetime import datetime
from pathlib import Path

from nicegui import ui

from ..uiutil import guarded_refresh_timer

from ..telemetry import read_sched_events, read_json

JARVIS_DIR = Path(__file__).parent.parent.parent

# 健康板只看任务执行事件；intent_* 等生命周期事件归 /intentions
_TASK_EVENTS = ("task_spawn", "task_finish", "task_skip", "task_timeout")

# 红旗判定: interval < 6h 的任务，3×interval 内零事件（spawn/skip/finish/timeout
# 都算活）→ 静默死亡
DEAD_MAX_INTERVAL_S = 6 * 3600
DEAD_FACTOR = 3


def _parse_ts(ts: str) -> float | None:
    """sched_events 'YYYY-MM-DD HH:MM:SS' local string → epoch."""
    try:
        return time.mktime(time.strptime(ts, "%Y-%m-%d %H:%M:%S"))
    except (ValueError, OverflowError, TypeError):
        return None


def _percentile(values: list[float], pct: float) -> float:
    """Nearest-rank percentile; values need not be sorted."""
    if not values:
        return 0.0
    s = sorted(values)
    k = max(0, min(len(s) - 1, int(round(pct / 100 * (len(s) - 1)))))
    return s[k]


def aggregate_task_health(events: list[dict], window_s: float = 86400,
                          now_ts: float | None = None) -> dict[str, dict]:
    """Per-task aggregation over sched_events entries.

    Returns {task: {last_finish, last_finish_ts, last_spawn_ts, spawns,
    finishes, failures, timeouts, failure_rate, p50_s, max_s,
    skip_reasons: {reason: count}}}. Window applies to the counters;
    last_finish/last_spawn are all-time (the honest "最后一次真实运行").
    """
    now_ts = now_ts if now_ts is not None else time.time()
    cutoff = now_ts - window_s
    out: dict[str, dict] = {}

    def slot(task: str) -> dict:
        if task not in out:
            out[task] = {
                "last_finish": "", "last_finish_ts": None, "last_spawn_ts": None,
                "spawns": 0, "finishes": 0, "failures": 0, "timeouts": 0,
                "failure_rate": 0.0, "p50_s": 0.0, "max_s": 0.0,
                "skip_reasons": {}, "_durations": [],
            }
        return out[task]

    for e in events:
        ev = e.get("event", "")
        if ev not in _TASK_EVENTS:
            continue
        task = str(e.get("task", "") or "")
        if not task:
            continue
        ts = _parse_ts(str(e.get("ts", "")))
        s = slot(task)
        in_window = ts is not None and ts >= cutoff

        if ev == "task_spawn":
            if ts is not None and (s["last_spawn_ts"] is None or ts > s["last_spawn_ts"]):
                s["last_spawn_ts"] = ts
            if in_window:
                s["spawns"] += 1
        elif ev == "task_finish":
            if ts is not None and (s["last_finish_ts"] is None or ts > s["last_finish_ts"]):
                s["last_finish_ts"] = ts
                s["last_finish"] = str(e.get("ts", ""))
            if in_window:
                s["finishes"] += 1
                if str(e.get("status", "ok")) != "ok":
                    s["failures"] += 1
                try:
                    s["_durations"].append(float(e.get("duration_s", 0) or 0))
                except (TypeError, ValueError):
                    pass
        elif ev == "task_timeout":
            if in_window:
                s["timeouts"] += 1
        elif ev == "task_skip":
            if in_window:
                reason = str(e.get("reason", "") or "?")
                s["skip_reasons"][reason] = s["skip_reasons"].get(reason, 0) + 1

    for s in out.values():
        attempts = s["finishes"] + s["timeouts"]
        s["failure_rate"] = (s["failures"] + s["timeouts"]) / attempts if attempts else 0.0
        s["p50_s"] = _percentile(s["_durations"], 50)
        s["max_s"] = max(s["_durations"], default=0.0)
        del s["_durations"]
    return out


def detect_silently_dead(hb_tasks: list[dict], events: list[dict],
                         now_ts: float | None = None) -> list[dict]:
    """HEARTBEAT.md tasks with interval<6h and ZERO events in 3×interval.

    Any _TASK_EVENTS entry counts as liveness — a task_skip(empty_pre) means
    the scheduler ran the task and its pre-script found nothing to do, which
    is healthy. Only a task the scheduler never even touches (zero events of
    any kind) is silently dead."""
    now_ts = now_ts if now_ts is not None else time.time()
    last_event: dict[str, float] = {}
    for e in events:
        if e.get("event") not in _TASK_EVENTS:
            continue
        ts = _parse_ts(str(e.get("ts", "")))
        task = str(e.get("task", "") or "")
        if ts is not None and task and ts > last_event.get(task, 0):
            last_event[task] = ts

    dead = []
    for t in hb_tasks:
        interval = t.get("interval", 0) or 0
        if not 0 < interval < DEAD_MAX_INTERVAL_S:
            continue
        seen = last_event.get(t["name"])
        silent_for = now_ts - seen if seen else None
        if seen is None or silent_for > DEAD_FACTOR * interval:
            dead.append({"name": t["name"], "interval": interval,
                         "silent_for_s": silent_for})
    return dead


def _load_heartbeat_tasks(jarvis_dir: Path | None = None) -> list[dict]:
    """Load task definitions from HEARTBEAT.md."""
    jd = jarvis_dir or JARVIS_DIR
    try:
        from core.heartbeat import parse_heartbeat
        hb_path = jd / "HEARTBEAT.md"
        if hb_path.exists():
            return parse_heartbeat(hb_path)
    except ImportError:
        pass
    return []


def _format_interval(seconds) -> str:
    seconds = int(seconds or 0)
    if seconds < 60:
        return f"{seconds}s"
    elif seconds < 3600:
        return f"{seconds // 60}m"
    elif seconds < 86400:
        return f"{seconds // 3600}h"
    else:
        return f"{seconds // 86400}d"


def _circuit_info(state: dict, task: str, now_ts: float) -> tuple[int, float]:
    """(consecutive_failures, disabled_for_s) from heartbeat_state circuit."""
    circuit = (state.get(task) or {}).get("circuit") or {}
    failures = int(circuit.get("consecutive_failures", 0) or 0)
    disabled_until = float(circuit.get("disabled_until", 0) or 0)
    return failures, max(disabled_until - now_ts, 0)


@ui.page("/agent-calendar")
def task_health_page():
    """Task Health board (keeps the legacy /agent-calendar route)."""
    ui.add_head_html("""
    <style>
        .summary-card { background: #f9fafb; border-radius: 12px; padding: 1rem; }
        .health-row { border: 1px solid #e5e7eb; border-radius: 8px; padding: 0.6rem 0.9rem; }
        .health-row:hover { border-color: #3b82f6; }
        .dead-banner { background: #fef2f2; border: 1px solid #fca5a5; border-radius: 8px;
                       padding: 0.75rem 1rem; }
    </style>
    """)

    with ui.column().classes("w-full max-w-5xl mx-auto p-4 gap-4"):
        with ui.row().classes("w-full items-center justify-between"):
            ui.link("← Home", "/").classes("text-gray-500 text-sm")
            ui.label("Task Health").classes("text-2xl font-bold")
        ui.label("真实运行视图——直接读 sched_events.jsonl，不是水印推算").classes(
            "text-xs text-gray-500")

        @ui.refreshable
        def board():
            now_ts = time.time()
            events = read_sched_events(JARVIS_DIR)
            hb_tasks = _load_heartbeat_tasks()
            state = read_json(JARVIS_DIR / "heartbeat_state.json", ttl=10, default={}) or {}
            health = aggregate_task_health(events, window_s=86400, now_ts=now_ts)

            # ── 红旗头条: 静默死亡 ──
            dead = detect_silently_dead(hb_tasks, events, now_ts=now_ts)
            if dead:
                with ui.element("div").classes("dead-banner w-full"):
                    ui.label("⚠ silently dead").classes("font-bold text-red-600")
                    for d in dead:
                        silent = (_format_interval(d["silent_for_s"]) + " 无任何事件"
                                  ) if d["silent_for_s"] is not None else "从未有事件"
                        ui.label(
                            f"{d['name']} — interval {_format_interval(d['interval'])}，{silent}"
                            f"（阈值 {DEAD_FACTOR}×interval）"
                        ).classes("text-sm text-red-700")

            # ── 概览 ──
            circuits_open = sum(
                1 for t in state
                if _circuit_info(state, t, now_ts)[1] > 0)
            failing = sum(1 for h in health.values() if h["failure_rate"] > 0)
            with ui.row().classes("w-full gap-4"):
                with ui.card().classes("summary-card flex-1"):
                    ui.label(str(len(health))).classes("text-2xl font-bold")
                    ui.label("tasks with events").classes("text-xs text-gray-500")
                with ui.card().classes("summary-card flex-1"):
                    color = "text-red-600" if dead else "text-green-600"
                    ui.label(str(len(dead))).classes(f"text-2xl font-bold {color}")
                    ui.label("silently dead").classes("text-xs text-gray-500")
                with ui.card().classes("summary-card flex-1"):
                    color = "text-amber-600" if failing else "text-gray-600"
                    ui.label(str(failing)).classes(f"text-2xl font-bold {color}")
                    ui.label("failing in 24h").classes("text-xs text-gray-500")
                with ui.card().classes("summary-card flex-1"):
                    color = "text-red-600" if circuits_open else "text-gray-600"
                    ui.label(str(circuits_open)).classes(f"text-2xl font-bold {color}")
                    ui.label("circuit open").classes("text-xs text-gray-500")

            # ── 每任务一行 ──
            ui.label("Per task (24h window)").classes("text-lg font-semibold mt-2")
            ordered = sorted(
                health.items(),
                key=lambda kv: kv[1]["last_finish_ts"] or 0,
                reverse=True,
            )
            dead_names = {d["name"] for d in dead}
            for task, h in ordered:
                with ui.element("div").classes("health-row w-full"):
                    with ui.row().classes("w-full items-center justify-between"):
                        with ui.column().classes("gap-0 flex-1"):
                            with ui.row().classes("items-center gap-2"):
                                ui.label(task).classes("font-medium text-sm")
                                if task in dead_names:
                                    ui.badge("⚠ silently dead", color="red").classes("text-xs")
                                failures, disabled_for = _circuit_info(state, task, now_ts)
                                if disabled_for > 0:
                                    ui.badge(
                                        f"熔断中 · 还剩 {_format_interval(disabled_for)}",
                                        color="red").classes("text-xs")
                                elif failures > 0:
                                    ui.badge(
                                        f"连败 {failures}", color="amber").classes("text-xs")
                            last = h["last_finish"] or "never"
                            ago = ""
                            if h["last_finish_ts"]:
                                ago = f"（{_format_interval(now_ts - h['last_finish_ts'])} 前）"
                            ui.label(f"last run: {last} {ago}").classes("text-xs text-gray-500")
                            if h["skip_reasons"]:
                                reasons = " · ".join(
                                    f"{n}× {r}" for r, n in sorted(
                                        h["skip_reasons"].items(),
                                        key=lambda kv: -kv[1]))
                                ui.label(f"skips: {reasons}").classes("text-xs text-amber-600")
                        with ui.column().classes("items-end gap-0"):
                            rate = h["failure_rate"]
                            rate_cls = ("text-red-600" if rate > 0.3
                                        else "text-amber-600" if rate > 0 else "text-green-600")
                            ui.label(f"fail {rate * 100:.0f}%").classes(f"text-sm font-medium {rate_cls}")
                            ui.label(
                                f"runs {h['finishes']} · p50 {h['p50_s']:.1f}s · max {h['max_s']:.1f}s"
                            ).classes("text-xs text-gray-400")

            if not health:
                ui.label("sched_events.jsonl 里没有任务事件。").classes(
                    "text-gray-400 text-sm italic")

        board()
        guarded_refresh_timer(15, board.refresh)

        # Navigation
        ui.separator()
        with ui.row().classes("gap-4"):
            ui.link("Home", "/").classes("text-blue-600")
            ui.link("Tasks", "/tasks").classes("text-blue-600")
            ui.link("Thinking", "/thinking").classes("text-purple-600")
