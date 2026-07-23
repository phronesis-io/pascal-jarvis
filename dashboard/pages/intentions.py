"""意图页 — 闭环漏斗 + 将来要做的事的时间线。

REQ-45: the funnel header makes state-transition leaks visible (created→
fired→executed→closed per 7d window, 静默丢弃 highlighted), the 过期尸检
list gives every auto-expired commitment a one-click re-arm, and the create
form captures category/closure_question so dashboard-created intents stop
defaulting to closure-free 'none'.
"""

import json
import time
from datetime import datetime, timedelta
from pathlib import Path

from core.timeutil import now_local
# _coerce aligns tz-awareness with now_local() — the live DB holds BOTH naive
# ('2026-09-25T09:00:00') and aware ('2026-06-13T12:30:00+08:00') datetimes,
# and naive-aware subtraction raises TypeError (the old 500 on every load).
from core.intentions import _coerce, CLOSURE_POLICY

from nicegui import ui

from ..uiutil import guarded_refresh_timer, jarvis_page, source_label

JARVIS_DIR = Path(__file__).parent.parent.parent

# 状态键 → 人话。页面上不直接打印 pending/executed 这类内部枚举。
_STATUS_LABELS = {
    "pending": "待执行",
    "triggered": "已触发",
    "executed": "已执行",
    "expired": "已过期",
    "cancelled": "已取消",
}

# 闭环类别键 → 人话（创建表单和待闭环标签共用）。
_CATEGORY_LABELS = {
    "hard": "硬性承诺（会跟进）",
    "context": "背景记录",
    "healing": "自我修复",
    "external": "等外部结果",
    "autonomous": "自主学习",
    "none": "无需闭环",
}


def _category_label(category: str) -> str:
    return _CATEGORY_LABELS.get(str(category or ""), str(category or "未分类"))


# Import lazily to avoid circular issues at module load
def _get_intentions_module():
    import sys
    path = str(JARVIS_DIR)
    if path not in sys.path:  # 每 30s 刷新都会进来 — 不能无限增长 sys.path
        sys.path.insert(0, path)
    from core import intentions
    return intentions


def _parse_trigger_when(intent: dict) -> str:
    """Human-readable trigger description."""
    tc = json.loads(intent["trigger_config"]) if isinstance(intent["trigger_config"], str) else intent["trigger_config"]
    tt = intent["trigger_type"]
    if tt == "date":
        dt_str = tc.get("datetime", "")
        if dt_str:
            try:
                dt = _coerce(datetime.fromisoformat(dt_str))
                now = now_local()
                delta = dt - now
                if delta.total_seconds() < 0:
                    return f"已过期 ({dt.strftime('%m/%d %H:%M')})"
                elif delta.total_seconds() < 3600:
                    return f"{int(delta.total_seconds() / 60)}分钟后"
                elif delta.total_seconds() < 86400:
                    return f"{int(delta.total_seconds() / 3600)}小时后"
                else:
                    return dt.strftime("%m/%d %H:%M")
            except (ValueError, TypeError):
                return dt_str
    elif tt == "cron":
        return f"周期计划 {tc.get('expression', '')}"
    elif tt == "interval":
        secs = tc.get("seconds", 0)
        if secs < 60:
            return f"每 {secs}秒"
        elif secs < 3600:
            return f"每 {secs // 60}分钟"
        else:
            return f"每 {secs // 3600}小时"
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
        try:
            cfg = json.loads(it["trigger_config"]) if isinstance(it["trigger_config"], str) else (it["trigger_config"] or {})
        except (json.JSONDecodeError, TypeError):
            cfg = {}
        it["was_due"] = cfg.get("datetime") or cfg.get("expression") or "?"
        out.append(it)
    return out


