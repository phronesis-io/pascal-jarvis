"""Intentions page — closure funnel + timeline of agent's future actions.

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

from ..uiutil import guarded_refresh_timer

JARVIS_DIR = Path(__file__).parent.parent.parent

# Import lazily to avoid circular issues at module load
def _get_intentions_module():
    import sys
    sys.path.insert(0, str(JARVIS_DIR))
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
        return f"cron: {tc.get('expression', '')}"
    elif tt == "interval":
        secs = tc.get("seconds", 0)
        if secs < 60:
            return f"每 {secs}秒"
        elif secs < 3600:
            return f"每 {secs // 60}分钟"
        else:
            return f"每 {secs // 3600}小时"
    return "?"


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
        "closure_asked": _count(
            "SELECT COUNT(*) FROM intentions WHERE closure_status != 'none' "
            "AND created_at >= ?", (cutoff,)),
        "closed": _count(
            "SELECT COUNT(*) FROM intentions "
            "WHERE closure_status IN ('done', 'recorded', 'na') "
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
        return "已复活 (cron 表达式无效，无法计算下次触发)"

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
    """Render a colored badge for intent status."""
    colors = {
        "pending": "blue",
        "triggered": "amber",
        "executed": "green",
        "expired": "gray",
        "cancelled": "red",
    }
    return ui.badge(status, color=colors.get(status, "gray")).classes("text-xs")


@ui.page("/intentions")
def intentions_page():
    """Intentions funnel + timeline page."""
    mod = _get_intentions_module()

    ui.add_head_html("""
    <style>
        .intent-card { border: 1px solid #e5e7eb; border-radius: 8px; padding: 0.75rem;
                       transition: all 0.15s; position: relative; }
        .intent-card:hover { border-color: #8b5cf6; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }
        .intent-pending { border-left: 3px solid #3b82f6; }
        .intent-executed { border-left: 3px solid #22c55e; opacity: 0.7; }
        .intent-cancelled { border-left: 3px solid #ef4444; opacity: 0.5; }
        .intent-expired { border-left: 3px solid #9ca3af; opacity: 0.5; }
        .timeline-dot { width: 12px; height: 12px; border-radius: 50%;
                        display: inline-block; margin-right: 8px; }
        .dot-pending { background: #3b82f6; }
        .dot-executed { background: #22c55e; }
        .dot-cancelled { background: #ef4444; }
        .funnel-card { background: #f9fafb; border-radius: 10px; padding: 0.6rem 0.9rem; }
    </style>
    """)

    with ui.column().classes("w-full max-w-4xl mx-auto p-4 gap-4"):
        with ui.row().classes("w-full items-center justify-between"):
            ui.link("← Home", "/").classes("text-gray-500 text-sm")
            ui.label("🎯 Intentions").classes("text-2xl font-bold")

        @ui.refreshable
        def content():
            # ── 漏斗头 (REQ-45): 7d created→fired→executed→closed + 泄漏率 ──
            funnel = compute_funnel(7)
            ui.label("7 天漏斗").classes("text-lg font-semibold")
            with ui.row().classes("w-full gap-3"):
                for label, key, cls in (
                    ("created", "created", "text-gray-700"),
                    ("fired", "fired", "text-blue-600"),
                    ("executed", "executed", "text-green-600"),
                    ("expired", "expired", "text-gray-400"),
                    ("closure-asked", "closure_asked", "text-amber-600"),
                    ("closed", "closed", "text-purple-600"),
                ):
                    with ui.card().classes("funnel-card"):
                        ui.label(str(funnel[key])).classes(f"text-xl font-bold {cls}")
                        ui.label(label).classes("text-xs text-gray-500")
            leak_cls = "text-red-600 font-medium" if funnel["leaked"] else "text-gray-400"
            ui.label(
                f"本周 fired {funnel['fired']}, executed {funnel['executed']}, "
                f"静默丢弃 {funnel['leaked']}" + (" ⚠" if funnel["leaked"] else "")
            ).classes(f"text-sm {leak_cls}")

            # Stats
            stats = mod.intent_stats()
            with ui.row().classes("gap-4"):
                ui.label(f"Pending: {stats.get('pending', 0)}").classes("text-blue-600 font-medium")
                ui.label(f"Executed: {stats.get('executed', 0)}").classes("text-green-600")
                ui.label(f"Expired: {stats.get('expired', 0)}").classes("text-gray-400")
                ui.label(f"Cancelled: {stats.get('cancelled', 0)}").classes("text-red-400")

            # Active intents (pending)
            ui.label("Upcoming").classes("text-lg font-semibold mt-4")
            pending = mod.list_intents(status="pending", limit=50)

            if pending:
                for intent in pending:
                    tags = json.loads(intent["tags"]) if isinstance(intent["tags"], str) else (intent["tags"] or [])
                    with ui.card().classes(f"intent-card intent-pending w-full"):
                        with ui.row().classes("w-full items-start justify-between"):
                            with ui.column().classes("gap-1 flex-1"):
                                with ui.row().classes("items-center gap-2"):
                                    ui.html('<span class="timeline-dot dot-pending"></span>')
                                    ui.label(intent["name"]).classes("font-medium")
                                ui.label(_parse_trigger_when(intent)).classes("text-xs text-blue-500")
                                if intent.get("purpose"):
                                    ui.label(intent["purpose"]).classes("text-xs text-gray-500 italic")
                                if intent.get("prompt"):
                                    prompt_preview = intent["prompt"][:120] + ("..." if len(intent["prompt"]) > 120 else "")
                                    ui.label(prompt_preview).classes("text-xs text-gray-400 mt-1")
                                # ⚠无闭环 drift: a chasing-category row with no closure
                                # question. healing/autonomous/context/none excluded —
                                # the dashboard never flags health/learning as "missing".
                                if (not (intent.get("closure_question") or "").strip()
                                        and intent.get("category") in ("hard", "external")):
                                    ui.badge("⚠ 无闭环", color="orange").classes("text-xs mt-1")
                                if tags:
                                    with ui.row().classes("gap-1 mt-1"):
                                        for tag in tags[:3]:
                                            ui.badge(tag, color="purple").props("outline").classes("text-xs")
                            with ui.column().classes("items-end gap-1"):
                                ui.label(f"src: {intent['source']}").classes("text-xs text-gray-400")
                                ui.label(f"id: {intent['id'][:12]}").classes("text-xs text-gray-300")

                                async def cancel_click(iid=intent["id"]):
                                    mod.cancel_intent(iid, reason="cancelled from dashboard")
                                    ui.notify("Intent cancelled", type="warning")
                                    content.refresh()

                                ui.button("Cancel", on_click=cancel_click).props("flat dense size=xs color=red")
            else:
                ui.label("No pending intents. The agent will create them from calendar events and conversations.").classes(
                    "text-gray-400 text-sm italic"
                )

            # ── 过期尸检 (REQ-45): every dropped commitment, with re-arm ──
            ui.separator()
            ui.label("💀 过期尸检 (expired · 可复活)").classes("text-lg font-semibold mt-2")
            autopsy = expired_autopsy(20)
            if autopsy:
                for intent in autopsy:
                    with ui.card().classes("intent-card intent-expired w-full"):
                        with ui.row().classes("w-full items-center justify-between"):
                            with ui.column().classes("gap-0 flex-1"):
                                ui.label(intent["name"]).classes("font-medium text-sm")
                                ui.label(f"原定: {intent['was_due']}").classes("text-xs text-gray-500")
                                if intent.get("last_error"):
                                    ui.label(intent["last_error"][:120]).classes("text-xs text-red-400")

                            async def rearm_click(iid=intent["id"]):
                                result = rearm_intent(iid)
                                if result:
                                    ui.notify(f"已复活：{result}", type="positive")
                                else:
                                    ui.notify("复活失败", type="negative")
                                content.refresh()

                            ui.button("⟳ re-arm", on_click=rearm_click).props("flat dense size=xs color=purple")
            else:
                ui.label("无过期意图。").classes("text-gray-400 text-sm italic")

            # 🔄 待闭环 — moments that fired and await a result. Exclude healing/
            # autonomous: the dashboard never scores health/learning follow-through.
            awaiting = mod.awaiting_closures()
            ui.separator()
            ui.label("🔄 待闭环 (awaiting result · 不催)").classes("text-lg font-semibold mt-2")
            if awaiting:
                for intent in awaiting:
                    age_d = awaiting_age_days(intent)
                    is_zombie = age_d > 3
                    with ui.card().classes("intent-card w-full").style("border-left:3px solid #f59e0b"):
                        with ui.row().classes("w-full items-center justify-between"):
                            with ui.column().classes("gap-0 flex-1"):
                                ui.label(intent["name"]).classes("font-medium text-sm")
                                ui.label(intent.get("closure_question") or intent.get("purpose", "")).classes(
                                    "text-xs text-amber-600")
                                age_cls = "text-red-500" if is_zombie else "text-gray-400"
                                ui.label(f"等了 {age_d:.1f} 天" + (" 🧟 zombie" if is_zombie else "")).classes(
                                    f"text-xs {age_cls}")
                            ui.badge(intent.get("category", "?"), color="amber").props("outline").classes("text-xs")

                            async def close_click(iid=intent["id"]):
                                mod.record_closure(iid, outcome="done", result="closed from dashboard")
                                ui.notify("Closure recorded", type="positive")
                                content.refresh()

                            ui.button("✓ done", on_click=close_click).props("flat dense size=xs color=green")
            else:
                ui.label("No open loops awaiting a result.").classes("text-gray-400 text-sm italic")

            # Recently executed
            ui.separator()
            ui.label("Recently Executed").classes("text-lg font-semibold mt-2")
            executed = mod.list_intents(status="executed", limit=10)
            if executed:
                for intent in executed:
                    with ui.card().classes("intent-card intent-executed w-full"):
                        with ui.row().classes("w-full items-center justify-between"):
                            with ui.column().classes("gap-0"):
                                with ui.row().classes("items-center gap-2"):
                                    ui.html('<span class="timeline-dot dot-executed"></span>')
                                    ui.label(intent["name"]).classes("font-medium text-sm")
                                if intent.get("executed_at"):
                                    ui.label(f"executed: {intent['executed_at'][:16]}").classes("text-xs text-gray-400")
                            _status_badge("executed")
            else:
                ui.label("No executed intents yet.").classes("text-gray-400 text-sm italic")

        content()
        guarded_refresh_timer(30, content.refresh)

        # Create new intent form
        ui.separator()
        ui.label("Create Intent").classes("text-lg font-semibold mt-2")

        with ui.card().classes("w-full p-4"):
            name_input = ui.input("Name", placeholder="Follow up on X").classes("w-full")
            with ui.row().classes("w-full gap-4"):
                trigger_type = ui.select(
                    ["date", "cron", "interval"],
                    value="date", label="Trigger type"
                )
                trigger_value = ui.input(
                    "When",
                    placeholder="2026-05-22T09:00 (date) or 0 9 * * * (cron) or 3600 (interval)"
                ).classes("flex-1")
            prompt_input = ui.textarea(
                "Prompt (what should the agent think about at that moment)",
                placeholder="Check in on Pascal about..."
            ).classes("w-full")
            purpose_input = ui.input("Purpose (why)", placeholder="Follow up on conversation").classes("w-full")
            # 闭环字段 (REQ-45): 堵创建侧 'none' 漏洞 — dashboard 建的 intent
            # 也要进闭环轴。category 直接取 CLOSURE_POLICY 的键。
            with ui.row().classes("w-full gap-4"):
                category_select = ui.select(
                    list(CLOSURE_POLICY.keys()), value="none", label="Category"
                )
                closure_input = ui.input(
                    "Closure question (做了吗？怎么样？)",
                    placeholder="后来怎么样了？"
                ).classes("flex-1")
            tags_input = ui.input("Tags (comma separated)", placeholder="health, follow-up").classes("w-full")

            async def create_intent_click():
                name = name_input.value.strip()
                if not name:
                    ui.notify("Name is required", type="warning")
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
                ui.notify(f"Intent '{name}' created!", type="positive")
                name_input.value = ""
                trigger_value.value = ""
                prompt_input.value = ""
                purpose_input.value = ""
                closure_input.value = ""
                tags_input.value = ""
                content.refresh()

            ui.button("Create Intent", on_click=create_intent_click).classes("mt-2")
