"""Deterministic, result-oriented review of durable Matters.

This is the shared read model for Codex and the low-noise weekly Lark card.
It reports owner-confirmed outcomes, recently released work awaiting owner
closure, and bounded next actions. It never reads raw transcripts and never
mutates Matter state.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta
from typing import Any

from core.timeutil import now_local
from core.matter_prompts import continuation_prompt


_TEXT_LIMITS = {
    "title": 160,
    "summary": 500,
    "next_action": 500,
    "outcome": 500,
}
_CARD_TITLE_LIMIT = 80
_CARD_DETAIL_LIMIT = 240


def _receipt_json(path: str) -> str:
    """Return a JSON1 expression that tolerates malformed legacy payloads."""
    return (
        "json_extract(CASE WHEN json_valid(e.payload) "
        f"THEN e.payload ELSE '{{}}' END,'{path}')"
    )


_OWNER_CLOSURE_SQL = f"""
    e.event_type='matter_closure_completed'
    AND e.actor='matter-closure'
    AND {_receipt_json('$.receipt.schema')}=
        'jarvis.matter-closure-receipt.v1'
    AND {_receipt_json('$.receipt.status')}='closed'
    AND {_receipt_json('$.receipt.authority')}='owner_confirmation'
    AND {_receipt_json('$.receipt.matter_id')}=m.id
    AND {_receipt_json('$.receipt.matter_status')}=m.status
    AND {_receipt_json('$.receipt.closed_at')}=m.closed_at
    AND {_receipt_json('$.receipt.outcome')}=m.outcome
    AND COALESCE({_receipt_json('$.receipt.closure_id')},'')<>''
    AND COALESCE({_receipt_json('$.receipt.receipt_digest')},'')<>''
