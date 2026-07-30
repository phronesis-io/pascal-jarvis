#!/usr/bin/env python3
"""缴回制度 — sweep pending memorials to a terminal state, once a day docket.

Before this task nothing ever swept the memorial ledger: a card sent once and
never tapped scrolled out of Lark and stayed `pending` forever. On 7/29 that
was 314 of 600 memorials, 110 older than a week, 47 of them decision-class —
real asks lost in a pile indistinguishable from noise.

Two outcomes, deliberately different:
  留中 (lapse)  alerts/notices past their deadline, and decisions so old nobody
                will ever answer them, are archived. Terminal, silent, counted.
  docket        decisions past their deadline but still answerable are grouped
                by source into ONE morning card. Never re-pushed individually:
                that is the card storm of 7/22.

Tier-0 by design — pure arithmetic over timestamps. No model call decides
whether Pascal answered something.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import memorial  # noqa: E402
from core.timeutil import now_local  # noqa: E402


def _already_sent_today(states: list[dict], today: str) -> bool:
    """One docket a day. The dedup_key carries the date, but the ledger is the
    authority — a same-day restart must not mint a second docket."""
    return any(
        str(s.get("source", "")) == memorial.ESCROW_DIGEST_SOURCE
        and str(s.get("ts", ""))[:10] == today
        for s in states
    )


def run(now=None, send: bool = True) -> dict:
    """Sweep once. Returns a summary dict (also the test seam)."""
    now = now or now_local()
    states = memorial.list_memorials()
    scan = memorial.escrow_scan(now=now, states=states)

    lapsed = 0
    for state, reason in scan["lapse"]:
        if memorial.lapse(state["id"], reason):
            lapsed += 1

    overdue = scan["overdue"]
    summary = {
        "lapsed": lapsed,
        "overdue": len(overdue),
        "docket_sent": False,
        "docket_id": "",
    }
    if not overdue:
        return summary

    # 御门听政: the docket is a morning ritual, not an interrupt. Outside the
    # window the sweep still runs — only the card waits.
    today = now.strftime("%Y-%m-%d")
    if now.hour not in memorial.ESCROW_DIGEST_HOURS:
        return summary
    if _already_sent_today(states, today):
        return summary

    title, body = memorial.escrow_docket(overdue, now=now)
    # 去看看 must actually GO somewhere. A record-only option would spend the
    # tap, mark the docket 已批 and move the user nowhere — the docket points
    # at the web desk, it is never a second inbox. When no reachable web-desk
    # URL exists the button is OMITTED rather than shipped dead.
    from core.mobile_access import web_desk_url
    desk = web_desk_url("/items")
    extra_buttons = [{"text": "去事项处理", "url": desk}] if desk else []
    mid, accepted = memorial.create(
        source=memorial.ESCROW_DIGEST_SOURCE,
        title=title,
        body=body,
        # 全部留中 is the escape hatch that keeps this from nagging forever:
        # the emperor may decline the whole docket in one tap.
        options=[
            {"key": "lapse_all", "label": "全部留中",
             "action": {"type": "memorial_lapse_all", "params": {}}},
        ],
        extra_buttons=extra_buttons,
        attention=memorial.ATTENTION_DECISION,
        dedup_key=f"escrow-docket-{today}",
        send=send,
    )
    summary["docket_sent"] = bool(accepted)
    # create() returns the existing memorial id when its dedup gate rejects a
    # duplicate. Only report an id for work this sweep actually created.
    summary["docket_id"] = mid if accepted else ""
    return summary


def main() -> int:
    summary = run()
    # Tier-0 pre-scripts print the user-facing product. The sweep has none:
    # 留中 is bookkeeping and the docket delivers itself as a card. Empty
    # stdout is the correct, silent outcome.
    print(
        f"[escrow] lapsed={summary['lapsed']} overdue={summary['overdue']} "
        f"docket={'sent' if summary['docket_sent'] else 'no'}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
