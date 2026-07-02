"""Skip digest (REQ-78 batch 1) — surface silently-skipped occurrences.

Evidence (6/30 + 7/1): 8 cron occurrences were retired by
`_skip_stale_cron_occurrence` during heartbeat stalls; the
`intent_occurrence_skipped` events had NO consumer anywhere, so a credit-card
bill reminder (¥12,345.67) and a Tushare token reminder vanished without a
trace until Pascal noticed himself.

This module is the first consumer. It scans sched_events.jsonl for skip-class
events in the last 24h and queues ONE aggregate entry on the existing intent
breach queue ("停摆期间跳过了 N 件事") — riding the intention-check apology
card, which already carries BREACH_MAX_SHOWS=1 anti-nag semantics. Per-item
re-delivery (billing/reminder tags) is batch 4, after this shadow period.

Consumed classes:
  - intent_occurrence_skipped            (all — stale cron occurrences)
  - intent_expired reason=expires_at_lapsed   (batch 2 will start emitting
    these; until then the filter simply matches nothing)
Other intent_expired reasons (storm_class / retries_exhausted / closure_stale)
are NOT consumed here: retries_exhausted already has its own _queue_breach
channel and double-reporting would nag.

Idempotency across watchdog restarts: data/.skip_digest_state.json records
consumed event keys (ts|task|missed-or-reason). Consumed keys are written
BEFORE the breach line is appended — if we crash in between, the items are
lost rather than duplicated (宁丢勿重: the digest is best-effort awareness,
duplicates are nagging).

Everything here is fail-open: any exception degrades to "no digest this
round" plus a stderr note. This module must never be able to break
intentions_pre.sh or self-diagnostic.
"""

from __future__ import annotations

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
        + "\n以上只汇总告知，不逐条补发。如果其中有需要现在处理的，说一声即可。"
    )
    return {
        "id": f"skipdigest_{int(time.time())}",
        "name": f"停摆期间跳过了 {n} 件事",
        "prompt": prompt,
        "purpose": "停摆期间被跳过的定时事项汇总（REQ-78，不补发只告知）",
        "trigger_time": now_ts,
        "attempt": 0,
        "ts": now_ts,
        "notify_attempts": 0,
    }


def queue_digest(jarvis_dir: Path = ROOT, force: bool = False,
                 dry_run: bool = False) -> int:
    """Fold unconsumed skip events into one breach-queue digest entry.

    Returns the number of events folded (0 = nothing to do / gated / failed).
    Never raises. Write order is consumed-state FIRST, breach line SECOND —
    a crash in between loses the digest instead of duplicating it.
    """
    try:
        jarvis_dir = Path(jarvis_dir)
        state_path = _state_file(jarvis_dir)
        state = _load_state(state_path)

        now = time.time()
        if not force and now - float(state.get("last_scan", 0)) < SCAN_INTERVAL_S:
            return 0

        events = collect_unconsumed(jarvis_dir, state)
        now_ts = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

        if dry_run:
            for e in events:
                print(f"[skip-digest] would consume: {_event_key(e)}")
            if events:
                print(json.dumps(_digest_entry(events, now_ts),
                                 ensure_ascii=False, indent=2))
            return len(events)

        consumed = {k: t for k, t in state.get("consumed", {}).items()
                    if now - float(t) < CONSUMED_RETENTION_S}
        for e in events:
            consumed[_event_key(e)] = now
        state = {"last_scan": now, "consumed": consumed}
        _save_state(state_path, state)   # BEFORE the append — 宁丢勿重

        if not events:
            return 0

        queue = _breach_queue(jarvis_dir)
        queue.parent.mkdir(parents=True, exist_ok=True)
        with open(queue, "a", encoding="utf-8") as f:
            f.write(json.dumps(_digest_entry(events, now_ts),
                               ensure_ascii=False) + "\n")
        print(f"[skip-digest] queued digest of {len(events)} skipped item(s)",
              file=sys.stderr)
        return len(events)
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
                    "— 汇总告知卡应已排队，请核对确实发出")
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
