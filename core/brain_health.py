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

Three complementary detectors (a brain-dead loop trips at least one):

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
     0 on every trip (task_protocol.py:76), so it must NOT be used for window
     COUNTING (its instantaneous value is still a valid threshold — detector 3).

  3. PRIORITY WEDGE (STATELESS). A single PRIORITY task stuck in a sustained
     failure state. Mirrors admin /health's priority_wedged rule exactly
     (admin.py _serve_health): consecutive_failures >= WEDGE_CONSEC_THRESHOLD,
     OR last_success stale past STARVATION_FACTOR×interval with a failing
     last_status. Red-team 7/9: on 7/8 intention-check wedged 16:04→23:37 and
     BOTH detectors above missed it — the wedged task stopped advancing
     total_runs, so detector 2 held its window count forever (the d_r == 0
     branch), and detector 1 needs recently_ran plus TWO starved tasks. The
     only page Pascal got was the duplicate degraded-channel alert he had
     just asked to be deleted; this detector is what makes that deletion's
     premise ("brain-health owns the wedge alert") actually true. A single
     wedged PRIORITY task pages — priority tasks are infrastructure, there
     is no "one flaky task" excuse for them.
"""

from __future__ import annotations

from core.interval_config import resolve_effective_interval

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

# Detector 3: a PRIORITY task with consecutive_failures at/above this is
# wedged. Mirrors admin /health's priority_wedged rule — keep the two in
# lockstep, or a wedge admin can see becomes invisible again (the exact 7/8
# gap this closes). consecutive_failures resets on circuit trip
# (task_protocol.py) and on real successes, but empty_pre cycles refresh
# last_success WITHOUT resetting it — so a frozen >=3 is a durable "the
# Claude side of this task keeps failing / stopped running" signal.
WEDGE_CONSEC_THRESHOLD = 3

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
    interval = resolve_effective_interval(
        name, task_interval, ts.get("effective_interval", 0), overrides)
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
    wedged: list[tuple[str, float]] = []           # (name, success_age)

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
        consecutive = circuit.get("consecutive_failures", 0) or 0

        # ── Detector 3: PRIORITY wedge (stateless) ──
        # Same rule as admin /health's priority_wedged (see module docstring).
        # Deliberately BEFORE the disabled_until skip below: priority circuits
        # are forced closed by the loop, so an open one is itself wedge-shaped,
        # and admin does not skip open circuits either.
        if name in priority:
            success_age = (now - last_success) if last_success > 0 else 0.0
            success_stale = (last_success > 0
                             and success_age > STARVATION_FACTOR * interval)
            if (consecutive >= WEDGE_CONSEC_THRESHOLD
                    or (success_stale and last_status in FAILURE_STATUSES)):
                wedged.append((name, success_age))

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

    brain_dead = (bool(priority_dead) or bool(wedged)
                  or len(starved) >= MIN_STARVED_FOR_SYSTEMIC)

    # Alert text is boss-facing (it lands in Pascal's Lark): plain sentences,
    # no internal jargon (last_success / envelope / PRIORITY / circuit). The
    # diagnostic detail he'd never act on directly lives in jarvis.log, not here.
    # One line per task — a task tripping two detectors must not read twice.
    alerts: list[str] = []
    named: set = set()
    for name, fw in priority_dead:
        alerts.append(f"{name} 最近一直在失败，没有一次成功")
        named.add(name)
    for name, age in wedged:
        if name in named:
            continue
        if age > 0:
            alerts.append(f"心跳任务 {name} 卡住 {_fmt_age(age)} 了，一直没有成功跑完")
        else:
            alerts.append(f"心跳任务 {name} 卡住了，一直没有成功跑完")
        named.add(name)
    for name, age, interval in starved:
        if name in named:
            continue
        alerts.append(
            f"{name} 已经 {_fmt_age(age)} 没有跑成过了"
            f"（正常应该每 {_fmt_age(interval)} 成功一次）")
        named.add(name)

    summary = ""
    if brain_dead:
        shown = alerts[:4]
        more = f"\n（还有 {len(alerts) - 4} 个类似的没列出）" if len(alerts) > 4 else ""
        summary = (
            "⚠️ 我有后台任务卡住了，一直没有成功跑完：\n"
            + "\n".join(f"· {a}" for a in shown) + more
            + "\n\n通常是我调用 Claude 的环节出了问题。我只会提醒、不会自己动手修。"
            + "方便的时候在电脑上对 Jarvis 说一句「查一下后台任务为什么失败」就能排查。")

    return {"brain_dead": brain_dead, "alerts": alerts,
            "summary": summary, "samples": samples}
