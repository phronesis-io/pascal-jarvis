"""Skip digest (REQ-78 batch 1 + batch 4) — surface silently-skipped occurrences.

Evidence (6/30 + 7/1): 8 cron occurrences were retired by
`_skip_stale_cron_occurrence` during heartbeat stalls; the
`intent_occurrence_skipped` events had NO consumer anywhere, so a credit-card
bill reminder and a Tushare token reminder vanished without a
trace until Pascal noticed himself.

This module is the consumer. It scans sched_events.jsonl for skip-class
events in the last 24h and rides the existing intent breach queue (the
intention-check apology card, BREACH_MAX_SHOWS=1 anti-nag), split by the
skipped intent's `category` (behavioral_rules §5 — the existing taxonomy):

  - category='hard' (① 硬约束 — 账单/续费/票; the credit-card-bill incident class,
    which carried tags=[] so a tags whitelist would have missed it) gets
    PER-ITEM backfill: one breach entry per occurrence with the original
    prompt riding along (batch 4, armed 7/9 after the shadow period).
  - everything else (other categories, or a KNOWN-absent row — deleted
    intent) still folds into ONE aggregate entry ("停摆期间跳过了 N 件事").
  - an event whose category lookup FAILED (db unreadable, not row-absent) is
    deferred untouched and retried next scan — never consumed on a failure
    (F-13; the old fail-open-to-aggregate permanently downgraded bills).

Consumed classes:
  - intent_occurrence_skipped            (all — stale cron occurrences)
  - intent_expired reason=expires_at_lapsed   (batch 2 will start emitting
    these; until then the filter simply matches nothing)
Other intent_expired reasons (storm_class / retries_exhausted / closure_stale)
are NOT consumed here: retries_exhausted already has its own _queue_breach
channel and double-reporting would nag.

Idempotency across watchdog restarts: data/.skip_digest_state.json records
consumed event keys (ts|task|missed-or-reason). Write ordering is the OPPOSITE
trade-off per class:
  - aggregate: consumed keys BEFORE the breach line — a crash in between
    loses the digest rather than duplicating it (宁丢勿重: best-effort
    awareness, duplicates are nagging).
  - hard backfill: breach line(s) BEFORE consumed keys — a crash in between
    re-delivers on the next scan (宁重勿丢: a rare duplicate bill reminder is
    acceptable, a lost one recreates the vanished-bill incident). The entry id
    is deterministic per occurrence, and appends are deduped against ids
    already in the queue, so the redo path doesn't spam.

Everything here is fail-open: any exception degrades to "no digest this
round" plus a stderr note. This module must never be able to break
intentions_pre.sh or self-diagnostic.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).parent.parent

WINDOW_HOURS = 24          # look-back window for skip-class events
SCAN_INTERVAL_S = 600      # queue_digest gate: called on every intentions pre-run
CONSUMED_RETENTION_S = 72 * 3600   # prune consumed keys once they age out of
                                   # any possible 24h window (+ margin)
TS_FMT = "%Y-%m-%d %H:%M:%S"       # sched_events timestamp format

# Intent categories that get per-item backfill instead of the aggregate line.
# Only ① 硬约束 — the other surfaced classes (context/external) are prep/follow
# -up whose moment already passed, and ③ healing must never be chased at all.
BACKFILL_CATEGORIES = ("hard",)

# Sentinel returned by _intent_row when the LOOKUP ITSELF failed (db
# unreadable / WAL recovery error / disk error) — as opposed to None, which
# means "row absent" and is a real answer. A failed lookup must NOT consume
# the event (F-13): downgrading a hard bill to the aggregate on a transient
# sqlite error was permanent, while the aggregate copy promised 会单独补发.
# Events with a failed lookup are deferred untouched; the next scan (10 min)
# retries classification.
_LOOKUP_FAILED = object()


def _state_file(jarvis_dir: Path) -> Path:
    return Path(jarvis_dir) / "data" / ".skip_digest_state.json"


def _breach_queue(jarvis_dir: Path) -> Path:
    # Same file core.intentions owns (BREACH_QUEUE) — we only ever APPEND a
    # line in the exact _queue_breach shape, so peek_breaches /
    # mark_breaches_shown treat the digest like any other breach entry.
    return Path(jarvis_dir) / "data" / ".intent_breach_queue.jsonl"


def _is_skip_event(e: dict) -> bool:
    """True for the two consumed classes; everything else is not ours."""
    ev = e.get("event", "")
    if ev == "intent_occurrence_skipped":
        return True
    return ev == "intent_expired" and e.get("reason") == "expires_at_lapsed"


def _event_key(e: dict) -> str:
    return f"{e.get('ts', '')}|{e.get('task', '')}|{e.get('missed') or e.get('reason') or ''}"


def _load_state(path: Path) -> dict:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(state, dict):
            return state
    except Exception:
        pass
    return {}


def _save_state(path: Path, state: dict) -> None:
    """Atomic tmp+rename — a watchdog restart mid-write must not corrupt."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _recent_skip_events(jarvis_dir: Path) -> list[dict]:
    from core.sched_events import query
    since = (datetime.now() - timedelta(hours=WINDOW_HOURS)).strftime(TS_FMT)
    return [e for e in query(jarvis_dir, since=since) if _is_skip_event(e)]


