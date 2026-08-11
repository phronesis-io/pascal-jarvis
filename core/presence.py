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

Since REQ-119 (2026-08-11) Lark is the only delivery surface: a card either
reached Feishu (ledger ``sent`` event) or stayed ledger-only (ambient
exhaust, ``delivery_status=ledger_only``). The digest line is the batched
surface for the latter.
"""

from __future__ import annotations

import os
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


def sent_count(hours: float = 24, now: datetime | None = None) -> int:
    """Cards that verifiably reached Feishu (ledger ``sent`` events)."""
    cutoff = (now or datetime.now()) - timedelta(hours=hours)
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


def check(now: datetime | None = None) -> str:
    """Selfmon sentinel line, or "" when presence is healthy.

    A missing ledger means a fresh install with no product activity yet —
    that is not an outage, so no page.
    """
    if not _ledger_path().exists():
        return ""
    if sent_count(24, now=now) < SENT_FLOOR_24H:
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
