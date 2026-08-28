"""Channel watermark monitoring (REQ-12).

Every push channel the user depends on (eigenflux feed, checkin,
recommendations, ...) has gone silently dead before — at least 4 outages were
discovered by the user asking "我好久没有收到 feed 了". This module computes
"time since the channel last worked" against its expected cadence so
self-diagnostic can flag a dead channel instead of waiting for a human.

Usage (from tasks/self_diagnostic_pre.sh):
    python3 -m core.watermarks
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from core.heartbeat import parse_heartbeat
from core.interval_config import (
    parse_interval_overrides,
    resolve_effective_interval,
)
from core.textutil import task_display_name

# A task is "starved" when it hasn't run for longer than this multiple of its
# expected interval. 2x tolerates one missed slot before alarming.
STARVATION_FACTOR = 2.0

# The resident loop ticks every 10s, but a shared model batch can legitimately
# run for about a minute.  Without fixed headroom a 1m task crossed its 2x
# boundary while the next run was already in flight and self-diagnostic paged
# on a healthy execution.  This grace is scheduling jitter, not an outage
# waiver; failing tasks and genuinely stopped channels still cross it quickly.
STARVATION_GRACE_SECONDS = 60

# Fresh-install grace: a collaborator's first-ever self-diagnostic (v1.3.0,
# 2026-07-13) listed SIX "has NEVER run" ⚠️ lines — including self-diagnostic
# reporting ITSELF, whose last_success only lands after its first cycle
# finishes — because last_success==0 alarmed with no notion of how long the
# install had existed. A task that never succeeded is only an outage once the
# install is older than the same 2x-interval bar used for starvation; younger
# than that it is simply a schedule that has not reached the task yet.
INSTALL_STAMP = Path("data") / ".install_stamp"


def _install_ts(jd: Path, now: float) -> float:
    """Mtime of the install stamp; self-heals by creating it on first use
    (existing installs get it stamped `now`, which is harmless — their tasks
    already carry last_success). Falls back to `now` if data/ is unwritable."""
    stamp = jd / INSTALL_STAMP
    try:
        return stamp.stat().st_mtime
    except OSError:
        pass
    try:
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.touch()
        os.utime(stamp, (now, now))
    except OSError:
        pass
    return now


def _fmt_age(seconds: float) -> str:
    if seconds >= 86400:
        return f"{seconds / 86400:.1f}d"
    if seconds >= 3600:
        return f"{seconds / 3600:.1f}h"
    return f"{int(seconds / 60)}min"


def _fmt_age_cn(seconds: float) -> str:
    """Chinese age for the ⚠️ lines — those land verbatim onthe owner's
    selfmon card and the guardian relay (card-style contract: 人话中文;
    2026-08-24 audit found「reply-followup: last real success 5min ago —
    STARVED」reaching the boss). Non-⚠️ info lines stay machine-flavored."""
    if seconds >= 86400:
        return f"{seconds / 86400:.1f} 天"
    if seconds >= 3600:
        return f"{seconds / 3600:.1f} 小时"
    return f"{max(1, int(seconds / 60))} 分钟"


# A queue entry is only "stuck" once a batch window has been open this long
# without a flush — one heartbeat cycle plus headroom. Anything younger is the
# queue working as designed (waiting for a window or a user-activity breakpoint).
QUEUE_OVERDUE_GRACE_SECONDS = 15 * 60

# Unified-delivery envelopes are often intentionally deferred to a future
# attention window (daily cap, quiet hours, burst budget).  Queue depth alone
# is therefore not a failure signal.  A due envelope gets one normal heartbeat
# flush window before it is considered stuck.
DELIVERY_QUEUE_OVERDUE_GRACE_SECONDS = 15 * 60


def _queue_status_line(jd: Path, queue_depth: int, now: float) -> str:
    """Describe the batch queue: held entries are normal (quiet hours hold for
    the morning digest, daytime general-interest holds for the next batch
    window), and only a flush that failed to fire after a window opened is a
    warning. The old unconditional 'pending morning flush' wording made normal
    daytime batching read as a stuck queue in self-diagnostic."""
    from datetime import datetime

    from core.heartbeat_loop import (BATCH_FLUSH_STAMP, BATCH_WINDOWS_MIN,
                                     _in_quiet_hours)
    # Derive time-of-day from the report's `now` (system-local tz, same clock
    # as the flush stamp) so tests can pin the wall clock.
    t = datetime.fromtimestamp(now)
    mod = t.hour * 60 + t.minute
    if _in_quiet_hours(mod):
        return (f"  Batch queue: {queue_depth} message(s) held for the "
                f"morning digest (quiet hours — normal)")
    try:
        last_flush = float((jd / BATCH_FLUSH_STAMP).read_text().strip())
    except (OSError, ValueError):
        last_flush = 0.0
    passed = [w for w in BATCH_WINDOWS_MIN if mod >= w]
    if passed:
        since_window = (mod - max(passed)) * 60
        flush_predates_window = now - last_flush > since_window + 60
        if flush_predates_window and since_window > QUEUE_OVERDUE_GRACE_SECONDS:
            return (f"  ⚠️ 有 {queue_depth} 条攒批消息卡住没发出去 — "
                    f"{max(passed) // 60:02d}:{max(passed) % 60:02d} 的发送时段"
                    f"已经过了 {_fmt_age_cn(since_window)}，一直没发出来")
    nxt = next((w for w in BATCH_WINDOWS_MIN if w > mod), None)
    when = (f"{nxt // 60:02d}:{nxt % 60:02d}" if nxt is not None
            else f"tomorrow {BATCH_WINDOWS_MIN[0] // 60:02d}:00")
    return (f"  Batch queue: {queue_depth} message(s) awaiting next batch "
            f"window ({when}) or user activity — normal")


def _delivery_queue_status_line(delivery: dict, now: float) -> str:
    """Classify unified-delivery work by due time, not raw queue depth."""
    from datetime import datetime

    depth = int(delivery.get("queued", 0) or 0)
    if depth <= 0:
        return ""

    queued_items = delivery.get("queued_items") or []
    projected_overdue = delivery.get("queued_overdue")
    overdue = []
    future_epochs = []
    for item in queued_items:
        try:
            created = float(item.get("created_epoch", 0) or 0)
            raw_next = item.get("next_attempt_epoch")
            next_attempt = (float(raw_next)
                            if raw_next is not None else None)
        except (TypeError, ValueError):
            continue
        if next_attempt is not None and next_attempt > now:
            future_epochs.append(next_attempt)
            continue
        due_since = next_attempt if next_attempt is not None else created
        if due_since and now - due_since > DELIVERY_QUEUE_OVERDUE_GRACE_SECONDS:
            overdue.append(item)

    overdue_count = (int(projected_overdue or 0)
                     if projected_overdue is not None else len(overdue))
    if overdue_count:
        return (f"  ⚠️ 排队的 {depth} 条消息里有 {overdue_count} 条早该送到了，"
                f"自动重试也没送出去")
    projected_next = delivery.get("next_queued_epoch")
    if projected_next is not None:
        try:
            future_epochs.append(float(projected_next))
        except (TypeError, ValueError):
            pass
    if future_epochs:
        next_time = datetime.fromtimestamp(min(future_epochs)).strftime(
            "%m-%d %H:%M")
        return (f"  Unified delivery: {depth} item(s) deferred to an allowed "
                f"attention window (next {next_time} — normal)")
    return (f"  Unified delivery: {depth} item(s) awaiting the current "
            "automatic flush window — normal")


def channel_watermark_report(jarvis_dir: str | Path,
                             heartbeat_file: str | Path | None = None,
                             now: float | None = None) -> str:
    """Build a human-readable watermark section for self-diagnostic."""
    jd = Path(jarvis_dir)
    hb = Path(heartbeat_file) if heartbeat_file else jd / "HEARTBEAT.md"
    now = now if now is not None else time.time()

    def _load(path: Path) -> dict:
        try:
            return json.loads(path.read_text())
        except (OSError, json.JSONDecodeError, ValueError):
            return {}

    state = _load(jd / "heartbeat_state.json")
    overrides = parse_interval_overrides(
        _load(jd / "interval_overrides.json"))
    install_ts = _install_ts(jd, now)

    starved, circuits, pending_first = [], [], []
    for task in parse_heartbeat(hb):
        name = task["name"]
        ts = state.get(name, {})
        # Same precedence as run_cycle: override → legacy effective_interval
        # in state → HEARTBEAT.md default. Ignoring the legacy field made
        # legitimately slowed-down tasks look STARVED.
        interval = resolve_effective_interval(
            name,
            task["interval"],
            ts.get("effective_interval", 0),
            overrides,
        )
        last_run = ts.get("last_run", 0)
        # TRUTH watermark (REQ-51): last_run is a scheduling watermark that
        # gets rewritten on every empty_pre/pre-failure skip, so chronically
        # failing channels looked perpetually fresh (repos-sync: 19/19 pre
        # timeouts, report said 'all channels within expected cadence').
        # last_success is set only on a real ok/idle finish. Fall back to
        # last_run for state predating the field.
        last_success = ts.get("last_success", 0) or last_run
        last_status = ts.get("last_status", "")
        disabled_until = ts.get("circuit", {}).get("disabled_until", 0)

        # ⚠️ lines are boss-facing (selfmon card + guardian relay): plain
        # Chinese with the shared display name, never the raw task id
        # (card-style contract, 2026-08-24 audit).
        display = task_display_name(name)
        if disabled_until > now:
            circuits.append(
                f"  ⚠️ 「{display}」这个后台任务因为连续失败暂停了，"
                f"{_fmt_age_cn(disabled_until - now)}后自动恢复")
            continue
        if last_status.startswith("pre_") and                 ts.get("circuit", {}).get("consecutive_failures", 0) >= 3:
            starved.append(
                f"  ⚠️ 「{display}」这个后台任务一直取不到数据"
                f"（连续 {ts['circuit']['consecutive_failures']} 次失败），"
                f"这条渠道等于断了")
        if last_success == 0:
            # Within the fresh-install grace (2x the task's own interval since
            # install) a missing first run is the schedule, not an outage —
            # report it as an info line, never a ⚠️ alert.
            if now - install_ts < (STARVATION_FACTOR * interval
                                   + STARVATION_GRACE_SECONDS):
                pending_first.append(name)
            else:
                starved.append(
                    f"  ⚠️ 「{display}」这个后台任务从来没跑成过"
                    f"（正常 {_fmt_age_cn(interval)}一次）")
        elif now - last_success > (STARVATION_FACTOR * interval
                                   + STARVATION_GRACE_SECONDS):
            starved.append(
                f"  ⚠️ 「{display}」这个后台任务已经 "
                f"{_fmt_age_cn(now - last_success)}没跑成了"
                f"（正常 {_fmt_age_cn(interval)}一次）")

    from core.state_projection import delivery_overview
    delivery = delivery_overview(
        jd, now=now,
        queue_overdue_grace_seconds=DELIVERY_QUEUE_OVERDUE_GRACE_SECONDS,
    )
    if delivery is None:
        delivery = _load(jd / ".delivery_state.json")
    consec_fails = delivery.get("consec_fails", 0)

    night_queue = jd / "night_queue.jsonl"
    try:
        queue_depth = len(night_queue.read_text().splitlines())
    except OSError:
        queue_depth = 0
    queue_line = _queue_status_line(jd, queue_depth, now) if queue_depth else ""

    lines = ["--- Channel Watermarks ---"]
    if starved:
        lines.extend(starved)
    if circuits:
        lines.extend(circuits)
    if pending_first:
        lines.append(
            f"  ○ first run pending (installed {_fmt_age(now - install_ts)} ago"
            f" — normal): {', '.join(pending_first)}")
    if consec_fails > 0:
        lines.append(f"  ⚠️ 飞书消息连续 {consec_fails} 次没发出去")
    delivery_queue_line = _delivery_queue_status_line(delivery, now)
    if delivery_queue_line:
        lines.append(delivery_queue_line)
    if queue_line:
        lines.append(queue_line)
    if len(lines) == 1:
        lines.append("  ✓ All task channels within expected cadence")
    return "\n".join(lines)


if __name__ == "__main__":
    import os
    print(channel_watermark_report(os.environ.get("JARVIS_DIR", ".")))
