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
from core.intentions import _coerce

from nicegui import ui



JARVIS_DIR = Path(__file__).parent.parent.parent

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


@ui.page("/intentions")
def intentions_page():
    ui.navigate.to("/items")
