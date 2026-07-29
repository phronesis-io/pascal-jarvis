"""Per-source attention ROI — does a source's interrupting lane earn itself?

`WEB_FIRST_SOURCES` in core.memorial is a hand-edited set: someone ran an audit,
noticed a source was noisy, and demoted it in code. Nothing measured whether the
demotion was right, nothing noticed the next noisy source, and nothing ever
promoted anything back. This module closes that loop with the ledger's own
evidence.

Measured over a trailing window, per (source, attention class):

    n        cards created
    engaged  cards that got a 批红 or a 「聊聊这个」
    rate     engaged / n

The policy is deliberately narrow, because the ledger says the decision lane is
mostly healthy and a blunt governor would do more harm than good:

  - **Demote decision → notice** when a source has asked at least
    MIN_SAMPLE times and been answered under DEMOTE_RATE. It keeps its durable
    Item/Signals surface; it just stops claiming a decision it does not get.
  - **Promote back** when a demoted source's cards start earning attention
    again. Demotion is a hypothesis, not a sentence.
  - **Never demote below notice.** A notice already interrupts nobody, so
    "demoting" it further would only mean hiding it, and quiet is not
    invisible (PRODUCT.md §12). Low-ROI notice sources are *reported* instead.
  - **Never touch protected sources.** For these, one miss costs more than a
    hundred ignored cards.

Every change is announced on a card. A governor that silently rewires where
Pascal's attention goes would be exactly the kind of invisible authority this
codebase refuses everywhere else.
"""

from __future__ import annotations

import json
import sys
import time
from datetime import timedelta
from pathlib import Path

from core.timeutil import now_local, now_local_str

ROOT = Path(__file__).resolve().parent.parent

WINDOW_DAYS = 14
MIN_SAMPLE = 8          # below this, a rate is noise, not evidence
DEMOTE_RATE = 0.25
PROMOTE_RATE = 0.50
CACHE_TTL_S = 300

# Sources whose lane is never governed by engagement. Missing one of these is
# expensive in a way that ignoring it is not: a stale calendar conflict, an
# unnoticed outage, a dropped commitment, a delegation waiting on a human.
PROTECTED_SOURCES = frozenset({
    "calendar-sync",
    "selfmon",
    "self-diagnostic",
    "delegation",
    "intentions",
    "intention-check",
    "mail",
    "checkin",
})

_sys_path_added = False
# None means "never loaded". A sentinel, not a timestamp: invalidation used to
# set _cache_at = 0.0 and rely on `monotonic() - 0 > TTL` to force a reload,
# which is only true when the clock's arbitrary origin is already past the TTL.
# On a host up for days that holds; in a fresh container (CI) or for the first
# five minutes after a reboot, monotonic() < TTL and the empty cache read as
# fresh — the governor would silently apply no overrides at all.
_cache: dict[str, str] | None = None
_cache_at = 0.0


def _get_db():
    global _sys_path_added
    if not _sys_path_added:
        sys.path.insert(0, str(ROOT))
        _sys_path_added = True
    from dashboard.db import get_db
    return get_db()


_initialized = False


def _init() -> None:
    global _initialized
    if _initialized:
        return
    db = _get_db()
    db.executescript("""
        CREATE TABLE IF NOT EXISTS attention_overrides (
            source TEXT PRIMARY KEY,
            decided_class TEXT NOT NULL,
            reason TEXT NOT NULL DEFAULT '',
            sample_n INTEGER NOT NULL DEFAULT 0,
            engaged_rate REAL NOT NULL DEFAULT 0,
            since TEXT NOT NULL
        );
    """)
    db.commit()
    _initialized = True


# ── measurement ──────────────────────────────────────────────────────────────


def _engaged(state: dict) -> bool:
    """A card earned its lane if Pascal acted on it at all.

    留中 (lapsed) is the exact opposite of engagement: the escrow sweep filed
    it BECAUSE nobody ever answered. Letting a non-pending status imply "acted"
    would have scored every auto-archived card as engaged, pushing the noisiest
    sources toward a ~100% rate and permanently blocking the demotion this
    module exists to perform.
    """
    from core.memorial import STATUS_LAPSED

    status = str(state.get("status", ""))
    if status == STATUS_LAPSED:
        return False
    if status not in ("", "pending"):
        return True
    return bool(state.get("chat_started_at") or state.get("decided_ts"))


