#!/usr/bin/env python3
"""Post-hook: weekly exercise memorial card (REQ-116).

ONE card, ONE matter: 本周运动 N 次 vs 目标 X-Y 次 + per-activity breakdown.
Claude writes the short body (from the pre-script's aggregate); this hook
wraps it in a memorial (奏折) card with a couple of 批红 options, prints it
for heartbeat_loop's CARD route (create(send=False) — same single-sender
pattern as mail_triage_post), and stamps the ISO-week state so the card goes
out at most once per week.

If Claude's body is unusable, the card body is rendered deterministically
from the aggregate itself — the weekly number should not be lost to a bad
LLM round-trip.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.lifelog import (exercise_card_mark, exercise_card_sent_this_week,
                          exercise_week_summary)
from core.safety import is_idle_reply, looks_like_error, parse_json_response, strip_task_framing

MAX_BODY_CHARS = 300

OPTIONS = [
    {"key": "ack", "label": "知道了", "action": None},
    {"key": "more", "label": "下周想多动一次", "action": None, "reply": True},
]


def _fallback_body(summary: dict) -> str:
    """Deterministic one-matter body straight from the aggregate."""
    n = summary.get("sessions", 0)
    goal = summary.get("goal", "2-3")
    lines = [f"本周运动 {n} 次（目标 {goal} 次）。"]
    by_activity = summary.get("by_activity") or {}
    if by_activity:
        lines.append("、".join(f"{a}×{c}" for a, c in by_activity.items()))
    return "\n".join(lines)


def main() -> int:
    raw = sys.stdin.read().strip()
    if is_idle_reply(raw):
        return 0

    body = ""
    if not looks_like_error(raw):
        parsed = parse_json_response(raw)
        if isinstance(parsed, dict):
            body = str(parsed.get("user_message") or parsed.get("response")
                       or "").strip()
        else:
            body = raw
        body = strip_task_framing(body)[:MAX_BODY_CHARS].strip()

    summary = None
    if not body:
        # Bad/errorish LLM output — render the numbers ourselves rather than
        # dropping the week (pre already gated on a non-empty aggregate).
        try:
            summary = exercise_week_summary()
            body = _fallback_body(summary)
        except Exception as e:
            print(f"[exercise-week] fallback summary failed: {e}",
                  file=sys.stderr)
            return 0
    if not body:
        return 0

    # Once per ISO week — stamp BEFORE printing (same rationale as
    # morning_anchor_post: losing one card beats double-sending it).
    if exercise_card_sent_this_week():
        print("[exercise-week] card already sent this week — dropping duplicate",
              file=sys.stderr)
        return 0
    exercise_card_mark()

    try:
        from core import memorial
        mem_id, _ = memorial.create(
            source="exercise-week", title="本周运动", body=body,
            work_receipt="汇总本周日历运动事件与手记记录并完成次数核对",
            options=OPTIONS, authoring_protocol=True, send=False,
            context="每周日晚的运动小结（REQ-116）：数据来自日历运动事件 + 手记运动条目，纯记录不说教。",
        )
        print(memorial.card_json(mem_id))
    except Exception as e:
        # Memorial should never be a single point of failure for the card.
        print(f"[exercise-week] memorial failed, using plain card: {e}",
              file=sys.stderr)
        from core.card import build_card
        print(build_card(
            "📊 本周运动", body, source="exercise-week",
            work_receipt="汇总本周日历运动事件与手记记录并完成次数核对",
        ))
    return 0


if __name__ == "__main__":
    sys.exit(main())
