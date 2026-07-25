"""意图页 — 闭环漏斗 + 将来要做的事的时间线。

REQ-45: the funnel header makes state-transition leaks visible (created→
fired→executed→closed per 7d window, 静默丢弃 highlighted), the 过期尸检
list gives every auto-expired commitment a one-click re-arm, and the create
form captures category/closure_question so dashboard-created intents stop
defaulting to closure-free 'none'.
"""

import json
from datetime import datetime, timedelta
from pathlib import Path

from core.timeutil import now_local
# _coerce aligns tz-awareness with now_local() — the live DB holds BOTH naive
# ('2026-09-25T09:00:00') and aware ('2026-06-13T12:30:00+08:00') datetimes,
# and naive-aware subtraction raises TypeError (the old 500 on every load).
from core.intentions import CLOSURE_POLICY, _coerce

from nicegui import ui

from ..uiutil import guarded_refresh_timer, jarvis_page, source_label


JARVIS_DIR = Path(__file__).parent.parent.parent
_CATEGORY_LABELS = {
    "hard": "必须闭环",
    "external": "外部跟进",
    "context": "留作上下文",
    "healing": "恢复节律",
    "autonomous": "自主观察",
    "none": "无需追问",
}

# Import lazily to avoid circular issues at module load
def _get_intentions_module():
    import sys
    path = str(JARVIS_DIR)
    if path not in sys.path:  # 每 30s 刷新都会进来 — 不能无限增长 sys.path
        sys.path.insert(0, path)
    from core import intentions
    return intentions


def _trigger_config(intent: dict) -> dict:
    try:
        value = intent.get("trigger_config") or {}
        decoded = json.loads(value) if isinstance(value, str) else value
        return dict(decoded) if isinstance(decoded, dict) else {}
    except (json.JSONDecodeError, TypeError, ValueError):
        return {}


def _aligned(value: datetime, reference: datetime) -> datetime:
    if reference.tzinfo is not None:
        if value.tzinfo is None:
            return value.replace(tzinfo=reference.tzinfo)
        return value.astimezone(reference.tzinfo)
    if reference.tzinfo is None and value.tzinfo is not None:
        return value.astimezone().replace(tzinfo=None)
    return value


def intent_next_fire(
    intent: dict,
    *,
    now: datetime | None = None,
) -> datetime | None:
    """Return the concrete next moment represented by an Intent row."""
    now = now or now_local()
    tc = _trigger_config(intent)
    trigger_type = str(intent.get("trigger_type") or "")

    if trigger_type == "date":
        try:
            return _aligned(
                datetime.fromisoformat(str(tc.get("datetime") or "")), now)
        except (TypeError, ValueError):
            return None

    if trigger_type == "cron":
        raw_next = str(intent.get("next_fire_at") or "")
        if raw_next:
            try:
                candidate = _aligned(datetime.fromisoformat(raw_next), now)
                # The lifecycle catches up a slightly overdue occurrence.
                # Keep that concrete watermark visible as due instead of
                # making the calendar jump ahead to the following beat.
                return candidate
            except (TypeError, ValueError):
                pass
        from dashboard.scheduler import cron_next
        try:
            return cron_next(str(tc.get("expression") or ""), after=now)
        except (TypeError, ValueError):
            return None

    if trigger_type == "interval":
        try:
            seconds = int(tc.get("seconds") or 0)
            raw_next = str(intent.get("next_fire_at") or "")
            if raw_next:
                return _aligned(datetime.fromisoformat(raw_next), now)
            anchor = _aligned(datetime.fromisoformat(
                str(intent.get("created_at") or "")), now)
        except (TypeError, ValueError):
            return None
        return anchor + timedelta(seconds=seconds) if seconds > 0 else None

    return None


def _relative_moment(moment: datetime, now: datetime) -> str:
    delta_seconds = (moment - now).total_seconds()
    stamp = moment.strftime("%m/%d %H:%M")
    if delta_seconds < 0:
        return f"已到点 · {stamp}"
    if delta_seconds < 3600:
        return f"{max(1, int(delta_seconds / 60))}分钟后 · {stamp}"
    if delta_seconds < 86400:
        return f"{int(delta_seconds / 3600)}小时后 · {stamp}"
    return stamp