def _intent_row(jarvis_dir: Path, intent_id: str):
    """Read-only category/prompt lookup for one intent.

    Returns the row dict, or None when the answer is KNOWN and negative
    (no id, no db at all, or the row is absent — a deleted intent), or
    _LOOKUP_FAILED when the lookup ITSELF failed (WAL read-only recovery
    error after an unclean shutdown, corrupted db, disk error). The caller
    treats None as "degrade to the aggregate digest and consume" but
    _LOOKUP_FAILED as "defer — retry next scan" (F-13): a transient sqlite
    error must not permanently downgrade a 硬约束 bill whose digest copy
    promises 会单独补发.

    Deliberately NOT core.intentions.get_intent(): that helper is pinned to
    the repo-ROOT db and runs the table-creation path on first use, while
    this module is parameterized by jarvis_dir and must never write.
    Never raises.
    """
    db_path = Path(jarvis_dir) / "data" / "jarvis.db"
    if not intent_id or not db_path.exists():
        return None
    try:
        import sqlite3
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        try:
            con.row_factory = sqlite3.Row
            row = con.execute(
                "SELECT name, prompt, category FROM intentions WHERE id = ?",
                (intent_id,)).fetchone()
            return dict(row) if row else None
        finally:
            con.close()
    except Exception:
        return _LOOKUP_FAILED


def collect_unconsumed(jarvis_dir: Path = ROOT, state: dict | None = None) -> list[dict]:
    """Skip-class events from the last 24h not yet folded into a digest."""
    if state is None:
        state = _load_state(_state_file(Path(jarvis_dir)))
    consumed = state.get("consumed", {})
    return [e for e in _recent_skip_events(Path(jarvis_dir))
            if _event_key(e) not in consumed]


def _digest_entry(events: list[dict], now_ts: str) -> dict:
    """One breach line in the exact shape core.intentions._queue_breach writes."""
    lines = []
    for e in events:
        name = e.get("name") or e.get("task") or "(未命名提醒)"
        when = e.get("missed") or e.get("ts") or "?"
        lines.append(f"- {name}（原定 {when}）")
    n = len(events)
    prompt = (
        f"心跳停摆期间有 {n} 件预定的事被跳过，没能按时提出来：\n"
        + "\n".join(lines)
        + "\n以上只汇总告知，不逐条补发（账单、续费这类要紧提醒不在此列，会单独补发）。"
          "如果其中有需要现在处理的，说一声即可。"
    )
    return {
        "id": f"skipdigest_{int(time.time())}",
        "name": f"停摆期间跳过了 {n} 件事",
        "prompt": prompt,
        "purpose": "停摆期间被跳过的定时事项汇总（REQ-78，要紧提醒另行逐条补发，其余只告知）",
        "trigger_time": now_ts,
        "attempt": 0,
        "ts": now_ts,
        "notify_attempts": 0,
    }


def _backfill_id(e: dict) -> str:
    # Deterministic per occurrence — the idempotency key that makes the
    # queue-first (宁重勿丢) ordering safe: a post-crash re-append is deduped
    # by id, and mark_breaches_shown retires same-id lines together anyway.
    h = hashlib.sha1(_event_key(e).encode("utf-8")).hexdigest()[:10]
    return f"skipitem_{e.get('task', '')}_{h}"


