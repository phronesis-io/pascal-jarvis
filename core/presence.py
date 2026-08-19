"""Presence — is Jarvis actually showing up in Feishu?

2026-08-07, owner's verdict on the platform: 「飞书里面没有卡片了，jarvis 就
没有存在感」. The 7/24–8/2 cliff (69→1 cards/day, routed to a phone desk that
was never paired) ran ten days with every internal check green: cards were
produced, "delivered", and archived to web surfaces with zero recorded
traffic. Feishu arrival volume IS the product's pulse, so:

- ``check``: a floor sentinel for selfmon — fewer than SENT_FLOOR_24H cards
  actually reaching Feishu in 24h is a red flag regardless of how healthy
  the pipeline claims to be.
- ``morning-digest``: ledger-only cards batched into the morning anchor (the
  style contract's 「攒批≥5条晨匣提一行」clause, PR #36 — never implemented
  until now), instead of silently rotting in an archive nobody opens.
The other reason the pulse goes flat is that the host was asleep. This module
only has to stop blaming the delivery chain for it (``check`` below); saying
it out loud is ``core.absence``'s receipt, sent on the wake itself.

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

JARVIS_DIR = Path(os.environ.get(
    "JARVIS_DIR", Path(__file__).resolve().parent.parent))

# Healthy days run 18-25 sent cards; the cliff ran 1-7. Five splits them
# with margin and survives quiet weekends (observed floor ~18 even then).
SENT_FLOOR_24H = 5
DIGEST_MIN = 5      # 攒批≥5条晨匣提一行 — the signed style contract's number
DIGEST_TITLES = 3

# Stable text on purpose: selfmon dedups alerts by line content, so a
# changing count would re-page every 4h for one persisting condition.
FLOOR_WARNING = ("⚠️ 过去24h真正到飞书的卡片不足5张——产出可能正被路由进"
                 "没人看的归档，Jarvis 正在消失。先查投递链路（7/24悬崖同款）")

# A shut lid produces the same reading as a broken pipe, and on 2026-08-18/19
# it did: the host slept ~39h, card output fell 76/day → 2, and this sentinel
# would have sent whoever read it to audit the delivery chain — which was
# fine. Volume is only evidence about routing when the machine was awake to
# route anything.
ABSENCE_HOURS = 3.0
ABSENCE_WARNING = ("⚠️ 过去24h这台机器有大段时间是睡着的（合盖或断电），"
                   "卡片少是因为 Jarvis 没醒着，不是投递坏了——投递链路不用查。")


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


def ledger_only(hours: float = 24, now: datetime | None = None) -> list[dict]:
    """Cards created in the window that are explicitly ledger-only.

    Counts ONLY rows whose delivery event says ``ledger_only`` (ambient
    exhaust, REQ-119) — the rows whose one reach IS the morning digest.
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
    """Selfmon sentinel line, or "" when presence is healthy.

    A fresh install with neither delivery DB nor memorial activity is not an
    outage. Once the delivery DB exists, it is authoritative even when the
    legacy memorial ledger has never been created.
    """
    moment = now or datetime.now()
    delivered = _delivery_sent_count(24, moment)
    if delivered is None and not _ledger_path().exists():
        return ""
    count = delivered if delivered is not None else sent_count(24, now=moment)
    if count < SENT_FLOOR_24H:
        if host_asleep_seconds(24, now=moment) >= ABSENCE_HOURS * 3600:
            return ABSENCE_WARNING
        return FLOOR_WARNING
    return ""


def morning_digest_line(now: datetime | None = None) -> str:
    """One deterministic line for the morning anchor, or "".

    Data formatting is code's job — the anchor's LLM contract stays ONE
    hand-written line; this rides below it.
    """
    rows = ledger_only(24, now=now)
    if len(rows) < DIGEST_MIN:
        return ""
    titles = "／".join(
        str(r.get("title", "")).strip()[:20] or "无题"
        for r in rows[-DIGEST_TITLES:])
    return f"📥 另有 {len(rows)} 条周知只进了归档，扫一眼标题：{titles}"


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
