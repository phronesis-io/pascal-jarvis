"""Jarvis home — decisions first, machinery second.

The previous page put thirty routine ``empty_pre`` skips ahead of the things
the user could actually act on.  Home is now the ten-second view: pending
memorials, a small outcome strip, then only meaningful activity. Operational
telemetry remains available, but lives in a collapsed system drawer.
"""

from __future__ import annotations

import time
from pathlib import Path

from nicegui import ui

from ..db import engagement_stats
from ..telemetry import (memorial_states, parse_ts_epoch, read_jsonl_tail,
                         read_sched_events)
from ..uiutil import (ROUTINE_FINISH_STATUSES, ROUTINE_SKIP_REASONS,
                      add_dashboard_head, dashboard_header,
                      finish_status_label, guarded_refresh_timer,
                      intent_event_label, memorial_attention_rank,
                      memorial_display_title, memorial_is_pending,
                      memorial_option_label, skip_reason_label, source_label)

JARVIS_DIR = Path(__file__).parent.parent.parent

# compute_selfmon re-reads MBs of logs; once a minute is plenty for a drawer.
_SELFMON_TTL = 60.0
_selfmon_cache = {"at": 0.0, "data": {"ok": False}}


def _selfmon_headline(jarvis_dir: Path) -> dict:
    now = time.monotonic()
    if now - _selfmon_cache["at"] < _SELFMON_TTL:
        return _selfmon_cache["data"]
    try:
        from core.selfmon import compute_selfmon
        m = compute_selfmon(jarvis_dir, window_hours=24)
        overdue = m["closure_overdue"]
        refires = m["same_intent_refires"]
        low_sources = m["noise_card_count"]["low_engagement_sources"]
        data = {
            "noise_cards": m["noise_card_count"]["total_sent"],
            "low_engagement": len(low_sources),
            "refires": refires["offender_count"],
            "closure_overdue": (overdue["overdue_count"]
                                if overdue.get("db_available") else "—"),
            "crashes": m["model_crash_or_skip"]["total"],
            "silent": m["silent_failures"]["total"],
            "ok": True,
            # 点名——"需关注信号 6"这种哑数字没法行动，名单才可以。
            "refire_names": [
                f"{o.get('name', iid)} ×{o.get('max_in_window', '?')}"
                f"/{o.get('window_min', '?')}分钟"
                for iid, o in list(dict(refires.get("offenders", {})).items())[:3]],
            "low_source_names": [source_label(s) for s in low_sources[:5]],
            "overdue_names": [
                str(o.get("name", o) if isinstance(o, dict) else o)
                for o in list(overdue.get("overdue", []))[:3]],
        }
    except Exception:  # noqa: BLE001 — the home surface must always render
        data = {"ok": False}
    _selfmon_cache.update(at=now, data=data)
    return data


def pending_memorials(jarvis_dir: str | Path, limit: int | None = None,
                      states: list[dict] | None = None) -> list[dict]:
    if states is None:
        states = memorial_states(jarvis_dir)
    pending = [s for s in states if memorial_is_pending(s)]
    pending.sort(key=memorial_attention_rank, reverse=True)
    return pending[:limit] if limit is not None else pending