def _late_hours(missed: str, now: datetime) -> int | None:
    try:
        dt = datetime.fromisoformat(missed)
        ref = now.astimezone() if dt.tzinfo else now
        return max(0, round((ref - dt).total_seconds() / 3600))
    except Exception:
        return None


def _backfill_entry(e: dict, row: dict, now: datetime, now_ts: str) -> dict:
    """Per-item breach line for one skipped 硬约束 occurrence (batch 4)."""
    name = row.get("name") or e.get("name") or e.get("task") or "(未命名提醒)"
    when = (e.get("missed") or e.get("ts") or "?").replace("T", " ")
    late = _late_hours(e.get("missed") or "", now)
    late_note = f"，迟到了约 {late} 小时" if late else ""
    original = (row.get("prompt") or "").strip()
    prompt = (
        f"「{name}」原定 {when} 提醒，当时系统停摆没能按时发出{late_note}。现在补上：\n"
        + (original or f"请现在处理：{name}")
    )
    return {
        "id": _backfill_id(e),
        "name": f"补发提醒：{name}",
        "prompt": prompt,
        "purpose": "停摆期间被跳过的要紧提醒，逐条补发（REQ-78 batch 4）",
        "trigger_time": when,
        "attempt": 0,
        "ts": now_ts,
        "notify_attempts": 0,
    }


def _queued_ids(queue: Path) -> set[str]:
    """Entry ids already on the breach queue (for the backfill dedupe)."""
    ids: set[str] = set()
    if not queue.exists():
        return ids
    for line in queue.read_text(encoding="utf-8").splitlines():
        try:
            ids.add(json.loads(line).get("id", ""))
        except (json.JSONDecodeError, ValueError):
            continue
    return ids


