"""Claude-independent brain-health assessment for the guardian daemon.

THE FAILURE CLASS THIS CLOSES — "alive but brain-dead, undetected":
On 2026-06-15 the heartbeat loop kept ticking for ~1h while every claude_call
returned '' (the `claude` binary was missing from the launchd PATH). Every
liveness signal stayed structurally fresh:
  - the daemon beat-marker (a beat is logged each cycle, even failed ones),
  - admin /health's heartbeat_age (built from last_run, which is rewritten on
    failed cycles, core/heartbeat.py:760),
  - the per-task circuit breaker (PRIORITY tasks force their circuit closed on
    every failure, core/heartbeat.py:613/765/901, so they NEVER trip the only
    user-visible alert at heartbeat.py:777-779).
The outage was invisible until an incidental restart.

The guardian daemon is the right place to catch this: it is a SEPARATE process
with no Claude dependency, it already owns a Claude-independent alert channel
(notify_lark → lark-cli → osascript banner → dead-letter), and it already has
restart-loop guards and a deploy window. This module is the pure, side-effect-
free core it calls: given a snapshot of heartbeat_state.json + the task table +
the previous sample, it returns the verdict and the alert text. No I/O here, so
it is trivially unit-testable.

Two complementary detectors (a brain-dead loop trips at least one):

  1. STARVATION / ran-but-failing (STATELESS). A task whose last_run is recent
     (it IS being scheduled) but whose last_success is stale past
     STARVATION_FACTOR×interval AND whose last_status is a failure. This is the
     REQ-51 truth-watermark logic from core.watermarks reused here: last_success
     is the only honest "it actually worked" signal (last_run lies). Catches
     non-priority tasks and any task whose success genuinely went stale. Two or
     more starved-failing tasks at once ⇒ systemic (not one flaky task).

  2. PRIORITY total_failures WINDOWS (STATEFUL). PRIORITY tasks keep last_success
     fresh — empty_pre marks success even on a cycle whose Claude call later
     fails (heartbeat.py:623-624) — so detector 1 misses them. Instead, sample
     circuit.total_failures / total_runs each daemon check; a window where
     failures advanced with ZERO successes is a "failing window". N consecutive
     failing windows ⇒ brain-dead. total_failures is the ONLY durable per-cycle
     Claude-failure signal for priority tasks — consecutive_failures is reset to
     0 on every trip (task_protocol.py:76), so it must NOT be used.
"""

from __future__ import annotations

# Mirrors core.watermarks.STARVATION_FACTOR (kept local so this daemon-critical
# module stays import-light and dependency-free for unit tests). 2x tolerates
# one missed slot before alarming.
STARVATION_FACTOR = 2.0

# last_status values that mean the cycle ran but the Claude call did not succeed.
FAILURE_STATUSES = {"failed", "timeout", "parse_failed"}

# A priority task must look brain-dead across this many consecutive daemon checks
# (with NO interleaved success) before we alert. >=2 means the daemon must see
# sustained failure across two windows (~8 min at a 4-min cadence), never a
# single flaky cycle.
FAIL_WINDOWS_THRESHOLD = 2

# A starvation-class signal is "systemic" (true brain-death, not one dead
# channel) only when this many tasks are simultaneously ran-but-failing. A
# single starved task is already reported by the watermark/self-diagnostic path.
MIN_STARVED_FOR_SYSTEMIC = 2

# Safety default when a task has no parseable interval (seconds).
_DEFAULT_INTERVAL = 600


def _fmt_age(seconds: float) -> str:
    if seconds >= 86400:
        return f"{seconds / 86400:.1f} 天"
    if seconds >= 3600:
        return f"{seconds / 3600:.1f} 小时"
    return f"{int(seconds / 60)} 分钟"


def _interval_for(name: str, ts: dict, task_interval: float, overrides: dict) -> float:
    """Same precedence as run_cycle / watermarks: override → legacy
    effective_interval in state → HEARTBEAT.md default."""
    interval = (overrides.get(name)
                or ts.get("effective_interval", 0)
                or task_interval)
    try:
        interval = float(interval)
    except (TypeError, ValueError):
        interval = 0
    return interval if interval > 0 else _DEFAULT_INTERVAL


