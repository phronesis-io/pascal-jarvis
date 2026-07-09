"""Tests for the daemon brain-death detector (core/brain_health.py).

Grounded in the 2026-06-15 incident: the heartbeat loop kept ticking while every
claude_call failed, and every existing liveness signal stayed fresh. These tests
pin the two detectors (stateless starvation + stateful priority windows), the
incident reproduction, and the false-positive guards that keep it alert-only-safe.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import brain_health
from core.brain_health import (FAIL_WINDOWS_THRESHOLD, MIN_STARVED_FOR_SYSTEMIC,
                               STARVATION_FACTOR, WEDGE_CONSEC_THRESHOLD, assess)

NOW = 1_000_000.0
HOUR = 3600.0
PRIORITY = {"memory-hourly", "intention-check", "calendar-sync"}


def _task(name, interval=HOUR):
    return {"name": name, "interval": interval}


def _ts(*, last_run, last_success, last_status="ok",
        total_failures=0, total_runs=0, disabled_until=0,
        consecutive_failures=0):
    return {
        "last_run": last_run,
        "last_success": last_success,
        "last_status": last_status,
        "circuit": {"total_failures": total_failures, "total_runs": total_runs,
                    "disabled_until": disabled_until,
                    "consecutive_failures": consecutive_failures},
    }


def _assess(state, tasks, prev=None):
    return assess(state=state, tasks=tasks, overrides={},
                  priority_tasks=PRIORITY, prev_samples=prev or {}, now=NOW)


# ── Healthy system: never alert ──────────────────────────────────────

def test_all_fresh_not_brain_dead():
    state = {
        "feed-triage": _ts(last_run=NOW - 60, last_success=NOW - 60),
        "memory-hourly": _ts(last_run=NOW - 60, last_success=NOW - 60,
                             total_failures=3, total_runs=100),
    }
    tasks = [_task("feed-triage"), _task("memory-hourly")]
    r = _assess(state, tasks)
    assert r["brain_dead"] is False
    assert r["alerts"] == []


def test_single_starved_task_below_systemic_threshold():
    # One ran-but-failing task is reported by the watermark path, not a
    # daemon-level brain-death page (MIN_STARVED_FOR_SYSTEMIC == 2).
    assert MIN_STARVED_FOR_SYSTEMIC == 2
    state = {
        "feed-triage": _ts(last_run=NOW - 60, last_success=NOW - 60),
        "content-recommend": _ts(last_run=NOW - 60,
                                 last_success=NOW - 5 * HOUR,
                                 last_status="parse_failed"),
    }
    tasks = [_task("feed-triage"), _task("content-recommend")]
    r = _assess(state, tasks)
    assert r["brain_dead"] is False


# ── Detector 1: starvation / ran-but-failing (stateless) ─────────────

def test_two_starved_failing_tasks_is_systemic():
    # The exact live signature found on 2026-06-15: two non-priority tasks ran
    # recently but last_success is stale past 2x interval with a failure status.
    state = {
        "content-recommend": _ts(last_run=NOW - 60,
                                 last_success=NOW - 5 * HOUR,
                                 last_status="parse_failed"),
        "perception-collect": _ts(last_run=NOW - 60,
                                  last_success=NOW - 3 * HOUR,
                                  last_status="failed"),
    }
    tasks = [_task("content-recommend"), _task("perception-collect")]
    r = _assess(state, tasks)
    assert r["brain_dead"] is True
    assert len(r["alerts"]) == 2


def test_stale_success_but_not_recently_run_is_not_flagged():
    # A task that simply isn't being scheduled (last_run old) is NOT brain-death
    # — it's a stopped/disabled task; the daemon's bot-alive + watermark paths
    # own that. Requires recently_ran to fire.
    state = {
        "a": _ts(last_run=NOW - 10 * HOUR, last_success=NOW - 10 * HOUR,
                 last_status="failed"),
        "b": _ts(last_run=NOW - 10 * HOUR, last_success=NOW - 10 * HOUR,
                 last_status="failed"),
    }
    tasks = [_task("a"), _task("b")]
    assert _assess(state, tasks)["brain_dead"] is False


def test_open_circuit_not_counted_as_starved():
    # A tripped circuit is reported via the circuit/watermark path, not here.
    state = {
        "a": _ts(last_run=NOW - 60, last_success=NOW - 5 * HOUR,
                 last_status="failed", disabled_until=NOW + HOUR),
        "b": _ts(last_run=NOW - 60, last_success=NOW - 5 * HOUR,
                 last_status="failed", disabled_until=NOW + HOUR),
    }
    tasks = [_task("a"), _task("b")]
    assert _assess(state, tasks)["brain_dead"] is False


def test_succeeded_within_window_not_starved():
    state = {
        "a": _ts(last_run=NOW - 60, last_success=NOW - int(1.5 * HOUR),
                 last_status="failed"),
        "b": _ts(last_run=NOW - 60, last_success=NOW - int(1.5 * HOUR),
                 last_status="failed"),
    }
    tasks = [_task("a"), _task("b")]
    # 1.5x interval < STARVATION_FACTOR(2.0)x ⇒ not yet starved.
    assert STARVATION_FACTOR == 2.0
    assert _assess(state, tasks)["brain_dead"] is False


# ── Detector 2: PRIORITY total_failures windows (stateful) ───────────

def test_priority_failing_windows_trip_after_threshold():
    # PRIORITY task keeps last_success fresh (empty_pre), so detector 1 misses
    # it. Simulate the daemon sampling it across consecutive checks while
    # total_failures advances with zero successes.
    tasks = [_task("memory-hourly")]

    def state_at(tf, tr):
        # last_success fresh + non-failure status ⇒ starvation path silent,
        # isolating the windows detector.
        return {"memory-hourly": _ts(last_run=NOW - 60, last_success=NOW - 60,
                                     last_status="empty_pre",
                                     total_failures=tf, total_runs=tr)}

    samples = {}
    # check 1: first sight, no delta
    r1 = _assess(state_at(10, 20), tasks, prev=samples)
    assert r1["brain_dead"] is False
    samples = r1["samples"]
    assert samples["memory-hourly"]["fail_windows"] == 0

    # check 2: +5 failures, +5 runs ⇒ 0 successes ⇒ one failing window
    r2 = _assess(state_at(15, 25), tasks, prev=samples)
    assert r2["brain_dead"] is False
    samples = r2["samples"]
    assert samples["memory-hourly"]["fail_windows"] == 1

    # check 3: another failing window ⇒ crosses FAIL_WINDOWS_THRESHOLD(2)
    r3 = _assess(state_at(20, 30), tasks, prev=samples)
    assert r3["brain_dead"] is True
    assert any("memory-hourly" in a for a in r3["alerts"])
    assert FAIL_WINDOWS_THRESHOLD == 2


def test_priority_success_resets_failing_windows():
    tasks = [_task("memory-hourly")]
    # Start with one failing window banked.
    samples = {"memory-hourly": {"total_failures": 10, "total_runs": 20,
                                 "fail_windows": 1}}
    # Next window: runs advanced more than failures ⇒ a real success happened.
    state = {"memory-hourly": _ts(last_run=NOW - 60, last_success=NOW - 60,
                                  last_status="ok",
                                  total_failures=10, total_runs=23)}
    r = _assess(state, tasks, prev=samples)
    assert r["samples"]["memory-hourly"]["fail_windows"] == 0
    assert r["brain_dead"] is False


def test_counters_backwards_rebaseline():
    # heartbeat_state.json reset / task replaced ⇒ counters drop ⇒ rebaseline,
    # never a spurious alert.
    tasks = [_task("memory-hourly")]
    samples = {"memory-hourly": {"total_failures": 100, "total_runs": 200,
                                 "fail_windows": 1}}
    state = {"memory-hourly": _ts(last_run=NOW - 60, last_success=NOW - 60,
                                  last_status="ok",
                                  total_failures=2, total_runs=5)}
    r = _assess(state, tasks, prev=samples)
    assert r["samples"]["memory-hourly"]["fail_windows"] == 0
    assert r["brain_dead"] is False


def test_didnt_run_this_window_holds_counter():
    # No new runs this window ⇒ fail_windows unchanged (neither advance nor reset).
    tasks = [_task("memory-hourly")]
    samples = {"memory-hourly": {"total_failures": 10, "total_runs": 20,
                                 "fail_windows": 1}}
    state = {"memory-hourly": _ts(last_run=NOW - 60, last_success=NOW - 60,
                                  last_status="empty_pre",
                                  total_failures=10, total_runs=20)}
    r = _assess(state, tasks, prev=samples)
    assert r["samples"]["memory-hourly"]["fail_windows"] == 1


# ── Detector 3: PRIORITY wedge (stateless) ───────────────────────────
# Red-team 7/9: the 7/8 intention-check wedge (16:04→23:37) evaded both
# detectors above; admin /health flagged priority_wedged the whole time and
# the only Lark page was the duplicate degraded-channel alert Pascal had
# just asked to be deleted. These tests replay that shape and pin the rule
# to admin's (consec>=3 OR success_stale && failing).

def _wedge_state(name="intention-check", *, hours_stuck=7.5, consec=3,
                 last_status="timeout"):
    """The replayed 7/8 wedge: the task stopped being scheduled (last_run
    stale ⇒ detector 1's recently_ran is False), stopped succeeding, and its
    counters froze (⇒ detector 2's d_r == 0 branch holds fail_windows)."""
    return {name: _ts(last_run=NOW - hours_stuck * HOUR,
                      last_success=NOW - hours_stuck * HOUR,
                      last_status=last_status,
                      total_failures=601, total_runs=1802,
                      consecutive_failures=consec)}


def test_replayed_7_8_wedge_is_detected():
    tasks = [_task("intention-check", interval=HOUR)]
    state = _wedge_state()

    # Prove the premise: with the wedge detector's inputs zeroed out, the two
    # old detectors alone stay silent across consecutive windows (frozen
    # counters hold fail_windows; single non-recently-ran task never starves).
    r1 = _assess(state, tasks)
    samples = r1["samples"]
    assert samples["intention-check"]["fail_windows"] == 0
    r2 = _assess(state, tasks, prev=samples)
    assert r2["samples"]["intention-check"]["fail_windows"] == 0

    # The wedge detector pages — on the FIRST sight, and it stays up.
    assert r1["brain_dead"] is True
    assert r2["brain_dead"] is True
    assert any("intention-check" in a and "卡住" in a for a in r1["alerts"])
    # Boss-facing copy: names the stuck task and its duration, no jargon.
    assert any("7.5 小时" in a for a in r1["alerts"])
    for banned in ("consecutive", "circuit", "PRIORITY", "wedge", "status"):
        assert all(banned not in a for a in r1["alerts"])
    assert "intention-check" in r1["summary"]


def test_wedge_consec_arm_fires_even_with_fresh_success():
    # empty_pre cycles refresh last_success WITHOUT resetting
    # consecutive_failures — admin's "refreshed case". consec>=3 alone wedges.
    assert WEDGE_CONSEC_THRESHOLD == 3
    tasks = [_task("intention-check")]
    state = {"intention-check": _ts(last_run=NOW - 60, last_success=NOW - 60,
                                    last_status="empty_pre",
                                    consecutive_failures=3)}
    r = _assess(state, tasks)
    assert r["brain_dead"] is True
    assert any("intention-check" in a for a in r["alerts"])


def test_wedge_stale_failing_arm_fires_below_consec_threshold():
    # Circuit trip reset consec to 0, but the task is stale-and-failing —
    # the second arm (success_stale && failing) still wedges it.
    tasks = [_task("intention-check")]
    state = _wedge_state(consec=0, last_status="failed")
    r = _assess(state, tasks)
    assert r["brain_dead"] is True
    assert any("intention-check" in a and "卡住" in a for a in r["alerts"])


def test_wedge_requires_priority_task():
    # The same shape on a non-priority task is NOT a wedge (single starved
    # task stays below MIN_STARVED_FOR_SYSTEMIC; not recently_ran anyway).
    tasks = [_task("feed-triage")]
    state = {"feed-triage": _ts(last_run=NOW - 7 * HOUR,
                                last_success=NOW - 7 * HOUR,
                                last_status="failed",
                                consecutive_failures=4)}
    assert _assess(state, tasks)["brain_dead"] is False


def test_wedge_not_flagged_when_merely_stale_but_not_failing():
    # Post-nap shape: every last_success is stale by the sleep length but the
    # task's last status is healthy and consec is low — never a wedge.
    tasks = [_task("intention-check")]
    state = {"intention-check": _ts(last_run=NOW - 9 * HOUR,
                                    last_success=NOW - 9 * HOUR,
                                    last_status="empty_pre",
                                    consecutive_failures=2)}
    assert _assess(state, tasks)["brain_dead"] is False


def test_wedge_healthy_priority_not_flagged():
    tasks = [_task("intention-check")]
    state = {"intention-check": _ts(last_run=NOW - 60, last_success=NOW - 60,
                                    last_status="ok",
                                    consecutive_failures=2)}
    assert _assess(state, tasks)["brain_dead"] is False


def test_wedge_and_windows_alert_once_per_task():
    # A task tripping detector 2 AND the wedge must appear on ONE alert line.
    tasks = [_task("memory-hourly")]
    samples = {"memory-hourly": {"total_failures": 10, "total_runs": 20,
                                 "fail_windows": 2}}
    state = {"memory-hourly": _ts(last_run=NOW - 60, last_success=NOW - 60,
                                  last_status="empty_pre",
                                  total_failures=15, total_runs=25,
                                  consecutive_failures=4)}
    r = _assess(state, tasks, prev=samples)
    assert r["brain_dead"] is True
    assert sum("memory-hourly" in a for a in r["alerts"]) == 1
