"""Jarvis home — decisions first, machinery second.

The previous page put thirty routine ``empty_pre`` skips ahead of the things
Pascal could actually act on.  Home is now the ten-second view: pending
memorials, a small outcome strip, then only meaningful activity. Operational
telemetry remains available, but lives in a collapsed system drawer.
"""

from __future__ import annotations

import time
from pathlib import Path

from nicegui import ui

from core.jsonl import read_jsonl

from ..db import engagement_stats
from ..telemetry import read_jsonl_tail, read_sched_events
from ..uiutil import (add_dashboard_head, dashboard_header,
                      guarded_refresh_timer, memorial_attention_rank,
                      memorial_display_title)

JARVIS_DIR = Path(__file__).parent.parent.parent

_ROUTINE_SKIP_REASONS = {
    "empty_pre", "not_due", "throttled", "interval_not_elapsed",
    "silent_output", "no_output", "duplicate", "queued_quiet_hours",
    "queued_daytime_batch",
}


def _selfmon_headline(jarvis_dir: Path) -> dict:
    try:
        from core.selfmon import compute_selfmon
        m = compute_selfmon(jarvis_dir, window_hours=24)
        overdue = m["closure_overdue"]
        return {
            "noise_cards": m["noise_card_count"]["total_sent"],
            "low_engagement": len(m["noise_card_count"]["low_engagement_sources"]),
            "refires": m["same_intent_refires"]["offender_count"],
            "closure_overdue": (overdue["overdue_count"]
                                if overdue.get("db_available") else "n/a"),
            "crashes": m["model_crash_or_skip"]["total"],
            "silent": m["silent_failures"]["total"],
            "ok": True,
        }
    except Exception:  # noqa: BLE001 — the home surface must always render
        return {"ok": False}


def _memorial_states(jarvis_dir: str | Path) -> list[dict]:
    """Fold the local memorial ledger without relying on module globals."""
    from core.memorial import _fold
    return list(_fold(read_jsonl(Path(jarvis_dir) / "memorials.jsonl")).values())


def pending_memorials(jarvis_dir: str | Path, limit: int | None = None) -> list[dict]:
    states = [s for s in _memorial_states(jarvis_dir)
              if s.get("status") == "pending"
              and s.get("delivery_status") not in {"failed", "expired"}]
    states.sort(key=memorial_attention_rank, reverse=True)
    return states[:limit] if limit is not None else states


def _parse_epoch(ts: str) -> float | None:
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return time.mktime(time.strptime(ts, fmt))
        except (ValueError, TypeError):
            continue
    return None


def build_activity_feed(jarvis_dir: str | Path, limit: int = 12) -> list[dict]:
    """Only changes a human may care about; routine polling stays backstage."""
    jd = Path(jarvis_dir)
    feed: list[dict] = []

    for e in read_sched_events(jd)[-500:]:
        ev = str(e.get("event", ""))
        ts = str(e.get("ts", ""))
        epoch = _parse_epoch(ts)
        if epoch is None:
            continue
        task = str(e.get("task", "") or "")
        if ev == "task_timeout":
            feed.append({"ts": ts, "epoch": epoch, "source": "异常",
                         "message": f"{task} 超时，系统会继续重试"})
        elif ev == "task_finish":
            status = str(e.get("status", "ok"))
            if status not in {"ok", "idle", "silent"}:
                feed.append({"ts": ts, "epoch": epoch, "source": "异常",
                             "message": f"{task} 结束状态：{status}"})
        elif ev == "task_skip":
            reason = str(e.get("reason", ""))
            if reason and reason not in _ROUTINE_SKIP_REASONS:
                feed.append({"ts": ts, "epoch": epoch, "source": "留意",
                             "message": f"{task} 未运行：{reason}"})
        elif ev.startswith("intent_"):
            name = str(e.get("name", "") or task)
            labels = {
                "intent_create": "新意图", "intent_trigger": "意图触发",
                "intent_execute": "意图已执行", "intent_close": "意图闭环",
                "intent_expire": "意图过期",
            }
            feed.append({"ts": ts, "epoch": epoch, "source": "意图",
                         "message": f"{labels.get(ev, ev)} · {name}"})

    for o in read_jsonl_tail(jd / "heartbeat_outbox.jsonl")[-50:]:
        ts = str(o.get("ts", ""))
        epoch = _parse_epoch(ts)
        if epoch is None:
            continue
        text = str(o.get("text", "")).replace("\n", " ").strip()
        # The pending section already shows memorial deliveries. Repeating
        # every card (and every chat opener) here merely rebuilds the noise wall.
        if (text.startswith("CARD:") or "**📜" in text or "📜 聊聊" in text
                or "📜 已带上" in text or "（奏折 mem_" in text):
            continue
        if len(text) > 100:
            text = text[:100].rstrip() + "…"
        if text:
            feed.append({"ts": ts, "epoch": epoch, "source": "送达",
                         "message": text})

    feed.sort(key=lambda x: x["epoch"], reverse=True)
    return feed[:limit]