def assess(*, state: dict, tasks: list[dict], overrides: dict,
           priority_tasks, prev_samples: dict, now: float,
           failure_threshold: int = 5) -> dict:
    """Assess whether the heartbeat is alive-but-brain-dead.

    Pure. Reads nothing; returns everything (including the next sample to persist).

    Args:
      state:        parsed heartbeat_state.json
      tasks:        parse_heartbeat() output (each dict has "name", "interval")
      overrides:    parsed interval_overrides.json (may be {})
      priority_tasks: iterable of PRIORITY task names
      prev_samples: persisted per-priority-task samples from the previous check
                    {name: {total_failures, total_runs, fail_windows}}
      now:          wall-clock seconds
      failure_threshold: CircuitState.FAILURE_THRESHOLD (informational)

    Returns dict:
      {brain_dead: bool, alerts: [str], summary: str, samples: {...}}
    """
    priority = set(priority_tasks)
    samples: dict = {}
    starved: list[tuple[str, float, float]] = []   # (name, success_age, interval)
    priority_dead: list[tuple[str, int]] = []      # (name, fail_windows)

    for task in tasks:
        name = task.get("name")
        if not name:
            continue
        ts = state.get(name, {}) or {}
        interval = _interval_for(name, ts, task.get("interval", 0), overrides)
        last_run = ts.get("last_run", 0) or 0
        last_success = ts.get("last_success", 0) or last_run
        last_status = ts.get("last_status", "") or ""
        circuit = ts.get("circuit", {}) or {}
        total_failures = circuit.get("total_failures", 0) or 0
        total_runs = circuit.get("total_runs", 0) or 0
        disabled_until = circuit.get("disabled_until", 0) or 0

        # ── Detector 2: PRIORITY total_failures windows (stateful) ──
        if name in priority:
            prev = prev_samples.get(name, {}) or {}
            p_tf = prev.get("total_failures", total_failures)  # first sight ⇒ no delta
            p_tr = prev.get("total_runs", total_runs)
            fail_windows = prev.get("fail_windows", 0)
            d_f = total_failures - p_tf
            d_r = total_runs - p_tr
            if d_f < 0 or d_r < 0:
                # Counters went backwards (state reset / task replaced) — rebaseline.
                fail_windows = 0
            else:
                successes = d_r - d_f
                if d_f > 0 and successes <= 0:
                    fail_windows += 1          # ran and ONLY failed this window
                elif successes > 0:
                    fail_windows = 0           # at least one real success ⇒ healthy
                # else d_r == 0: didn't run this window ⇒ hold the counter
            samples[name] = {"total_failures": total_failures,
                             "total_runs": total_runs,
                             "fail_windows": fail_windows}
            if fail_windows >= FAIL_WINDOWS_THRESHOLD:
                priority_dead.append((name, fail_windows))

        # ── Detector 1: starvation / ran-but-failing (stateless) ──
        # An already-tripped circuit is reported via the circuit/watermark path.
        if disabled_until > now:
            continue
        recently_ran = last_run > 0 and (now - last_run) < STARVATION_FACTOR * interval
        success_stale = last_success > 0 and (now - last_success) > STARVATION_FACTOR * interval
        if recently_ran and success_stale and last_status in FAILURE_STATUSES:
            starved.append((name, now - last_success, interval))

    brain_dead = bool(priority_dead) or len(starved) >= MIN_STARVED_FOR_SYSTEMIC

    # Alert text is boss-facing (it lands in Pascal's Lark): plain sentences,
    # no internal jargon (last_success / envelope / PRIORITY / circuit). The
    # diagnostic detail he'd never act on directly lives in jarvis.log, not here.
    alerts: list[str] = []
    for name, fw in priority_dead:
        alerts.append(f"{name} 最近一直在失败，没有一次成功")
    for name, age, interval in starved:
        alerts.append(
            f"{name} 已经 {_fmt_age(age)} 没有跑成过了"
            f"（正常应该每 {_fmt_age(interval)} 成功一次）")

    summary = ""
    if brain_dead:
        shown = alerts[:4]
        more = f"\n（还有 {len(alerts) - 4} 个类似的没列出）" if len(alerts) > 4 else ""
        summary = (
            "⚠️ 我有几个后台任务卡住了——它们还在反复重试，但每次都失败：\n"
            + "\n".join(f"· {a}" for a in shown) + more
            + "\n\n通常是我调用 Claude 的环节出了问题。我只会提醒、不会自己动手修。"
            + "方便的时候在电脑上对 Jarvis 说一句「查一下后台任务为什么失败」就能排查。")

    return {"brain_dead": brain_dead, "alerts": alerts,
            "summary": summary, "samples": samples}
