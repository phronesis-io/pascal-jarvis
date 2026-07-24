"""Shared UI helpers for dashboard pages.

Single source of truth for the 御前 visual system and for the
machine-vocabulary → 人话 maps. Pages must not print raw scheduler /
intent enum keys; they go through the label helpers here.
"""

from __future__ import annotations

import inspect
import re
from contextlib import contextmanager, nullcontext

from nicegui import ui

_NAV = [
    ("今日", "/"),
    ("事项", "/items"),
    ("任务", "/tasks"),
    ("收藏", "/bookmarks"),
    ("运行", "/ops"),
    ("更多", "/settings"),
]


def surface_from_headers(headers) -> tuple[str, str]:
    """Map an authenticated gateway request to its product surface."""
    device_id = str((headers or {}).get("X-Jarvis-Device", "") or "").strip()
    return ("mobile", device_id) if device_id else ("desktop", "local")


def client_surface() -> tuple[str, str]:
    """Return the current NiceGUI surface and its trusted actor identity."""
    try:
        request = ui.context.client.request
        return surface_from_headers(request.headers)
    except (AttributeError, RuntimeError):
        return "desktop", "local"


_GENERIC_MEMORIAL_TITLES = {
    "", "EigenFlux", "eigenflux", "heartbeat", "pgc-improvement", "一件事",
    "Intent", "intent", "EigenFlux 消息", "EigenFlux 分析", "repos-sync",
    "跨 Session 动态", "变动",
}

# 来源键 → 用户可读标签。奏折卡、首页预览、互动表共用一份。
SOURCE_LABELS = {
    "eigenflux-feed-triage": "EigenFlux",
    "eigenflux": "EigenFlux",
    "eigenflux-friends": "EigenFlux",
    "eigenflux-publish": "EigenFlux",
    "mail": "邮件",
    "mail-triage": "邮件",
    "selfmon": "自诊断",
    "intention-check": "意图",
    "intentions": "意图",
    "intent": "意图",
    "cross-session-sync": "跨 Session",
    "daily-reflect": "复盘",
    "weekly-review": "周回顾",
    "calendar-sync": "日程",
    "checkin": "关怀",
    "metrics-digest": "指标日报",
    "phronesis-monitor": "Phronesis",
    "repos-sync": "仓库",
    "heartbeat": "心跳",
    "pgc-improvement": "PGC",
    "release": "发版",
}


def source_label(source: str, fallback: str = "奏折") -> str:
    raw = str(source or "").strip()
    if not raw:
        return fallback
    return SOURCE_LABELS.get(raw, raw.replace("-", " "))


# 调度器"跳过"里属于正常节律的原因——不该被任何面板当成失败标红。
ROUTINE_SKIP_REASONS = {
    "empty_pre", "not_due", "throttled", "interval_not_elapsed",
    "silent_output", "no_output", "duplicate", "queued_quiet_hours",
    "queued_daytime_batch", "batch_deferred",
}

_SKIP_REASON_LABELS = {
    "empty_pre": "没有新内容",
    "not_due": "还没到时间",
    "throttled": "触发太密，已限速",
    "interval_not_elapsed": "间隔未到",
    "silent_output": "没有产出",
    "no_output": "没有产出",
    "duplicate": "重复，已合并",
    "queued_quiet_hours": "夜间免打扰，已排队",
    "queued_daytime_batch": "合并进白天批次",
    "batch_deferred": "合并进批处理",
    "shared_call_backoff": "上游繁忙，稍后再试",
    "no_envelope_acked": "上一条还没送达确认",
    "pre_nonzero": "前置检查未通过",
    "pre_error": "前置检查出错",
    "overlap_lock": "上一轮还没跑完",
    "circuit_open": "连续出错，熔断暂停中",
}

_FINISH_STATUS_LABELS = {
    "ok": "正常",
    "idle": "无事可做",
    "silent": "静默",
    "failed": "失败",
    "timeout": "超时",
    "parse_failed": "输出没解析出来",
    "crashed": "崩溃",
    "skipped": "跳过",
    "killed": "重启时停掉（正常）",
}

# task_finish 里不算异常的收场——重启被停(killed)是部署节奏，不是故障。
ROUTINE_FINISH_STATUSES = {"ok", "idle", "silent", "killed"}

# 判定"真失败"的权威口径，与 core/selfmon.CRASH_STATUSES 一致。
CRASH_FINISH_STATUSES = {"failed", "parse_failed", "timeout"}

_INTENT_EVENT_LABELS = {
    "intent_create": "新意图",
    "intent_fired": "意图触发",
    "intent_trigger": "意图触发",
    "intent_executed": "意图已执行",
    "intent_execute": "意图已执行",
    "intent_retry": "意图重试",
    "intent_close": "意图闭环",
    "intent_closure": "意图闭环",
    "intent_closure_reask": "追问是否办结",
    "intent_closure_touch": "闭环跟进",
    "intent_expire": "意图过期",
    "intent_expired": "意图过期",
    "intent_occurrence_skipped": "本次跳过",
}