def derive_status(jarvis_dir: str | Path,
                  now_ts: float | None = None) -> tuple[str, str]:
    now_ts = now_ts if now_ts is not None else time.time()
    newest = 0.0
    for e in read_sched_events(jarvis_dir)[-50:]:
        epoch = _parse_epoch(str(e.get("ts", "")))
        if epoch is not None and epoch > newest:
            newest = epoch
    if not newest:
        return "离线", "red"
    age = now_ts - newest
    if age < 120:
        return "在岗", "green"
    if age < 600:
        return "候命", "amber"
    return "失联", "red"


def _format_relative_time(epoch: float, now_ts: float | None = None) -> str:
    now_ts = now_ts if now_ts is not None else time.time()
    delta = max(now_ts - epoch, 0)
    if delta < 60:
        return "刚刚"
    if delta < 3600:
        return f"{int(delta / 60)} 分钟前"
    if delta < 86400:
        return f"{int(delta / 3600)} 小时前"
    return f"{int(delta / 86400)} 天前"


def _compact(text: str, limit: int = 190) -> str:
    one = " ".join(str(text or "").split())
    return one if len(one) <= limit else one[:limit].rstrip() + "…"


def _metric(label: str, value, alert: bool = False) -> None:
    with ui.element("div").classes("metric-cell"):
        cls = "metric-value" + (" is-alert" if alert else "")
        ui.label(str(value)).classes(cls)
        ui.label(label).classes("metric-label")


def _memorial_preview(state: dict) -> None:
    with ui.card().classes("memorial-card"):
        with ui.row().classes("w-full items-center justify-between gap-3"):
            ui.label(state.get("source", "奏折")).classes("memorial-source")
            ui.label(state.get("ts", "")).classes("memorial-time")
        ui.label(memorial_display_title(state)).classes("memorial-title")
        ui.markdown(_compact(state.get("body", ""))).classes("memorial-body")
        with ui.row().classes("memorial-actions"):
            ui.link("去批示 →", "/memorials").classes(
                "jarvis-nav-link is-active")


@ui.page("/")
def home_page():
    add_dashboard_head()
    with ui.column().classes("jarvis-page"):
        status, _ = derive_status(JARVIS_DIR)
        dashboard_header("/", "今日御前", f"Jarvis {status} · 这里只放需要你知道或决定的事")

        @ui.refreshable
        def live_content():
            pending = pending_memorials(JARVIS_DIR)
            states = _memorial_states(JARVIS_DIR)
            stats = engagement_stats(7)
            sm = _selfmon_headline(JARVIS_DIR)
            marked = sum(s.get("decided_opt") == "watch" for s in states)
            issues = ((sm.get("refires", 0) or 0)
                      + (sm.get("closure_overdue", 0)
                         if isinstance(sm.get("closure_overdue"), int) else 0)
                      + (sm.get("low_engagement", 0) or 0)) if sm.get("ok") else "—"

            with ui.element("div").classes("metric-strip"):
                _metric("待批奏折", len(pending), alert=bool(pending))
                _metric("已标重点", marked)
                _metric("7 日互动率", f"{stats['rate']}%")
                _metric("需关注信号", issues, alert=issues not in (0, "—"))

            with ui.column().classes("w-full gap-3"):
                ui.label("DECISIONS").classes("section-kicker")
                with ui.row().classes("w-full items-end justify-between gap-4"):
                    with ui.column().classes("gap-1"):
                        ui.label("御前待批").classes("section-title")
                        ui.label("一张卡只说一件事；批示，或带进对话。").classes("section-note")
                    ui.link(f"查看全部 {len(pending)} →", "/memorials").classes(
                        "jarvis-nav-link")
                if pending:
                    with ui.element("div").classes("memorial-grid"):
                        for state in pending[:4]:
                            _memorial_preview(state)
                else:
                    ui.label("没有待批事项。Jarvis 会继续在后台看着，有事再来。\n").classes(
                        "empty-guidance")

            with ui.column().classes("w-full gap-3"):
                ui.label("CHANGES").classes("section-kicker")
                ui.label("真正发生的变化").classes("section-title")
                feed = build_activity_feed(JARVIS_DIR)
                if feed:
                    now_ts = time.time()
                    with ui.element("div").classes("activity-list"):
                        for entry in feed:
                            with ui.element("div").classes("timeline-entry"):
                                ui.label(entry["source"]).classes("activity-source")
                                ui.label(entry["message"]).classes("activity-message")
                                ui.label(_format_relative_time(entry["epoch"], now_ts)).classes(
                                    "activity-time")
                else:
                    ui.label("后台按计划巡检，最近没有需要你处理的变化。").classes(
                        "empty-guidance")

            with ui.expansion("系统底稿 · 24 小时", icon="monitor_heart").classes(
                    "system-drawer w-full"):
                if not sm.get("ok"):
                    ui.label("暂时读不到自监控数据。").classes("section-note")
                else:
                    with ui.element("div").classes("metric-strip"):
                        _metric("噪声卡片", sm["noise_cards"])
                        _metric("重复触发", sm["refires"], alert=bool(sm["refires"]))
                        _metric("闭环逾期", sm["closure_overdue"],
                                alert=sm["closure_overdue"] not in (0, "n/a"))
                        _metric("静默失败", sm["silent"], alert=bool(sm["silent"]))

        live_content()
        guarded_refresh_timer(15, live_content.refresh)