def _parse_trigger_when(intent: dict) -> str:
    """Human-readable cadence with a concrete next fire time."""
    tc = _trigger_config(intent)
    tt = str(intent.get("trigger_type") or "")
    now = now_local()
    next_fire = intent_next_fire(intent, now=now)
    if tt == "date":
        dt_str = tc.get("datetime", "")
        if next_fire:
            relative = _relative_moment(next_fire, now)
            return relative.replace("已到点 ·", "已过期 ·")
        return str(dt_str)
    elif tt == "cron":
        expression = str(tc.get("expression") or "")
        when = _relative_moment(next_fire, now) if next_fire else "尚未排出下次时间"
        return f"周期 {expression} · 下次 {when}"
    elif tt == "interval":
        try:
            secs = int(tc.get("seconds") or 0)
        except (TypeError, ValueError):
            return "固定间隔配置无效"
        if secs < 60:
            cadence = f"每 {secs}秒"
        elif secs < 3600:
            cadence = f"每 {secs // 60}分钟"
        else:
            cadence = f"每 {secs // 3600}小时"
        when = _relative_moment(next_fire, now) if next_fire else "尚未排出下次时间"
        return f"{cadence} · 下次 {when}"
    return "未知触发方式"


# ---------------------------------------------------------------------------
# 漏斗数据层 (REQ-45) — pure data functions, unit-tested directly
# ---------------------------------------------------------------------------