def skip_reason_label(reason: str) -> str:
    reason = str(reason or "").strip()
    return _SKIP_REASON_LABELS.get(reason, f"未按计划运行（{reason}）" if reason else "未按计划运行")


def finish_status_label(status: str) -> str:
    status = str(status or "").strip()
    return _FINISH_STATUS_LABELS.get(status, status)


def intent_event_label(event: str) -> str:
    return _INTENT_EVENT_LABELS.get(str(event or ""), str(event or ""))


def memorial_display_title(state: dict) -> str:
    """Turn legacy transport titles into a useful one-line subject."""
    title = str(state.get("title", "") or "").strip()
    title = re.sub(r"^(?:TITLE|标题)\s*[:：]\s*", "", title,
                   flags=re.I).strip()
    if title not in _GENERIC_MEMORIAL_TITLES:
        return title
    body = str(state.get("body", "") or "").strip()
    first = next((line.strip() for line in body.splitlines() if line.strip()), "一件事")
    first = re.sub(r"^(?:TITLE|标题)\s*[:：]\s*", "", first,
                   flags=re.I).strip()
    first = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", first)
    first = re.sub(r"[*_`#]+", "", first)
    first = re.sub(r"^[\s📡📬🩺🎯🧠🫀🌿📅💡⏰📺📊🪞🧭📋🌙⏳🗞️🚨✅⚠️❗️🔧🎙️]+", "", first)
    first = first.split("。", 1)[0].split("！", 1)[0].strip(" ·|-：:")
    return first if len(first) <= 30 else first[:30].rstrip() + "…"


def memorial_display_body(state: dict) -> str:
    """Hide legacy TITLE framing duplicated into the persisted body."""
    body = str(state.get("body", "") or "").strip()
    raw_title = str(state.get("title", "") or "").strip()
    if raw_title and re.match(r"^(?:TITLE|标题)\s*[:：]", raw_title, re.I):
        if body.startswith(raw_title):
            return body[len(raw_title):].lstrip(" \n:：")
    body = re.sub(r"^(?:TITLE|标题)\s*[:：]\s*[^\n]+\n?", "",
                  body, count=1, flags=re.I).lstrip()
    return body


def memorial_option_label(label: str) -> str:
    """Correct legacy promise-heavy copy without rewriting the audit ledger."""
    return {"重要，持续盯": "标为重点"}.get(str(label), str(label))


def memorial_review_surface(state: dict) -> str:
    from core.memorial import review_surface
    return review_surface(state)


def memorial_surface_label(state: dict) -> str:
    """Say where attention belongs, without making notices look like approvals."""
    if state.get("status") == "decided":
        return "已批"
    from core.memorial import ATTENTION_ALERT, REVIEW_LARK
    if not memorial_is_pending(state):
        if str(state.get("attention", "")) == ATTENTION_ALERT:
            return "飞书提醒 · 无需批"
        return "知会 · 无需批"
    if memorial_review_surface(state) == REVIEW_LARK:
        return "飞书即时批"
    return "手机集中批"


def memorial_visible_options(state: dict) -> list[dict]:
    """Do not let model-invented notice replies masquerade as approvals."""
    options = list(state.get("options") or [])
    if memorial_is_pending(state):
        return options
    if memorial_is_notice(state):
        return [
            option for option in options
            if str(option.get("key", "")) in {"read", "watch"}
        ]
    return []


def memorial_is_pending(state: dict) -> bool:
    """Only explicit choices belong in the user's pending-decision queue."""
    from core.memorial import requires_decision
    return (state.get("status") == "pending"
            and state.get("delivery_status") not in {"failed", "expired"}
            and requires_decision(state))


def memorial_is_notice(state: dict) -> bool:
    """Unread FYI-class history: visible on the web, never counted as 待批."""
    from core.memorial import requires_decision
    return (state.get("status") == "pending"
            and not requires_decision(state))


def memorial_attention_rank(state: dict) -> tuple[int, float]:
    """Direct asks and personal reflection outrank ambient network signals."""
    source = str(state.get("source", ""))
    if source in {"selfmon", "mail", "mail-triage", "intent", "intentions",
                  "intention-check", "checkin"}:
        tier = 3
    elif source in {"daily-reflect", "weekly-review", "cross-session-sync",
                    "memory", "calendar-sync"}:
        tier = 2
    elif source.startswith("eigenflux"):
        tier = 0
    else:
        tier = 1
    try:
        epoch = float(state.get("epoch", 0) or 0)
    except (TypeError, ValueError):
        # One corrupt ledger row must not blank the whole decision surface.
        epoch = 0.0
    return tier, epoch