def compute_stats(window_days: int = WINDOW_DAYS) -> dict[tuple[str, str], dict]:
    """Per (source, attention class) engagement over the trailing window."""
    from core import memorial

    cutoff = (now_local() - timedelta(days=window_days)).strftime("%Y-%m-%d %H:%M")
    stats: dict[tuple[str, str], dict] = {}
    for state in memorial.list_memorials():
        if str(state.get("ts", "")) < cutoff:
            continue
        source = str(state.get("source", ""))
        if not source:
            continue
        # The lane the card actually occupied. Legacy rows predate the field,
        # so they fall back to the *natural* class — never _default_attention,
        # which now applies this module's own overrides and would feed a
        # demotion back in as evidence for itself.
        attention = str(state.get("attention", "")) or memorial.natural_attention(
            source, list(state.get("options") or []),
            list(state.get("extra_buttons") or []))
        row = stats.setdefault((source, attention), {"n": 0, "engaged": 0})
        row["n"] += 1
        if _engaged(state):
            row["engaged"] += 1
    for row in stats.values():
        row["rate"] = row["engaged"] / row["n"] if row["n"] else 0.0
    return stats


# ── policy ───────────────────────────────────────────────────────────────────


def evaluate(stats: dict[tuple[str, str], dict] | None = None) -> dict:
    """Decide the override set from evidence. Pure — writes nothing.

    Returns ``{"demote": {...}, "promote": [...], "noisy_notices": [...]}``.
    """
    from core.memorial import ATTENTION_DECISION, ATTENTION_NOTICE

    stats = compute_stats() if stats is None else stats
    _init()
    current = {row["source"]: dict(row) for row in _get_db().execute(
        "SELECT * FROM attention_overrides").fetchall()}

    demote: dict[str, dict] = {}
    promote: list[dict] = []
    noisy_notices: list[dict] = []

    for (source, attention), row in stats.items():
        if source in PROTECTED_SOURCES or row["n"] < MIN_SAMPLE:
            continue
        if attention == ATTENTION_DECISION and row["rate"] < DEMOTE_RATE:
            demote[source] = {
                "source": source, "to": ATTENTION_NOTICE,
                "n": row["n"], "rate": row["rate"],
                "reason": (f"最近 {WINDOW_DAYS} 天问了 {row['n']} 次，"
                           f"只回了 {row['engaged']} 次"),
            }
        elif attention == ATTENTION_NOTICE and row["rate"] < DEMOTE_RATE:
            # Already at the floor. Report, never hide.
            noisy_notices.append({"source": source, "n": row["n"],
                                  "rate": row["rate"]})

    for source, row in current.items():
        # A demoted source now emits notice cards, so its recovery evidence
        # lives in the NOTICE lane. Reading the decision lane instead would
        # find nothing (it was demoted) and promote it back every cycle —
        # a flip-flop that would re-interrupt Pascal every six hours.
        stat = stats.get((source, ATTENTION_NOTICE))
        if stat is None or stat["n"] < MIN_SAMPLE:
            continue          # not enough evidence either way; hold as-is
        if stat["rate"] >= PROMOTE_RATE:
            promote.append({"source": source, "was": row["decided_class"],
                            "n": stat["n"], "rate": stat["rate"]})

    return {"demote": demote, "promote": promote,
            "noisy_notices": sorted(noisy_notices, key=lambda x: x["rate"])}