def compute_funnel(days: int = 7) -> dict:
    """7d 窗口的状态转移计数 — created→fired→executed / expired / closed.

    时间列 (created_at/triggered_at/executed_at/closed_at) 都是 create_intent
    写入的本地 naive 字符串，零填充 ISO 格式 → 字符串比较即时间比较。
    `leaked` = 静默丢弃：expired 且 last_error 是自动过期类
    （'expired after N attempts…' / 'auto-expired…'）。
    """
    mod = _get_intentions_module()
    mod._init()
    db = mod._get_db()
    cutoff = (now_local() - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")

    def _count(sql: str, params: tuple = ()) -> int:
        return db.execute(sql, params).fetchone()[0]

    return {
        "created": _count(
            "SELECT COUNT(*) FROM intentions WHERE created_at >= ?", (cutoff,)),
        "fired": _count(
            "SELECT COUNT(*) FROM intentions "
            "WHERE triggered_at IS NOT NULL AND triggered_at >= ?", (cutoff,)),
        "executed": _count(
            "SELECT COUNT(*) FROM intentions "
            "WHERE executed_at IS NOT NULL AND executed_at >= ?", (cutoff,)),
        # expired 行没有专属时间戳 — 以最后已知活动时间锚定窗口
        "expired": _count(
            "SELECT COUNT(*) FROM intentions WHERE status = 'expired' "
            "AND COALESCE(triggered_at, created_at) >= ?", (cutoff,)),
        # 'na' 是行政注销（TTL 清扫/历史批量回填，core/intentions.py:1034 会把
        # 从没追问过的 none 直接翻成 na 并盖 closed_at）——算进"已追问/已闭环"
        # 就是在替系统认领没做过的跟进。
        "closure_asked": _count(
            "SELECT COUNT(*) FROM intentions "
            "WHERE closure_status IN ('awaiting', 'done', 'recorded') "
            "AND created_at >= ?", (cutoff,)),
        "closed": _count(
            "SELECT COUNT(*) FROM intentions "
            "WHERE closure_status IN ('done', 'recorded') "
            "AND COALESCE(closed_at, created_at) >= ?", (cutoff,)),
        "written_off": _count(
            "SELECT COUNT(*) FROM intentions WHERE closure_status = 'na' "
            "AND COALESCE(closed_at, created_at) >= ?", (cutoff,)),
        "leaked": _count(
            "SELECT COUNT(*) FROM intentions WHERE status = 'expired' "
            "AND COALESCE(triggered_at, created_at) >= ? "
            "AND (last_error LIKE '%expired after%attempts%' "
            "     OR last_error LIKE 'auto-expired%')", (cutoff,)),
    }


def expired_autopsy(limit: int = 20) -> list[dict]:
    """过期尸检 — expired rows with name / was-due time / last_error."""
    mod = _get_intentions_module()
    mod._init()
    db = mod._get_db()
    rows = db.execute(
        "SELECT * FROM intentions WHERE status = 'expired' "
        "ORDER BY COALESCE(triggered_at, created_at) DESC LIMIT ?",
        (limit,),
    ).fetchall()
    out = []
    for r in rows:
        it = dict(r)
        cfg = _trigger_config(it)
        it["was_due"] = cfg.get("datetime") or cfg.get("expression") or "?"
        out.append(it)
    return out


def rearm_intent(intent_id: str) -> bool | str:
    """Restore one still-expired schedule through the shared lifecycle rule."""
    mod = _get_intentions_module()
    result = mod.rearm_expired_intent(intent_id, actor="dashboard")
    return str(result["label"]) if result else False


def agenda_trigger_config(
    trigger_type: str,
    raw_value: str,
    *,
    category: str,
    closure_question: str,
) -> tuple[dict | None, str]:
    """Validate the dashboard's human-facing scheduling contract."""
    raw_value = str(raw_value or "").strip()
    if not raw_value:
        return None, "请填写时间或周期"
    if trigger_type in {"cron", "interval"} and (
            category != "none" or str(closure_question or "").strip()):
        return None, "周期计划暂不支持逐次结果追问，请改为指定时间"
    if category not in CLOSURE_POLICY:
        return None, "未知的结果跟进方式"
    if trigger_type == "date":
        question = str(closure_question or "").strip()
        has_followup = bool(CLOSURE_POLICY[category]["followup"])
        if has_followup and not question:
            return None, "选择结果跟进后，请填写结果追问"
        if question and not has_followup:
            return None, "当前跟进方式不会追问结果，请选择一种跟进方式"
        return {"datetime": raw_value}, ""
    if trigger_type == "cron":
        return {"expression": raw_value}, ""
    if trigger_type == "interval":
        try:
            seconds = int(raw_value)
        except ValueError:
            return None, "固定间隔请填写秒数"
        if seconds <= 0:
            return None, "固定间隔必须大于 0 秒"
        return {"seconds": seconds}, ""
    return None, "未知触发方式"


def close_intent_from_agenda(intent_id: str) -> bool:
    """Close one Intent while preserving the dashboard interaction channel."""
    return bool(_get_intentions_module().record_closure(
        intent_id,
        outcome="done",
        result="closed from Jarvis agenda",
        via="dashboard",
    ))


def close_intent_feedback(intent_id: str) -> tuple[str, str]:
    """Return truthful UI feedback for an attempted closure write."""
    if close_intent_from_agenda(intent_id):
        return "已记为办结", "positive"
    return "这项已经办结，无需重复记录", "info"


def agenda_commitments(mod=None, limit: int = 300) -> list[dict]:
    """Return visible user commitments, including work already in flight."""
    mod = mod or _get_intentions_module()
    rows = list(mod.list_intents(status="pending", limit=limit))
    rows.extend(mod.list_intents(status="triggered", limit=limit))
    return [
        intent for intent in rows
        if not str(intent.get("parent_intent_id") or "").strip()
    ]


def awaiting_age_days(intent: dict, now: datetime | None = None) -> float:
    """Age in days of an awaiting closure, anchored like lifecycle_sweep."""
    now = now or now_local()
    anchor_raw = intent.get("executed_at") or intent.get("triggered_at") or intent.get("created_at")
    try:
        anchor = _coerce(datetime.fromisoformat(anchor_raw)) if anchor_raw else None
    except (ValueError, TypeError):
        anchor = None
    if not anchor:
        return 0.0
    return max((now - anchor).total_seconds() / 86400, 0.0)


@ui.page("/intentions")
def intentions_page():
    """User-facing calendar for Jarvis commitments and closure."""
    mod = _get_intentions_module()

    with jarvis_page(
        "/intentions",
        "Jarvis 日历",
        "接下来 Jarvis 会在什么时候做什么。",
    ):
        @ui.refreshable
        def content():
            now = now_local()
            pending = agenda_commitments(mod)
            next_by_id = {
                str(intent.get("id") or ""): intent_next_fire(
                    intent, now=now)
                for intent in pending
            }
            pending.sort(
                key=lambda intent: (
                    next_by_id[str(intent.get("id") or "")] is None,
                    next_by_id[str(intent.get("id") or "")]
                    or datetime.max.replace(
                        tzinfo=now.tzinfo),
                    int(intent.get("priority") or 5),
                )
            )

            ui.label("壹 · 接下来").classes("section-kicker")
            with ui.row().classes("w-full items-end justify-between gap-3"):
                ui.label("已经答应的时间").classes("section-title")
                ui.label(f"{len(pending)} 项").classes("section-note")

            if not pending:
                ui.label("现在没有排上日程的事。").classes("empty-guidance")
            else:
                with ui.element("div").classes("agenda-list"):
                    for intent in pending:
                        trigger_type = str(intent.get("trigger_type") or "")
                        with ui.element("article").classes("agenda-row"):
                            with ui.element("div").classes("agenda-date"):
                                next_fire = next_by_id[
                                    str(intent.get("id") or "")]
                                if next_fire:
                                    ui.label(next_fire.strftime("%m/%d")).classes(
                                        "agenda-day")
                                    ui.label(next_fire.strftime("%H:%M")).classes(
                                        "agenda-time")
                                else:
                                    ui.label("待定").classes("agenda-day")
                            with ui.column().classes("agenda-copy"):
                                with ui.row().classes(
                                        "w-full items-center gap-2"):
                                    ui.label(intent["name"]).classes(
                                        "agenda-title")
                                    ui.badge(
                                        "执行中"
                                        if intent.get("status") == "triggered"
                                        else "周期" if trigger_type in {
                                            "cron", "interval"} else "一次"
                                    ).props("outline").classes("agenda-kind")
                                ui.label(_parse_trigger_when(intent)).classes(
                                    "agenda-when")
                                purpose = str(
                                    intent.get("purpose")
                                    or intent.get("prompt")
                                    or "").strip()
                                if purpose:
                                    ui.label(purpose[:180]).classes(
                                        "agenda-purpose")
                                ui.label(
                                    source_label(intent.get("source", ""))
                                ).classes("memorial-source")

                            def cancel(intent_id=intent["id"]):
                                cancelled = mod.cancel_intent(
                                    intent_id,
                                    reason="cancelled from Jarvis agenda",
                                )
                                state = mod.get_intent(intent_id) or {}
                                was_in_flight = (
                                    state.get("cancel_previous_status")
                                    == "triggered"
                                )
                                message = (
                                    "已停止后续触发；本次执行可能已经开始"
                                    if was_in_flight
                                    else "已取消"
                                )
                                ui.notify(
                                    message if cancelled else "取消失败",
                                    type="warning" if cancelled else "negative",
                                )
                                content.refresh()

                            ui.button(
                                icon="event_busy", on_click=cancel,
                            ).props("flat round dense").tooltip("取消这项安排")

            awaiting = mod.awaiting_closures()
            ui.separator()
            ui.label("贰 · 等结果").classes("section-kicker")
            ui.label("已经做了，还等你确认结果").classes("section-title")
            if not awaiting:
                ui.label("没有等待确认的结果。").classes("empty-guidance")
            for intent in awaiting:
                age_days = awaiting_age_days(intent, now)
                with ui.element("article").classes("agenda-followup"):
                    with ui.column().classes("gap-1 min-w-0"):
                        ui.label(intent["name"]).classes("agenda-title")
                        ui.label(
                            intent.get("closure_question")
                            or intent.get("purpose")
                            or "后来怎么样了？"
                        ).classes("agenda-purpose")
                        ui.label(
                            f"等待 {age_days:.1f} 天 · "
                            f"{_CATEGORY_LABELS.get(intent.get('category'), '跟进')}"
                        ).classes("agenda-when")

                    def close(intent_id=intent["id"]):
                        message, tone = close_intent_feedback(intent_id)
                        ui.notify(message, type=tone)
                        content.refresh()

                    ui.button(
                        icon="check", on_click=close,
                    ).props("unelevated round").tooltip("记为办结")

            autopsy = expired_autopsy(20)
            ui.separator()
            ui.label("叁 · 过期未办").classes("section-kicker")
            ui.label("需要重新安排的承诺").classes("section-title")
            if not autopsy:
                ui.label("没有过期未办的事。").classes("empty-guidance")
            for intent in autopsy:
                with ui.element("article").classes("agenda-followup"):
                    with ui.column().classes("gap-1 min-w-0"):
                        ui.label(intent["name"]).classes("agenda-title")
                        ui.label(
                            f"原定 {intent['was_due']}"
                        ).classes("agenda-when")
                        if intent.get("last_error"):
                            ui.label(
                                str(intent["last_error"])[:180]
                            ).classes("agenda-purpose")

                    def rearm(intent_id=intent["id"]):
                        result = rearm_intent(intent_id)
                        ui.notify(
                            f"已重新安排：{result}" if result else "重新安排失败",
                            type="positive" if result else "negative",
                        )
                        content.refresh()

                    ui.button(
                        icon="replay", on_click=rearm,
                    ).props("outline round").tooltip("重新安排")

            funnel = compute_funnel(7)
            ui.separator()
            ui.label("肆 · 七天闭环").classes("section-kicker")
            with ui.element("div").classes("metric-strip"):
                for label, key in (
                    ("新建", "created"),
                    ("触发", "fired"),
                    ("执行", "executed"),
                    ("闭环", "closed"),
                    ("过期", "expired"),
                    ("静默丢失", "leaked"),
                ):
                    with ui.element("div").classes("metric-cell"):
                        ui.label(str(funnel[key])).classes(
                            "metric-value"
                            + (" is-alert"
                               if key in {"expired", "leaked"} and funnel[key]
                               else ""))
                        ui.label(label).classes("metric-label")

        content()
        guarded_refresh_timer(30, content.refresh)

        ui.separator()
        ui.label("伍 · 新建").classes("section-kicker")
        ui.label("交办一件将来的事").classes("section-title")
        with ui.element("section").classes("agenda-create"):
            name_input = ui.input(
                "名称", placeholder="例如：周三复诊"
            ).classes("w-full").props("outlined")
            with ui.row().classes("w-full gap-3 agenda-create-row"):
                trigger_type = ui.select(
                    {
                        "date": "指定时间",
                        "cron": "周期计划",
                        "interval": "固定间隔",
                    },
                    value="date",
                    label="触发方式",
                ).props("outlined").classes("item-filter-select")
                trigger_value = ui.input(
                    "时间或周期",
                    placeholder="2026-07-30T15:00",
                ).props("outlined").classes("flex-1")
            prompt_input = ui.textarea(
                "到时候做什么",
                placeholder="提醒我，并带上需要的上下文。",
            ).classes("w-full").props("outlined")
            with ui.row().classes("w-full gap-3 agenda-create-row"):
                category_select = ui.select(
                    {
                        key: label
                        for key, label in _CATEGORY_LABELS.items()
                        if key in CLOSURE_POLICY
                    },
                    value="none",
                    label="结果跟进",
                ).props("outlined").classes("item-filter-select")
                closure_input = ui.input(
                    "结果追问",
                    placeholder="后来怎么样了？",
                ).props("outlined").classes("flex-1")

            def create_intent():
                name = str(name_input.value or "").strip()
                raw_trigger = str(trigger_value.value or "").strip()
                if not name or not raw_trigger:
                    ui.notify("请填写名称和时间", type="warning")
                    return
                selected_type = str(trigger_type.value or "date")
                category = str(category_select.value or "none")
                closure_question = str(
                    closure_input.value or "").strip()
                trigger_config, validation_error = agenda_trigger_config(
                    selected_type,
                    raw_trigger,
                    category=category,
                    closure_question=closure_question,
                )
                if validation_error:
                    ui.notify(validation_error, type="warning")
                    return
                try:
                    mod.create_intent(
                        name=name,
                        trigger_type=selected_type,
                        trigger_config=trigger_config,
                        prompt=str(prompt_input.value or "").strip(),
                        source="dashboard",
                        action_type="notify",
                        category=category,
                        closure_question=closure_question,
                    )
                except ValueError as exc:
                    ui.notify(f"创建失败：{exc}", type="negative")
                    return
                name_input.value = ""
                trigger_value.value = ""
                prompt_input.value = ""
                closure_input.value = ""
                ui.notify("已排上 Jarvis 日历", type="positive")
                content.refresh()

            ui.button(
                "交办", icon="add", on_click=create_intent,
            ).props("unelevated no-caps")
