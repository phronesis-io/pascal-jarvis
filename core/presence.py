"""Presence — are owner-visible results reaching their promised surface?

2026-08-07, owner's verdict on the platform: 「飞书里面没有卡片了，jarvis 就
没有存在感」. The 7/24–8/2 cliff (69→1 cards/day, routed to a phone desk that
was never paired) ran ten days with every internal check green: cards were
produced, "delivered", and archived to web surfaces with zero recorded
traffic. That incident required transport receipts, not a permanent quota of
messages, so:

- ``check``: a debt sentinel for selfmon — an owner-visible delivery that
  failed or remains stuck after a real attempt is a red flag. Raw message
  volume is not health: a quiet day with nothing worth saying is success.
- ``morning-digest``: ledger-only cards batched into the morning anchor (the
  style contract's 「攒批≥5条晨匣提一行」clause, PR #36 — never implemented
  until now), instead of silently rotting in an archive nobody opens.

Since REQ-119 (2026-08-11) Lark is the only delivery surface: a card either
has a successful receipt in the unified delivery database or stayed
ledger-only (ambient exhaust, ``delivery_status=ledger_only``). The digest
line is the batched surface for the latter.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

from core.textutil import middle_ellipsize

JARVIS_DIR = Path(os.environ.get(
    "JARVIS_DIR", Path(__file__).resolve().parent.parent))

DIGEST_MIN = 5      # 攒批≥5条晨匣提一行 — the signed style contract's number
DIGEST_TITLES = 3
DIGEST_TITLE_CHARS = 24
DELIVERY_DEBT_GRACE_MINUTES = 15

# Stable text on purpose: selfmon dedups alerts by line content, so a
# changing count would re-page every 4h for one persisting condition.
DELIVERY_DEBT_WARNING = (
    "⚠️ 有本该送达的消息在真实投递后仍失败或卡住——检查投递债务")

def _ledger_path() -> Path:
    return JARVIS_DIR / "memorials.jsonl"


def _events() -> list[dict]:
    from core.jsonl import read_jsonl
    return read_jsonl(_ledger_path())


def _in_window(ev: dict, cutoff: datetime) -> bool:
    try:
        return datetime.strptime(
            str(ev.get("ts", "")), "%Y-%m-%d %H:%M") >= cutoff
    except ValueError:
        return False


def _delivery_sent_count(hours: float, now: datetime) -> int | None:
    """Authoritative count of delivered Lark cards, or None pre-migration."""
    from core.runtime_paths import database_path
    from core.timeutil import now_local

    path = database_path(JARVIS_DIR)
    if not path.exists():
        return None
    if now.tzinfo is None:
        now = now.replace(tzinfo=now_local().tzinfo)
    cutoff_epoch = (now - timedelta(hours=hours)).timestamp()
    try:
        uri = path.resolve().as_uri() + "?mode=ro"
        with sqlite3.connect(uri, uri=True) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(
                "SELECT source FROM delivery_envelopes "
                "WHERE kind='card' AND route_channel='lark' "
                "AND delivered_epoch>=? "
                "AND state IN ('delivered','read','acted')",
                (cutoff_epoch,),
            ).fetchall()
    except (OSError, sqlite3.Error):
        return None

    count = 0
    for row in rows:
        if row["source"] == "deploy-smoke":
            continue
        count += 1
    return count


def sent_count(hours: float = 24, now: datetime | None = None) -> int:
    """Cards that verifiably reached Feishu.

    The unified delivery DB is authoritative because it records the transport
    receipt for every producer. The memorial ``sent`` event remains a bounded
    migration fallback for fresh installs and pre-delivery databases.
    """
    moment = now or datetime.now()
    delivered = _delivery_sent_count(hours, moment)
    if delivered is not None:
        return delivered
    cutoff = moment - timedelta(hours=hours)
    return sum(1 for e in _events()
               if e.get("ev") == "sent" and _in_window(e, cutoff))


def delivery_debt_count(hours: float = 24,
                        now: datetime | None = None) -> int:
    """Owner-visible envelopes that failed or stalled after a real attempt."""
    from core.runtime_paths import database_path
    from core.timeutil import now_local

    path = database_path(JARVIS_DIR)
    if not path.exists():
        return 0
    moment = now or datetime.now()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=now_local().tzinfo)
    cutoff = (moment - timedelta(hours=hours)).timestamp()
    overdue = (moment - timedelta(
        minutes=DELIVERY_DEBT_GRACE_MINUTES)).timestamp()
    try:
        uri = path.resolve().as_uri() + "?mode=ro"
        with sqlite3.connect(uri, uri=True) as db:
            row = db.execute(
                "SELECT COUNT(*) FROM delivery_envelopes "
                "WHERE kind='card' AND route_channel='lark' "
                "AND source!='deploy-smoke' AND created_epoch>=? AND ("
                "state='failed' OR (state IN ('queued','attempting') "
                "AND attempts>0 AND updated_epoch<=? AND last_error!=''))",
                (cutoff, overdue),
            ).fetchone()
    except (OSError, sqlite3.Error):
        return 0
    return int(row[0] if row else 0)


def ledger_only(hours: float = 24, now: datetime | None = None) -> list[dict]:
    """Cards created in the window whose only reach is this digest.

    Counts ONLY rows whose delivery event says ``ledger_only``: ambient
    exhaust that never enters the pipeline (REQ-119), plus — since
    2026-08-20 — cards the daily attention cap dropped, which are owed a
    mention rather than obsolete (core.memorial.suppressed_delivery_status).
    Inferring from "created but no ``sent`` event" would also sweep in
    Lark-routed cards still sitting in the quiet-hours queue and cards on
    the retry path, double-exposing them once the queue flushes
    (adversarial review, 2026-08-11).
    """
    events = _events()
    ledger_ids = {
        str(e.get("id")) for e in events
        if e.get("ev") == "delivery"
        and str(e.get("status", "")) == "ledger_only"
    }
    cutoff = (now or datetime.now()) - timedelta(hours=hours)
    return [e for e in events
            if e.get("ev") == "create" and _in_window(e, cutoff)
            and str(e.get("id")) in ledger_ids]


def host_asleep_seconds(hours: float = 24,
                        now: datetime | None = None) -> float:
    """Recorded host sleep inside the window, in seconds.

    Reads the `sleep_gap` events heartbeat_loop emits. Those events became
    trustworthy on 2026-08-19: until then the loop bracketed only its own 10s
    sleep, so it logged 0.7h of the 39.4h that daemon.py measured. Anything
    reading this before that fix would have under-read absence by ~50x.
    """
    moment = now or datetime.now()
    cutoff = moment - timedelta(hours=hours)
    try:
        from core.sched_events import query
        rows = query(JARVIS_DIR, since=cutoff.strftime("%Y-%m-%d %H:%M:%S"),
                     event="sleep_gap")
    except Exception:
        return 0.0
    total = 0.0
    for row in rows:
        try:
            total += float(row.get("duration_s") or 0)
        except (TypeError, ValueError):
            continue
    return total


def check(now: datetime | None = None) -> str:
    """Selfmon sentinel line, or "" when no promised delivery is owed."""
    return DELIVERY_DEBT_WARNING if delivery_debt_count(now=now) else ""


def last_call_decisions(now: datetime | None = None) -> list[dict]:
    """Capped decisions whose deadline expires before the NEXT anchor.

    ``ledger_only(24)`` gives a dropped decision its creation-morning
    mention — but the decision deadline (48h escrow) is still more than a
    day away at that moment, so the morning it actually expires has no
    surface at all: the 2026-08-21 13:41 broadcast draft was named in the
    8/22 anchor with the deadline 29h out, had rolled past the 24h window
    by the 8/23 anchor, and lapsed at 13:41 that afternoon with nothing
    saying so. This is the bounded follow-up: a still-pending, never-
    delivered decision earns exactly ONE last call, on its final morning.
    The two mentions are disjoint by construction — a deadline inside the
    next 24h means the row was created more than 24h ago, outside the
    ``ledger_only`` window.
    """
    from core.memorial import ATTENTION_DECISION, ESCROW_DEADLINE_H
    moment = now or datetime.now()
    horizon = moment + timedelta(hours=24)
    deadline_h = ESCROW_DEADLINE_H[ATTENTION_DECISION]
    events = _events()
    # decide/lapse/resolve all end the pending state; a row the owner already
    # answered (or that the escrow sweep filed as 留中) owes him nothing.
    closed = {str(e.get("id")) for e in events
              if e.get("ev") in ("decide", "lapse", "resolve")}
    ledger_ids = {str(e.get("id")) for e in events
                  if e.get("ev") == "delivery"
                  and str(e.get("status", "")) == "ledger_only"}
    out: list[dict] = []
    for e in events:
        if e.get("ev") != "create":
            continue
        if str(e.get("attention", "")) != ATTENTION_DECISION:
            continue
        mid = str(e.get("id"))
        if mid in closed or mid not in ledger_ids:
            continue
        try:
            created = datetime.strptime(
                str(e.get("ts", "")), "%Y-%m-%d %H:%M")
        except ValueError:
            continue
        deadline = created + timedelta(hours=deadline_h)
        # Already past → the overdue docket's business, not a fake last call.
        if moment <= deadline < horizon:
            out.append({"title": str(e.get("title", "")).strip(),
                        "deadline": deadline})
    return out


def _summarize_titles(rows: list[dict]) -> str:
    """Show newest distinct titles and collapse exact repeats into counts."""
    counts: dict[str, int] = {}
    ordered_newest: list[str] = []
    for row in reversed(rows):
        title = str(row.get("title", "")).strip() or "无题"
        counts[title] = counts.get(title, 0) + 1
        if counts[title] == 1:
            ordered_newest.append(title)
    rendered = []
    for title in ordered_newest[:DIGEST_TITLES]:
        label = middle_ellipsize(title, DIGEST_TITLE_CHARS)
        count = counts[title]
        rendered.append(f"{label} ×{count}" if count > 1 else label)
    return "／".join(rendered)


def morning_digest_line(now: datetime | None = None) -> str:
    """One deterministic line for the morning anchor, or "".

    Data formatting is code's job — the anchor's LLM contract stays ONE
    hand-written line; this rides below it.
    """
    moment = now or datetime.now()
    rows = ledger_only(24, now=moment)
    # 攒批≥5 is the style contract's threshold for 周知. A card that was going
    # to ask the owner for a decision and lost its slot to the daily cap is not
    # 周知 — holding it back for lacking four companions would be the same
    # silent drop this line exists to end, so any decision-class row in the
    # bin publishes the line on its own.
    decisions = [r for r in rows if str(r.get("attention", "")) == "decision"]

    dying = last_call_decisions(now=moment)
    last_call = ""
    if dying:
        def _when(deadline: datetime) -> str:
            # At anchor time (~09:00) a next-24h deadline on tomorrow's date
            # is always small hours, but the CLI can run any time of day —
            # 明天 is never wrong, 明早 13:48 would be.
            day = "今天" if deadline.date() == moment.date() else "明天"
            return f"{day} {deadline.strftime('%H:%M')}"
        parts = "、".join(
            f"「{middle_ellipsize(d['title'] or '无题', DIGEST_TITLE_CHARS)}」"
            f"{_when(d['deadline'])}"
            for d in dying[:DIGEST_TITLES])
        last_call = f"⏳ {parts} 到期"

    if len(rows) < DIGEST_MIN and not decisions:
        if last_call:
            # A last call publishes alone for the same reason a dropped
            # decision does: it asked for a judgment and never arrived.
            return f"{last_call}——先前被日额度挤掉，一直没送到过你手上"
        return ""
    titles = _summarize_titles(decisions or rows)
    if decisions:
        line = (f"📥 另有 {len(rows)} 条只进了归档，其中 {len(decisions)} 条"
                f"本来是要你拿主意的：{titles}")
    else:
        line = f"📥 另有 {len(rows)} 条周知只进了归档，扫一眼标题：{titles}"
    return f"{line}；{last_call}" if last_call else line


def main(argv: list[str]) -> int:
    cmd = argv[0] if argv else "check"
    if cmd == "check":
        line = check()
        if line:
            print(line)
        return 0
    if cmd == "morning-digest":
        line = morning_digest_line()
        if line:
            print(line)
        return 0
    print("usage: python3 -m core.presence [check|morning-digest]",
          file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