def add_dashboard_head() -> None:
    """Load the shared visual system for user-facing dashboard pages.

    ui.colors rebinds Quasar's brand palette to the 御前 tokens — without it
    every q-btn falls back to Quasar blue (its own !important wins the
    same-specificity fight against style.css).
    """
    ui.colors(primary="#152833", secondary="#2b7a68", accent="#9a7135",
              positive="#2b7a68", negative="#b8473a", warning="#9a7135")
    ui.add_head_html(
        '<link rel="stylesheet" href="/static/style.css?v=20260724-continuity2">'
        # use-credentials: the browser's manifest fetch defaults to
        # credentials-omit, so behind the authenticated mobile gateway it 401s
        # and the PWA never gets its manifest.
        '<link rel="manifest" href="/static/manifest.webmanifest" '
        'crossorigin="use-credentials">'
        '<link rel="icon" href="/static/app-icon.svg" type="image/svg+xml">'
        '<link rel="apple-touch-icon" href="/static/app-icon-192.png">'
        '<meta name="apple-mobile-web-app-capable" content="yes">'
        '<meta name="apple-mobile-web-app-status-bar-style" content="default">'
        '<meta name="apple-mobile-web-app-title" content="Jarvis">'
        '<meta name="theme-color" content="#f5f7f8" '
        'media="(prefers-color-scheme: light)">'
        '<meta name="theme-color" content="#10181d" '
        'media="(prefers-color-scheme: dark)">'
        '<script>if ("serviceWorker" in navigator) {'
        'window.addEventListener("load", () => '
        'navigator.serviceWorker.register("/sw.js", {scope: "/"}));}</script>'
    )


def dashboard_header(active: str, title: str, subtitle: str = "") -> None:
    """Persistent decision-first navigation, shared by primary surfaces."""
    with ui.element("header").classes("jarvis-masthead"):
        with ui.row().classes("w-full items-start justify-between gap-4"):
            with ui.column().classes("gap-0"):
                ui.label("JARVIS · 御前").classes("jarvis-eyebrow")
                ui.label(title).classes("jarvis-display")
                if subtitle:
                    ui.label(subtitle).classes("jarvis-subtitle")
        with ui.element("nav").classes("jarvis-nav"):
            for label, href in _NAV:
                classes = "jarvis-nav-link"
                if href == active or (href == "/settings" and active in {
                        "/settings", "/usage", "/engagement", "/thinking",
                        "/agent-calendar"}):
                    classes += " is-active"
                ui.link(label, href).classes(classes)
        with ui.element("nav").classes("mobile-dock"):
            for label, href, icon in (
                    ("今日", "/", "home"),
                    ("事项", "/items", "inbox"),
                    ("更多", "/settings", "more_horiz")):
                selected = (href == active or (href == "/settings" and active not in {
                    "/", "/items"}))
                with ui.link(target=href).classes(
                        "mobile-dock-link" + (" is-active" if selected else "")):
                    ui.icon(icon, size="21px")
                    ui.label(label)


@contextmanager
def jarvis_page(active: str, title: str, subtitle: str = ""):
    """Standard page scaffold: head + page column + masthead, then content."""
    add_dashboard_head()
    with ui.column().classes("jarvis-page") as column:
        dashboard_header(active, title, subtitle)
        yield column


class _ClientBoundTimer(ui.timer):
    """ui.timer that dies quietly with its client instead of crash-looping.

    The daemon's liveness probe hits '/' every few minutes; each probe is a
    throwaway NiceGUI client that never opens a websocket. When the client is
    pruned, nicegui 3.12's Timer._run_in_loop evaluates `self._get_context()`
    (→ the `parent_slot` Element property) BEFORE its first _should_stop()
    check, and that property raises `RuntimeError: The parent slot of the
    element has been deleted` — one full traceback per pruned client, which
    was 100% of dashboard stderr (316 in one day). Catching inside the
    callback cannot help: the raise happens in the timer machinery before the
    callback ever runs. Intercept at the actual raise point instead.
    """

    def _get_context(self):
        try:
            return super()._get_context()
        except RuntimeError:
            self.cancel()
            return nullcontext()


def guarded_refresh_timer(interval: float, refresh) -> ui.timer:
    """Periodic refresh timer for dashboard pages, safe on pruned clients.

    `refresh` may be sync (refreshable.refresh) or an async callable. The
    in-callback guard below covers slot-deletion raised *during* a refresh
    (element updates racing a disconnect); _ClientBoundTimer covers the
    machinery-level raise before the callback runs.
    """
    timer: ui.timer | None = None

    async def _tick():
        try:
            result = refresh()
            if inspect.isawaitable(result):
                await result
        except RuntimeError as e:
            if "slot" in str(e) and "deleted" in str(e):
                if timer is not None:
                    timer.cancel()
            else:
                raise
    timer = _ClientBoundTimer(interval, _tick)
    return timer