def apply(decision: dict) -> list[str]:
    """Persist the override set. Returns human-readable change lines."""
    _init()
    db = _get_db()
    changes: list[str] = []
    existing = {row["source"] for row in db.execute(
        "SELECT source FROM attention_overrides").fetchall()}

    for source, item in decision["demote"].items():
        if source in existing:
            continue          # already held down; not a change
        db.execute(
            "INSERT OR REPLACE INTO attention_overrides"
            " (source, decided_class, reason, sample_n, engaged_rate, since)"
            " VALUES (?,?,?,?,?,?)",
            (source, item["to"], item["reason"], item["n"], item["rate"],
             now_local_str()))
        changes.append(f"「{source}」不再占用决策位（{item['reason']}），"
                       f"以后只进事项/信号，不会消失")
    for item in decision["promote"]:
        db.execute("DELETE FROM attention_overrides WHERE source = ?",
                   (item["source"],))
        changes.append(f"「{item['source']}」恢复决策位（最近又开始被回应了）")
    db.commit()
    _invalidate()
    return changes


def _invalidate() -> None:
    global _cache
    _cache = None


def overrides() -> dict[str, str]:
    """Current override map, cached briefly — this is on the delivery path."""
    global _cache, _cache_at
    now = time.monotonic()
    if _cache is not None and (now - _cache_at) < CACHE_TTL_S:
        return _cache
    try:
        _init()
        _cache = {row["source"]: row["decided_class"] for row in _get_db().execute(
            "SELECT source, decided_class FROM attention_overrides").fetchall()}
    except Exception:
        # A governor that cannot read its own table must not break delivery.
        _cache = {}
    _cache_at = now
    return _cache


def class_for(source: str, natural: str) -> str:
    """Apply any override to a card's natural attention class.

    Only ever quiets a decision into a notice. It cannot raise a class, cannot
    touch an alert, and cannot silence anything — a notice still has its
    durable Item and Signals surface.
    """
    from core.memorial import ATTENTION_DECISION, ATTENTION_NOTICE

    if natural != ATTENTION_DECISION:
        return natural
    if str(source or "") in PROTECTED_SOURCES:
        return natural
    if overrides().get(str(source or "")) == ATTENTION_NOTICE:
        return ATTENTION_NOTICE
    return natural


def refresh(announce: bool = True) -> list[str]:
    """Recompute, persist, and announce. Called from Tier-0 maintenance."""
    changes = apply(evaluate())
    if changes and announce:
        try:
            from core import memorial
            memorial.create(
                source="attention-roi",
                title="调整了几个来源的打扰级别",
                body=("按最近两周你实际回应的情况自动调整：\n\n"
                      + "\n".join(f"- {c}" for c in changes)
                      + "\n\n改错了就告诉我，我改回去。"),
                preset="fyi",
                dedup_key=f"attention-roi:{now_local().strftime('%Y-%m-%d')}",
            )
        except Exception as exc:
            print(f"[attention-roi] 变更卡发送失败: {exc}", file=sys.stderr)
    return changes


def main(argv: list[str] | None = None) -> int:
    import argparse

    p = argparse.ArgumentParser(prog="core.attention_roi")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("report", help="看各来源的注意力 ROI（不改任何东西）")
    r = sub.add_parser("refresh", help="按证据重算并生效")
    r.add_argument("--quiet", action="store_true", help="不发变更卡")
    args = p.parse_args(argv)

    if args.cmd == "report":
        stats = compute_stats()
        decision = evaluate(stats)
        print(f"最近 {WINDOW_DAYS} 天，按 来源 × 打扰级别：\n")
        for (source, attention), row in sorted(
                stats.items(), key=lambda x: (-x[1]["n"])):
            if row["n"] < 3:
                continue
            flag = ""
            if source in decision["demote"]:
                flag = "  ← 建议降级"
            elif source in PROTECTED_SOURCES:
                flag = "  (受保护)"
            print(f"  {attention:9} {source:24} n={row['n']:3} "
                  f"回应={row['engaged']:3} {row['rate'] * 100:3.0f}%{flag}")
        if decision["noisy_notices"]:
            print("\n已经在最低档、但基本没人看的来源（只报告，不动）：")
            for item in decision["noisy_notices"]:
                print(f"  {item['source']:24} n={item['n']:3} "
                      f"{item['rate'] * 100:3.0f}%")
        current = overrides()
        print(f"\n当前生效的降级：{json.dumps(current, ensure_ascii=False) or '无'}")
    else:
        changes = refresh(announce=not args.quiet)
        print("\n".join(changes) if changes else "没有需要调整的。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