def rearm_intent(intent_id: str) -> bool | str:
    """一键复活: status='pending'、attempt=0、清 expires_at、重锚调度 (REQ-45).

    Returns False if the intent is missing, else a short human description of
    when it will next fire (truthy) so the toast can be honest per trigger type.

    expires_at MUST be cleared: many expired date rows carry a PAST expires_at,
    and get_due_intents() runs cleanup_expired() first — which flips a re-armed
    'pending' row straight back to 'expired' (WHERE expires_at < now) before it
    can ever fire (silent re-expiry, success toast lying).

    Per trigger type:
    - date: rewrite trigger_config.datetime = now+10min.
    - cron: trigger_config (the expression) is untouched — overwriting it with a
      datetime would destroy the schedule. Instead recompute next_fire_at via
      cron_next() so a stale-past watermark doesn't fire instantly, nor a future
      one get skipped as >CRON_STALENESS late.
    - interval: re-anchor the schedule by resetting created_at=now (the interval
      due-check anchors on created_at), so it fires within one interval rather
      than instantly off a stale anchor.
    All status/expires_at/attempt/anchor writes land so the row is never left
    half-updated. next_fire_at/created_at/attempt are not in update_intent's
    allowed set — written via a direct UPDATE on the same connection.
    """
    mod = _get_intentions_module()
    mod._init()
    it = mod.get_intent(intent_id)
    if not it:
        return False

    tt = it.get("trigger_type")
    db = mod._get_db()

    if tt == "date":
        new_dt = (now_local() + timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%S")
        # trigger_config + status + expires_at via update_intent (all allowed);
        # attempt/last_error via a direct UPDATE (not in the allowed set).
        mod.update_intent(intent_id, status="pending",
                          trigger_config={"datetime": new_dt}, expires_at=None)
        db.execute(
            "UPDATE intentions SET attempt = 0, last_error = ? WHERE id = ?",
            ("re-armed from dashboard (+10min)", intent_id),
        )
        db.commit()
        return "10分钟后触发"

    if tt == "cron":
        try:
            tc = json.loads(it["trigger_config"]) if isinstance(it["trigger_config"], str) else (it["trigger_config"] or {})
        except (json.JSONDecodeError, TypeError):
            tc = {}
        expr = tc.get("expression", "")
        from dashboard.scheduler import cron_next
        nxt = cron_next(expr, after=now_local())
        nxt_iso = nxt.isoformat() if nxt else None
        mod.update_intent(intent_id, status="pending", expires_at=None)
        db.execute(
            "UPDATE intentions SET attempt = 0, last_error = ?, next_fire_at = ? WHERE id = ?",
            ("re-armed from dashboard (cron re-anchored)", nxt_iso, intent_id),
        )
        db.commit()
        if nxt:
            return f"下次触发: {nxt.strftime('%m/%d %H:%M')}"
        return "已复活 (周期表达式无效，算不出下次触发时间)"

    # interval (and any other non-date/non-cron): re-anchor on created_at=now.
    new_anchor = now_local().strftime("%Y-%m-%dT%H:%M:%S")
    mod.update_intent(intent_id, status="pending", expires_at=None)
    db.execute(
        "UPDATE intentions SET attempt = 0, last_error = ?, created_at = ? WHERE id = ?",
        ("re-armed from dashboard (interval re-anchored)", new_anchor, intent_id),
    )
    db.commit()
    return "一个周期内触发"


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


def _status_badge(status: str):
    """Render a colored badge for intent status (人话标签)."""
    colors = {
        "pending": "blue",
        "triggered": "amber",
        "executed": "green",
        "expired": "gray",
        "cancelled": "red",
    }
    return ui.badge(_STATUS_LABELS.get(status, status),
                    color=colors.get(status, "gray")).classes("text-xs")


def _intent_meta_tag(intent: dict) -> None:
    """来源做成小标签；内部 id 收进 tooltip，不占版面。"""
    tag = ui.label(source_label(intent.get("source", ""))).classes(
        "activity-source")
    with tag:
        ui.tooltip(f"内部编号 {intent.get('id', '')}")


@ui.page("/intentions")
def intentions_page():
    """意图漏斗 + 时间线。"""
    ui.navigate.to("/items")
    return
    mod = _get_intentions_module()

    with jarvis_page("/intentions", "意图",
                     "Jarvis 答应过将来要做的事：什么时候做、做没做、有没有交代结果。"):

        @ui.refreshable
        def content():
            # ── 漏斗头 (REQ-45): 7d created→fired→executed→closed + 泄漏率 ──
            funnel = compute_funnel(7)
            ui.label("壹 · 七天漏斗").classes("section-kicker")
            ui.label("最近七天").classes("section-title")
            with ui.element("div").classes("metric-strip"):
                for label, key in (
                    ("已建", "created"),
                    ("已触发", "fired"),
                    ("已执行", "executed"),
                    ("已过期", "expired"),
                    ("已追问", "closure_asked"),
                    ("已闭环", "closed"),
                ):
                    with ui.element("div").classes("metric-cell"):
                        alert = key == "expired" and funnel[key] > 0
                        ui.label(str(funnel[key])).classes(
                            "metric-value" + (" is-alert" if alert else ""))
                        ui.label(label).classes("metric-label")
            leak_note = (
                f"本周触发 {funnel['fired']} 件、执行 {funnel['executed']} 件；"
                f"没交代就没了的有 {funnel['leaked']} 件"
                + ("，需要看看。" if funnel["leaked"] else "。")
                + (f"另有 {funnel['written_off']} 件系统记为「不再跟」（没真追问过）。"
                   if funnel["written_off"] else "")
            )
            leak_cls = "text-red-600 font-medium" if funnel["leaked"] else ""
            ui.label(leak_note).classes(f"section-note {leak_cls}")

            # 存量统计（全部历史）
            stats = mod.intent_stats()
            ui.label(
                f"存量：待执行 {stats.get('pending', 0)} · "
                f"已执行 {stats.get('executed', 0)} · "
                f"已过期 {stats.get('expired', 0)} · "
                f"已取消 {stats.get('cancelled', 0)}"
            ).classes("section-note")

            # ── 即将发生 ──
            ui.label("贰 · 即将发生").classes("section-kicker mt-4")
            ui.label("排上日程的事").classes("section-title")
            pending = mod.list_intents(status="pending", limit=50)

            if pending:
                for intent in pending:
                    tags = json.loads(intent["tags"]) if isinstance(intent["tags"], str) else (intent["tags"] or [])
                    with ui.card().classes("w-full p-3"):
                        with ui.row().classes("w-full items-start justify-between"):
                            with ui.column().classes("gap-1 flex-1"):
                                ui.label(intent["name"]).classes("font-medium")
                                ui.label(_parse_trigger_when(intent)).classes("text-xs text-blue-500")
                                if intent.get("purpose"):
                                    ui.label(intent["purpose"]).classes("text-xs text-gray-500 italic")
                                if intent.get("prompt"):
                                    prompt_preview = intent["prompt"][:120] + ("…" if len(intent["prompt"]) > 120 else "")
                                    ui.label(prompt_preview).classes("text-xs text-gray-400 mt-1")
                                # ⚠无闭环 drift: a chasing-category row with no closure
                                # question. healing/autonomous/context/none excluded —
                                # the dashboard never flags health/learning as "missing".
                                if (not (intent.get("closure_question") or "").strip()
                                        and intent.get("category") in ("hard", "external")):
                                    ui.badge("没约好怎么算办完", color="orange").classes("text-xs mt-1")
                                if tags:
                                    with ui.row().classes("gap-1 mt-1"):
                                        for tag in tags[:3]:
                                            ui.badge(tag, color="purple").props("outline").classes("text-xs")
                            with ui.column().classes("items-end gap-1"):
                                _intent_meta_tag(intent)

                                async def cancel_click(iid=intent["id"]):
                                    mod.cancel_intent(iid, reason="cancelled from dashboard")
                                    ui.notify("已取消这条意图", type="warning")
                                    content.refresh()

                                ui.button("取消", on_click=cancel_click).props("flat dense size=xs color=red")
            else:
                ui.label("暂时没有排上日程的意图。日程和对话里出现要跟进的事时，"
                         "Jarvis 会自动记在这里。").classes("empty-guidance")

            # ── 过期尸检 (REQ-45): every dropped commitment, with re-arm ──
            ui.separator()
            ui.label("叁 · 过期未办").classes("section-kicker mt-2")
            ui.label("过了时间没办成的事（可以再来一次）").classes("section-title")
            autopsy = expired_autopsy(20)
            if autopsy:
                for intent in autopsy:
                    with ui.card().classes("w-full p-3"):
                        with ui.row().classes("w-full items-center justify-between"):
                            with ui.column().classes("gap-0 flex-1"):
                                ui.label(intent["name"]).classes("font-medium text-sm")
                                ui.label(f"原定: {intent['was_due']}").classes("text-xs text-gray-500")
                                if intent.get("last_error"):
                                    ui.label(f"上次出错：{intent['last_error'][:120]}").classes(
                                        "text-xs text-red-400")

                            async def rearm_click(iid=intent["id"]):
                                result = rearm_intent(iid)
                                if result:
                                    ui.notify(f"已重新安排：{result}", type="positive")
                                else:
                                    ui.notify("重新安排失败", type="negative")
                                content.refresh()

                            ui.button("再来一次", on_click=rearm_click).props("flat dense size=xs")
            else:
                ui.label("没有过期未办的事。").classes("empty-guidance")

            # 待闭环 — moments that fired and await a result. Exclude healing/
            # autonomous: the dashboard never scores health/learning follow-through.
            awaiting = mod.awaiting_closures()
            ui.separator()
            ui.label("肆 · 等结果").classes("section-kicker mt-2")
            ui.label("做了、还没交代结果的事（不催你）").classes("section-title")
            if awaiting:
                for intent in awaiting:
                    age_d = awaiting_age_days(intent)
                    is_zombie = age_d > 3
                    with ui.card().classes("w-full p-3"):
                        with ui.row().classes("w-full items-center justify-between"):
                            with ui.column().classes("gap-0 flex-1"):
                                ui.label(intent["name"]).classes("font-medium text-sm")
                                ui.label(intent.get("closure_question") or intent.get("purpose", "")).classes(
                                    "text-xs text-amber-600")
                                age_cls = "text-red-500" if is_zombie else "text-gray-400"
                                ui.label(f"等了 {age_d:.1f} 天" + ("，过期未闭环" if is_zombie else "")).classes(
                                    f"text-xs {age_cls}")
                            ui.badge(_category_label(intent.get("category")),
                                     color="amber").props("outline").classes("text-xs")

                            async def close_click(iid=intent["id"]):
                                mod.record_closure(iid, outcome="done", result="closed from dashboard")
                                ui.notify("已记为办结", type="positive")
                                content.refresh()

                            ui.button("办结", on_click=close_click).props("flat dense size=xs color=green")
            else:
                ui.label("没有等结果的事。").classes("empty-guidance")

            # Recently executed
            ui.separator()
            ui.label("伍 · 最近办完").classes("section-kicker mt-2")
            ui.label("最近执行过的意图").classes("section-title")
            executed = mod.list_intents(status="executed", limit=10)
            if executed:
                for intent in executed:
                    with ui.card().classes("w-full p-3"):
                        with ui.row().classes("w-full items-center justify-between"):
                            with ui.column().classes("gap-0"):
                                ui.label(intent["name"]).classes("font-medium text-sm")
                                if intent.get("executed_at"):
                                    ui.label(f"执行于 {intent['executed_at'][:16]}").classes(
                                        "text-xs text-gray-400")
                            _status_badge("executed")
            else:
                ui.label("还没有执行过的意图。").classes("empty-guidance")

        content()
        guarded_refresh_timer(30, content.refresh)

        # Create new intent form
        ui.separator()
        ui.label("陆 · 新建").classes("section-kicker mt-2")
        ui.label("交办一件将来的事").classes("section-title")

        with ui.card().classes("w-full p-4"):
            name_input = ui.input("名称", placeholder="给这件事起个名字").classes("w-full")
            with ui.row().classes("w-full gap-4"):
                trigger_type = ui.select(
                    {"date": "指定时间", "cron": "周期计划", "interval": "固定间隔"},
                    value="date", label="什么时候触发",
                )
                trigger_value = ui.input(
                    "时间",
                    placeholder="填触发时间/周期",
                ).classes("flex-1").props(
                    'hint="格式示例 — 指定时间: 2026-05-22T09:00（年-月-日T时:分）· '
                    '周期计划: 0 9 * * *（每天 9 点）· 固定间隔: 3600（秒）"'
                )
            prompt_input = ui.textarea(
                "到时候做什么（写给 Jarvis 的话）",
                placeholder="到时提醒我跟进这件事的进展……"
            ).classes("w-full")
            purpose_input = ui.input("为什么要做", placeholder="记下这件事的来龙去脉").classes("w-full")
            # 闭环字段 (REQ-45): 堵创建侧 'none' 漏洞 — dashboard 建的 intent
            # 也要进闭环轴。category 直接取 CLOSURE_POLICY 的键。
            with ui.row().classes("w-full gap-4"):
                category_select = ui.select(
                    {k: _CATEGORY_LABELS.get(k, k) for k in CLOSURE_POLICY},
                    value="none", label="事后要不要交代结果",
                )
                closure_input = ui.input(
                    "闭环追问（做了吗？怎么样？）",
                    placeholder="后来怎么样了？"
                ).classes("flex-1")
            tags_input = ui.input("标签（逗号分隔）", placeholder="健康, 跟进").classes("w-full")

            async def create_intent_click():
                name = name_input.value.strip()
                if not name:
                    ui.notify("名称不能为空", type="warning")
                    return
                tt = trigger_type.value
                tv = trigger_value.value.strip()
                if tt == "date":
                    tc = {"datetime": tv}
                elif tt == "cron":
                    tc = {"expression": tv}
                else:
                    tc = {"seconds": int(tv) if tv.isdigit() else 600}
                tags = [t.strip() for t in tags_input.value.split(",") if t.strip()]

                try:
                    # create_intent RAISES ValueError on empty/invalid datetime
                    # or cron expression — surface it instead of 500ing.
                    mod.create_intent(
                        name=name,
                        trigger_type=tt,
                        trigger_config=tc,
                        prompt=prompt_input.value.strip(),
                        purpose=purpose_input.value.strip(),
                        tags=tags,
                        source="dashboard",
                        action_type="notify",
                        category=category_select.value or "none",
                        closure_question=closure_input.value.strip(),
                    )
                except ValueError as e:
                    ui.notify(f"创建失败: {e}", type="negative")
                    return
                ui.notify(f"「{name}」已排上日程", type="positive")
                name_input.value = ""
                trigger_value.value = ""
                prompt_input.value = ""
                purpose_input.value = ""
                closure_input.value = ""
                tags_input.value = ""
                content.refresh()

            ui.button("交办", on_click=create_intent_click).classes("mt-2")