def build_activity_feed(jarvis_dir: str | Path, limit: int = 12) -> list[dict]:
    """Only changes a human may care about; routine polling stays backstage."""
    jd = Path(jarvis_dir)
    feed: list[dict] = []

    events = read_sched_events(jd)[-500:]
    # 触发+已执行成对出现时只留"已执行"——同一件事不占两行。只有当同名
    # 意图在触发"之后"确实执行了才折叠：卡住没执行的触发必须留在面上。
    executed_at: dict[str, list[float]] = {}
    for e in events:
        if str(e.get("event", "")) in {"intent_executed", "intent_execute"}:
            ep = parse_ts_epoch(str(e.get("ts", "")))
            if ep is not None:
                name = str(e.get("name", "") or e.get("task", ""))
                executed_at.setdefault(name, []).append(ep)

    for e in events:
        ev = str(e.get("event", ""))
        ts = str(e.get("ts", ""))
        epoch = parse_ts_epoch(ts)
        if epoch is None:
            continue
        task = str(e.get("task", "") or "")
        if ev == "task_timeout":
            feed.append({"ts": ts, "epoch": epoch, "source": "异常",
                         "message": f"{task} 超时，系统会继续重试"})
        elif ev == "task_finish":
            status = str(e.get("status", "ok"))
            if status not in ROUTINE_FINISH_STATUSES:
                feed.append({"ts": ts, "epoch": epoch, "source": "异常",
                             "message": f"{task}：{finish_status_label(status)}"})
        elif ev == "task_skip":
            reason = str(e.get("reason", ""))
            if reason and reason not in ROUTINE_SKIP_REASONS:
                feed.append({"ts": ts, "epoch": epoch, "source": "留意",
                             "message": f"{task}：{skip_reason_label(reason)}"})
        elif ev.startswith("intent_"):
            name = str(e.get("name", "") or task)
            if (ev in {"intent_fired", "intent_trigger"}
                    and any(x >= epoch for x in executed_at.get(name, ()))):
                continue
            feed.append({"ts": ts, "epoch": epoch, "source": "意图",
                         "message": f"{intent_event_label(ev)} · {name}"})

    for o in read_jsonl_tail(jd / "heartbeat_outbox.jsonl")[-50:]:
        ts = str(o.get("ts", ""))
        epoch = parse_ts_epoch(ts)
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
        epoch = parse_ts_epoch(str(e.get("ts", "")))
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


def _metric(label: str, value, alert: bool = False,
            href: str | None = None, on_click=None) -> None:
    """数字都是门：能点的指标才带得动决策，死数字只制造疑问。"""
    cell = ui.element("a" if href else "div").classes("metric-cell")
    if href:
        cell.props(f'href="{href}"')
    if on_click is not None:
        cell.classes("is-clickable")
        cell.on("click", on_click)
    with cell:
        cls = "metric-value" + (" is-alert" if alert else "")
        ui.label(str(value)).classes(cls)
        ui.label(label).classes("metric-label")


def _memorial_preview(state: dict, refresh) -> None:
    """首页预览卡直接可批——十秒视图不该为一次点击跳一页。"""
    from core import memorial

    def decide(mid: str, key: str):
        payload = memorial.decide(mid, key)
        toast = payload.get("toast", {})
        ui.notify(toast.get("content", "已记录"),
                  type="positive" if toast.get("type") == "success" else "info")
        refresh()

    def chat(mid: str):
        payload = memorial.chat(mid)
        toast = payload.get("toast", {})
        ui.notify(toast.get("content", "已切到对话"),
                  type="positive" if toast.get("type") == "success" else "info")
        refresh()

    with ui.card().classes("memorial-card"):
        with ui.row().classes("w-full items-center justify-between gap-3"):
            ui.label(source_label(state.get("source", ""))).classes(
                "memorial-source")
            ui.label(state.get("ts", "")).classes("memorial-time")
        ui.label(memorial_display_title(state)).classes("memorial-title")
        ui.markdown(_compact(state.get("body", ""))).classes("memorial-body")
        with ui.row().classes("memorial-actions"):
            for index, option in enumerate(state.get("options", [])[:3]):
                ui.button(
                    memorial_option_label(option.get("label", "选择")),
                    on_click=lambda mid=state["id"], key=option.get("key", ""):
                        decide(mid, key),
                ).props("unelevated no-caps" if index == 0
                        else "outline no-caps").classes(
                    "memorial-primary" if index == 0 else "memorial-secondary")
            ui.button("聊聊这个",
                      on_click=lambda mid=state["id"]: chat(mid)).props(
                "flat no-caps").classes("memorial-chat")
            ui.link("全文 →", "/memorials").classes("jarvis-nav-link")