def queue_digest(jarvis_dir: Path = ROOT, force: bool = False,
                 dry_run: bool = False) -> int:
    """Fold unconsumed skip events into breach-queue entries.

    Returns the number of events consumed (0 = nothing to do / gated / failed).
    Never raises. Write ordering is per class (see module docstring):
    硬约束 per-item lines FIRST (宁重勿丢 — a crash before the consumed-state
    save re-delivers next scan, deduped by deterministic id), consumed state
    SECOND, aggregate digest LAST (宁丢勿重 — a crash loses it instead of
    duplicating it). Events whose category lookup FAILED (db unreadable —
    distinct from "row absent") are deferred untouched (F-13): not queued,
    not consumed; the next scan retries their classification, so a transient
    sqlite error can never permanently downgrade a bill to the aggregate.
    Queue appends hold core.intentions.breach_queue_lock (F-6): unlocked
    appends raced the bot process's clear_breaches rewrite, which could land
    a pre-append snapshot and silently destroy the backfill line.
    """
    try:
        from core.intentions import breach_queue_lock

        jarvis_dir = Path(jarvis_dir)
        state_path = _state_file(jarvis_dir)
        state = _load_state(state_path)

        now = time.time()
        if not force and now - float(state.get("last_scan", 0)) < SCAN_INTERVAL_S:
            return 0

        events = collect_unconsumed(jarvis_dir, state)
        now_dt = datetime.now()
        now_ts = now_dt.strftime("%Y-%m-%dT%H:%M:%S")

        backfill: list[tuple[dict, dict]] = []
        aggregate: list[dict] = []
        deferred = 0
        for e in events:
            row = _intent_row(jarvis_dir, e.get("task", ""))
            if row is _LOOKUP_FAILED:
                deferred += 1     # lookup failed — retry next scan, untouched
                continue
            if row and row.get("category") in BACKFILL_CATEGORIES:
                backfill.append((e, row))
            else:
                aggregate.append(e)
        if deferred:
            print(f"[skip-digest] {deferred} event(s) deferred — "
                  f"category lookup failed, retrying next scan",
                  file=sys.stderr)

        if dry_run:
            for e, row in backfill:
                print(f"[skip-digest] would backfill: {_event_key(e)}")
                print(json.dumps(_backfill_entry(e, row, now_dt, now_ts),
                                 ensure_ascii=False, indent=2))
            for e in aggregate:
                print(f"[skip-digest] would consume: {_event_key(e)}")
            if aggregate:
                print(json.dumps(_digest_entry(aggregate, now_ts),
                                 ensure_ascii=False, indent=2))
            return len(backfill) + len(aggregate)

        queue = _breach_queue(jarvis_dir)

        # 1. 硬约束 backfill — BEFORE the consumed-state save. An append
        #    failure aborts the whole scan (nothing consumed → full retry).
        #    The already-queued read AND the append sit under ONE lock so a
        #    concurrent rewrite can neither destroy the append nor stale the
        #    dedupe read.
        if backfill:
            queue.parent.mkdir(parents=True, exist_ok=True)
            n_appended = 0
            with breach_queue_lock(queue):
                already = _queued_ids(queue)
                with open(queue, "a", encoding="utf-8") as f:
                    for e, row in backfill:
                        entry = _backfill_entry(e, row, now_dt, now_ts)
                        if entry["id"] in already:
                            continue   # redo of a crashed scan — already queued
                        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
                        n_appended += 1
            print(f"[skip-digest] queued {n_appended} per-item backfill(s) "
                  f"({len(backfill) - n_appended} already queued)",
                  file=sys.stderr)

        # 2. Consumed state — ONLY events whose classification succeeded
        #    (backfill + aggregate). Deferred events stay unconsumed.
        consumed = {k: t for k, t in state.get("consumed", {}).items()
                    if now - float(t) < CONSUMED_RETENTION_S}
        for e, _row in backfill:
            consumed[_event_key(e)] = now
        for e in aggregate:
            consumed[_event_key(e)] = now
        state = {"last_scan": now, "consumed": consumed}
        _save_state(state_path, state)

        # 3. Aggregate digest — AFTER the save (宁丢勿重, batch-1 behavior).
        if aggregate:
            queue.parent.mkdir(parents=True, exist_ok=True)
            with breach_queue_lock(queue):
                with open(queue, "a", encoding="utf-8") as f:
                    f.write(json.dumps(_digest_entry(aggregate, now_ts),
                                       ensure_ascii=False) + "\n")
            print(f"[skip-digest] queued digest of {len(aggregate)} skipped item(s)",
                  file=sys.stderr)
        return len(backfill) + len(aggregate)
    except Exception as e:  # noqa: BLE001 — fail-open by contract
        try:
            print(f"[skip-digest] queue_digest failed (fail-open): {e}",
                  file=sys.stderr)
        except Exception:
            pass
        return 0


def diag_line(jarvis_dir: Path = ROOT) -> str:
    """One self-diagnostic line: ⚠️ iff any skip-class event in 24h (REQ-78.2).

    Counts ALL events in the window (consumed or not) — the alert is about
    the stall having happened, not about digest bookkeeping.
    """
    try:
        n = len(_recent_skip_events(Path(jarvis_dir)))
        if n > 0:
            return (f"⚠️ {n} 个定时事项在过去24h被停摆跳过"
                    "（intent_occurrence_skipped / expires_at_lapsed）"
                    "— 汇总告知/逐条补发卡应已排队，请核对确实发出")
        return "✓ 过去24h没有定时事项被停摆跳过"
    except Exception as e:  # noqa: BLE001 — fail-open by contract
        return f"⚠️ skip-digest 检查本身失败：{e}"


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="REQ-78 skip digest")
    ap.add_argument("--dir", default=str(ROOT), help="jarvis dir")
    ap.add_argument("--diag", action="store_true",
                    help="print the self-diagnostic line")
    ap.add_argument("--queue", action="store_true",
                    help="fold unconsumed skips into a breach digest (force)")
    ap.add_argument("--dry-run", action="store_true",
                    help="show what --queue would do without writing")
    args = ap.parse_args(argv)

    if args.diag:
        print(diag_line(Path(args.dir)))
        return 0
    if args.queue or args.dry_run:
        n = queue_digest(Path(args.dir), force=True, dry_run=args.dry_run)
        print(f"[skip-digest] {n} event(s) {'would be ' if args.dry_run else ''}consumed")
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
