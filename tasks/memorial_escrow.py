#!/usr/bin/env python3
"""缴回制度 — sweep pending memorials to a terminal state, once a day docket.

Before this task nothing ever swept the memorial ledger: a card sent once and
never tapped scrolled out of Lark and stayed `pending` forever. On 7/29 that
was 314 of 600 memorials, 110 older than a week, 47 of them decision-class —
real asks lost in a pile indistinguishable from noise.

Two outcomes, deliberately different:
  留中 (lapse)  alerts/notices past their deadline, and decisions so old nobody
                will ever answer them, are archived. Terminal, silent, counted.
  docket        while any delivered decision is past its deadline but still
                answerable, ONE morning card names only those open asks. An
                unsent card cannot create an obligation for the owner, and
                notices/alerts keep their own lifecycle. Never re-pushed
                individually: that is the card storm of 7/22.

Tier-0 by design — pure arithmetic over timestamps. No model call decides
whether the owner answered something.
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

    # The lapse loop above just moved rows; the docket must read the ledger as
    # it stands NOW. escrow_docket applies the stricter human-obligation
    # predicate: delivered unresolved decisions only.
    fresh = memorial.list_memorials()
    title, body = memorial.escrow_docket(fresh, now=now)
    # No 去-somewhere button: the web desk is retired (REQ-120) and a URL
    # button pointing at it would be a dead end by the no-dead-ends rule.
    mid, accepted = memorial.create(
        source=memorial.ESCROW_DIGEST_SOURCE,
        title=title,
        body=body,
        work_receipt="完成待批台账折叠、过期清理和重复事项核对",
        owner_need="decision_batch",
        why_now="晨间批次窗口到了，多个已送达判断可以一次处理",
        owner_action="一次处理这些判断，或选择先都放着",
        silence_cost="不提示会让多个已送达判断在待批状态继续积压",
        # 「先都放着」 is the escape hatch that keeps this from nagging forever:
        # the whole docket can be declined in one tap. Plain wording, not
        # court jargon (owner 2026-08-11: 「以后别说黑话了」).
        options=[
            {"key": "lapse_all", "label": "先都放着",
             "action": {"type": "memorial_lapse_all", "params": {}}},
        ],
        attention=memorial.ATTENTION_DECISION,
        dedup_key=f"escrow-docket-{today}",
        send=send,
    )
    summary["docket_sent"] = bool(accepted)
    # create() returns the existing memorial id when its dedup gate rejects a
    # duplicate. Only report an id for work this sweep actually created.
    summary["docket_id"] = mid if accepted else ""
    if accepted:
        # Today's docket supersedes yesterday's: an unanswered docket is not
        # an ask anybody will still answer once a fresher one exists, and it
        # must never sit pending forever (it is excluded from every sweep and
        # count by design — see memorial.counts_in_ledger).
        for st in fresh:
            if (str(st.get("source", "")) == memorial.ESCROW_DIGEST_SOURCE
                    and st.get("status") == "pending"
                    and st.get("id") != mid):
                memorial.resolve(st["id"], "已被今天的新一张替代",
                                 "superseded_by_next_docket")
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