@ui.page("/")
def home_page():
    add_dashboard_head()
    with ui.column().classes("jarvis-page"):
        dashboard_header("/", "今日御前", "这里只放需要你知道或决定的事")

        @ui.refreshable
        def live_content():
            # Liveness must live INSIDE the refreshable: computed once at
            # page build it would keep saying 在岗 forever on an open tab —
            # lying exactly in the failure case it exists to catch.
            status, tone = derive_status(JARVIS_DIR)
            with ui.element("span").classes("status-pill"):
                ui.element("span").classes(f"status-dot is-{tone}")
                ui.label(f"Jarvis {status}")

            states = memorial_states(JARVIS_DIR)
            pending = pending_memorials(JARVIS_DIR, states=states)
            stats = engagement_stats(7)
            sm = _selfmon_headline(JARVIS_DIR)
            marked = sum(s.get("decided_opt") == "watch" for s in states)
            issues = ((sm.get("refires", 0) or 0)
                      + (sm.get("closure_overdue", 0)
                         if isinstance(sm.get("closure_overdue"), int) else 0)
                      + (sm.get("low_engagement", 0) or 0)) if sm.get("ok") else "—"

            drawer_ref: dict = {}

            def open_drawer():
                exp = drawer_ref.get("el")
                if exp is not None:
                    exp.value = True
                    ui.run_javascript(
                        f'document.getElementById("c{exp.id}")'
                        '?.scrollIntoView({behavior: "smooth"})')

            with ui.element("div").classes("metric-strip"):
                _metric("待批奏折", len(pending), alert=bool(pending),
                        href="/memorials")
                _metric("已标重点", marked, href="/memorials")
                _metric("7 日互动率", f"{stats['rate']}%", href="/engagement")
                _metric("需关注信号", issues, alert=issues not in (0, "—"),
                        on_click=open_drawer)

            with ui.column().classes("w-full gap-3"):
                ui.label("壹 · 决策").classes("section-kicker")
                with ui.row().classes("w-full items-end justify-between gap-4"):
                    with ui.column().classes("gap-1"):
                        ui.label("御前待批").classes("section-title")
                        ui.label("一张卡只说一件事；批示，或带进对话。").classes("section-note")
                    ui.link(f"查看全部 {len(pending)} →", "/memorials").classes(
                        "jarvis-nav-link")
                if pending:
                    with ui.element("div").classes("memorial-grid"):
                        for state in pending[:4]:
                            _memorial_preview(state, live_content.refresh)
                else:
                    ui.label("没有待批事项。Jarvis 会继续在后台看着，有事再来。").classes(
                        "empty-guidance")

            with ui.column().classes("w-full gap-3"):
                ui.label("贰 · 变化").classes("section-kicker")
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
                    "system-drawer w-full") as drawer:
                drawer_ref["el"] = drawer
                if not sm.get("ok"):
                    ui.label("暂时读不到自监控数据。").classes("section-note")
                else:
                    with ui.element("div").classes("metric-strip"):
                        _metric("噪声卡片", sm["noise_cards"])
                        _metric("重复触发", sm["refires"], alert=bool(sm["refires"]))
                        _metric("闭环逾期", sm["closure_overdue"],
                                alert=sm["closure_overdue"] not in (0, "—"))
                        _metric("静默失败", sm["silent"], alert=bool(sm["silent"]))
                    # 名单在此：光有数字只会引出"这 6 是什么"的追问。
                    callouts = []
                    if sm.get("refire_names"):
                        callouts.append("重复触发：" + "、".join(sm["refire_names"]))
                    if sm.get("overdue_names"):
                        callouts.append("闭环逾期：" + "、".join(sm["overdue_names"]))
                    if sm.get("low_source_names"):
                        callouts.append("这些来源发了卡但你很少理："
                                        + "、".join(sm["low_source_names"]))
                    for line in callouts:
                        ui.label(line).classes("section-note")

        live_content()
        guarded_refresh_timer(15, live_content.refresh)