"""


def _db():
    from core.db import get_db

    return get_db()


def _bounded_limit(value: int) -> int:
    return max(1, min(int(value), 20))


def _matter(row: Any) -> dict[str, Any]:
    item = dict(row)
    for field, limit in _TEXT_LIMITS.items():
        if field in item:
            clean = " ".join(str(item.get(field) or "").split())
            item[field] = clean[:limit]
    item["continuation_prompt"] = continuation_prompt(item)
    return item


def build_matter_review(
    *, days: int = 7, limit: int = 8, now: datetime | None = None,
) -> dict[str, Any]:
    """Build one bounded review from authoritative Matter and Run state."""
    current = now or now_local()
    period_days = max(1, min(int(days), 90))
    bounded = _bounded_limit(limit)
    cutoff = current - timedelta(days=period_days)
    cutoff_text = cutoff.strftime("%Y-%m-%dT%H:%M:%S")
    cutoff_epoch = cutoff.timestamp()
    now_epoch = current.timestamp()
    db = _db()

    outcomes = [
        _matter(row)
        for row in db.execute(
            f"""SELECT m.id,m.title,m.outcome,m.kind,m.priority,m.closed_at
               FROM matters m
               WHERE m.status IN ('done','archived')
                 AND m.closed_at >= ?
                 AND EXISTS (
                   SELECT 1 FROM matter_events e
                   WHERE e.matter_id=m.id
                     AND {_OWNER_CLOSURE_SQL}
                 )
               ORDER BY m.closed_at DESC,m.priority DESC
               LIMIT ?""",
            (cutoff_text, bounded),
        ).fetchall()
    ]

    unconfirmed_closed = int(
        db.execute(
            f"""SELECT COUNT(*) FROM matters m
               WHERE m.status IN ('done','archived')
                 AND m.closed_at >= ?
                 AND NOT EXISTS (
                   SELECT 1 FROM matter_events e
                   WHERE e.matter_id=m.id
                     AND {_OWNER_CLOSURE_SQL}
                 )""",
            (cutoff_text,),
        ).fetchone()[0]
    )

    closure_candidates = [
        _matter(row)
        for row in db.execute(
            """SELECT m.id,m.title,m.summary,m.next_action,m.kind,m.status,
                      m.priority,m.updated_at,r.id AS run_id,
                      r.released_epoch,r.result_digest
               FROM matters m
               JOIN matter_runs r ON r.matter_id=m.id
               WHERE m.status IN ('active','waiting','blocked')
                 AND r.run_sequence=(
                   SELECT MAX(r2.run_sequence) FROM matter_runs r2
                   WHERE r2.matter_id=m.id
                 )
                 AND r.status='released'
                 AND TRIM(r.result_digest)<>''
                 AND r.released_epoch >= ?
               ORDER BY r.released_epoch DESC,m.priority DESC
               LIMIT ?""",
            (cutoff_epoch, bounded),
        ).fetchall()
    ]
    candidate_ids = {item["id"] for item in closure_candidates}

    active_run_count = int(
        db.execute(
            """SELECT COUNT(*) FROM matter_runs r
               JOIN matters m ON m.id=r.matter_id
               WHERE m.status IN ('active','waiting','blocked')
                 AND r.status IN ('acquired','running')
                 AND r.lease_expires_epoch > ?""",
            (now_epoch,),
        ).fetchone()[0]
    )
    active_rows = db.execute(
        """SELECT m.id,m.title,m.summary,m.next_action,m.kind,m.status,
                  m.priority,m.updated_at,
                  EXISTS(
                    SELECT 1 FROM matter_runs r
                    WHERE r.matter_id=m.id
                      AND r.status IN ('acquired','running')
                      AND r.lease_expires_epoch > ?
                  ) AS has_active_run
           FROM matters m
           WHERE m.status IN ('active','waiting','blocked')
           ORDER BY CASE m.status
                      WHEN 'blocked' THEN 0
                      WHEN 'waiting' THEN 1
                      ELSE 2
                    END,
                    m.priority DESC,m.updated_at DESC
           LIMIT 500""",
        (now_epoch,),
    ).fetchall()
    active = [_matter(row) for row in active_rows]

    attention = [
        item for item in active
        if item["id"] not in candidate_ids
        and (
            item["status"] in {"blocked", "waiting"}
            or not str(item.get("next_action") or "").strip()
        )
    ][:bounded]
    attention_ids = {item["id"] for item in attention}
    next_actions = [
        item for item in active
        if item["id"] not in candidate_ids
        and item["id"] not in attention_ids
        and not item.get("has_active_run")
        and str(item.get("next_action") or "").strip()
    ][:bounded]

    material = bool(outcomes or closure_candidates or attention or next_actions)
    return {
        "schema": "jarvis.matter-review.v1",
        "generated_at": current.isoformat(timespec="seconds"),
        "period_days": period_days,
        "summary": {
            "confirmed_outcomes": len(outcomes),
            "awaiting_owner_closure": len(closure_candidates),
            "attention": len(attention),
            "next_actions": len(next_actions),
            "active_runs": active_run_count,
        },
        "outcomes": outcomes,
        "closure_candidates": closure_candidates,
        "attention": attention,
        "next_actions": next_actions,
        "integrity": {
            "recent_closed_without_owner_receipt": unconfirmed_closed,
        },
        "material": material,
        "authority": {
            "outcome_requires": "matter_closure_completed",
            "result_receipt_completes_matter": False,
            "read_only": True,
        },
    }


def _line(item: dict[str, Any], detail: str) -> str:
    title = " ".join(str(item.get("title") or "").split())[:_CARD_TITLE_LIMIT]
    clean = " ".join(str(detail or "").split())[:_CARD_DETAIL_LIMIT]
    return f"- **{title}**：{clean}" if clean else f"- **{title}**"


def render_matter_review(report: dict[str, Any], *, per_section: int = 3) -> str:
    """Render a compact Chinese review without turning it into another inbox."""
    if not report.get("material"):
        return ""
    cap = max(1, min(int(per_section), 5))
    sections: list[str] = []
    summary = report.get("summary") or {}
    needs_attention = sum(int(summary.get(key) or 0) for key in (
        "awaiting_owner_closure", "attention", "next_actions",
    ))
    conclusion = (
        f"这周有 {needs_attention} 件事值得你看，最重要的分别列在下面。"
        if needs_attention
        else "这周没有需要你接手的事，结果已经整理好，知道就行。"
    )

    outcomes = report.get("outcomes") or []
    if outcomes:
        lines = [_line(item, item.get("outcome") or "已确认闭环")
                 for item in outcomes[:cap]]
        sections.append("**本周形成的结果**\n" + "\n".join(lines))

    candidates = report.get("closure_candidates") or []
    if candidates:
        lines = [
            _line(item, item.get("next_action") or "已有执行收据，待确认是否收口")
            for item in candidates[:cap]
        ]
        sections.append("**已有产出，尚未确认收口**\n" + "\n".join(lines))

    attention = report.get("attention") or []
    if attention:
        lines = [
            _line(
                item,
                item.get("next_action")
                or item.get("summary")
                or "还没有明确下一步",
            )
            for item in attention[:cap]
        ]
        sections.append("**卡住或等待中**\n" + "\n".join(lines))

    next_actions = report.get("next_actions") or []
    if next_actions:
        lines = [_line(item, item.get("next_action") or "")
                 for item in next_actions[:cap]]
        sections.append("**接下来最值得推进**\n" + "\n".join(lines))

    return "\n\n".join([conclusion, *sections])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--limit", type=int, default=8)
    parser.add_argument("--text", action="store_true")
    args = parser.parse_args(argv)
    report = build_matter_review(days=args.days, limit=args.limit)
    if args.text:
        print(render_matter_review(report))
    else:
        print(json.dumps(report, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
