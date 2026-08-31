"""What the owner already answered, and what still waits on his desk.

Read side of the memorial ledger for tasks that draft asks (intention-check):
a recurring intent is a fresh model call with no view of the ledger, so
without this feed a settled matter (「先都放着」) or one already pending came
back as a reworded decision card day after day (2026-08-25 audit).
"""
from __future__ import annotations

import time
from datetime import datetime

VERDICT_LOOKBACK_DAYS = 7
VERDICT_LIST_MAX = 12


def _ledger_ts_epoch(value: str) -> float | None:
    try:
        return datetime.strptime(str(value), "%Y-%m-%d %H:%M").timestamp()
    except (ValueError, TypeError):
        return None


def recent_verdicts(days: int = VERDICT_LOOKBACK_DAYS,
                    limit: int = VERDICT_LIST_MAX,
                    now: float | None = None) -> list[dict]:
    """What the owner already answered, and what still waits on his desk.

    Decision cards decided or 留中 within ``days`` plus decision cards still
    pending from that window, newest first. A task that drafts asks
    (intention-check) reads this so a settled matter (「先都放着」) or one
    already in front of him is not opened again as a fresh card — the
    2026-08-25 audit found one blog-deadline matter asked 7 times in 6 days,
    4 of them after he had answered, because every recurring intent is a
    fresh model call with no view of the ledger.
    """
    from core import memorial as m
    now_epoch = time.time() if now is None else float(now)
    floor = now_epoch - max(1, int(days)) * 86400
    rows: list[tuple[float, int, dict]] = []
    for order, st in enumerate(m._fold(m.read_jsonl(m._ledger_path())).values()):
        if not m.counts_in_ledger(st):
            continue
        if str(st.get("attention", "")) != m.ATTENTION_DECISION:
            continue
        status = st.get("status")
        if status == "decided":
            when = _ledger_ts_epoch(st.get("decided_ts"))
            verdict = str(st.get("decided_label") or st.get("decided_opt") or "已处理")
        elif status == m.STATUS_LAPSED:
            when = _ledger_ts_epoch(st.get("lapsed_ts"))
            verdict = str(st.get("lapse_reason") or "留中")
        elif status == "pending":
            when = _ledger_ts_epoch(st.get("ts"))
            verdict = "仍在他桌上等批"
        else:
            continue
        if when is None or when < floor:
            continue
        # Ledger timestamps are minute-grained; later rows win ties.
        rows.append((when, order, {
            "title": str(st.get("title", "")),
            "source": str(st.get("source", "")),
            "status": str(status),
            "verdict": verdict,
            "ts": datetime.fromtimestamp(when).strftime("%Y-%m-%d %H:%M"),
        }))
    rows.sort(key=lambda r: (r[0], r[1]), reverse=True)
    return [r[2] for r in rows[:max(1, int(limit))]]
