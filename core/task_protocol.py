"""Task Protocol — defines the contract for heartbeat tasks.

A Task is a self-contained unit of proactive agent behavior:
  - gather(): collect data from the world (replaces pre-scripts)
  - should_run(): decide whether this invocation is worth executing
  - process(): apply reasoning to gathered data (LLM or Python)
  - deliver(): route output to user/storage (replaces post-scripts)

Tasks also carry their own metadata:
  - tier: 0 (Python-only), 1 (Haiku), 2 (Opus)
  - context_files: which memory files this task needs (selective injection)
  - circuit_state: tracks consecutive failures for auto-disable

This module provides the Protocol (interface) and a CircuitBreaker mixin.
Existing bash-based tasks continue to work — this is opt-in for new tasks
and gradual migration of existing ones.

Usage:
    class CalendarSync(TaskBase):
        name = "calendar-sync"
        tier = 0  # no LLM needed
        context_files = []  # no memory injection

        def gather(self) -> str:
            return run_lark_calendar_export()

        def process(self, data: str) -> str:
            return format_calendar_markdown(data)  # pure Python, no Claude

        def deliver(self, output: str) -> str:
            write_to_memory("hot/calendar_today.md", output)
            if events_changed(output):
                return notify_user(output)
            return ""
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field


# ── Circuit Breaker ──────────────────────────────────────────────────

@dataclass
class CircuitState:
    """Tracks consecutive failures for a task. Persisted in heartbeat_state.json."""
    consecutive_failures: int = 0
    last_failure_time: float = 0
    disabled_until: float = 0
    total_failures: int = 0
    total_runs: int = 0
    # RECENT trip count, used to escalate the disable duration. Unlike
    # total_failures (a monotonic lifetime counter that brain_health.py reads
    # for its drift detection — must NOT change semantics), this DECAYS: it
    # drops one level per clean run. So a long-healthy task that trips once
    # backs off for the 1h baseline, not the 16h cap — escalation tracks
    # CURRENT flakiness, not a lifetime grudge. (Before this, the duration used
    # total_failures // THRESHOLD, so a task with a long failure history jumped
    # straight to multi-hour cooldowns on its next trip even after weeks clean
    # — the over-punishment that pinned eigenflux-publish/perception-collect.)
    escalation_level: int = 0

    # Thresholds
    FAILURE_THRESHOLD: int = 5          # disable after 5 consecutive failures
    DISABLE_DURATION: float = 3600      # disable for 1 hour
    ESCALATION_FACTOR: float = 2.0      # double disable time on repeated trips
    MAX_ESCALATION: int = 5             # cap: 3600 * 2^4 = 16h

    def record_success(self):
        self.consecutive_failures = 0
        self.total_runs += 1
        # Recover toward baseline: each clean run sheds one escalation level,
        # so a task that trips and then runs cleanly returns to the 1h baseline.
        if self.escalation_level > 0:
            self.escalation_level -= 1

    def record_failure(self) -> bool:
        """Record a failure. Returns True if circuit just tripped (newly disabled)."""
        self.consecutive_failures += 1
        self.total_failures += 1
        self.total_runs += 1
        self.last_failure_time = time.time()

        if self.consecutive_failures >= self.FAILURE_THRESHOLD:
            # Escalate on RECENT trips, not lifetime failures.
            self.escalation_level = min(self.escalation_level + 1, self.MAX_ESCALATION)
            duration = self.DISABLE_DURATION * (self.ESCALATION_FACTOR ** (self.escalation_level - 1))
            self.disabled_until = time.time() + duration
            self.consecutive_failures = 0  # reset for next cycle
            return True
        return False

    @property
    def is_open(self) -> bool:
        """True = circuit is open (task disabled). False = closed (task can run)."""
        if self.disabled_until <= 0:
            return False
        if time.time() >= self.disabled_until:
            # Auto-reset: disable period expired
            self.disabled_until = 0
            return False
        return True

    @property
    def remaining_disable_seconds(self) -> int:
        if not self.is_open:
            return 0
        return max(0, int(self.disabled_until - time.time()))

    def to_dict(self) -> dict:
        d = {
            "consecutive_failures": self.consecutive_failures,
            "last_failure_time": self.last_failure_time,
            "disabled_until": self.disabled_until,
            "total_failures": self.total_failures,
            "total_runs": self.total_runs,
        }
        if self.escalation_level > 0:
            d["escalation_level"] = self.escalation_level
        return d

    @classmethod
    def from_dict(cls, d: dict) -> CircuitState:
        cs = cls()
        cs.consecutive_failures = d.get("consecutive_failures", 0)
        cs.last_failure_time = d.get("last_failure_time", 0)
        cs.disabled_until = d.get("disabled_until", 0)
        cs.total_failures = d.get("total_failures", 0)
        cs.total_runs = d.get("total_runs", 0)
        cs.escalation_level = d.get("escalation_level", 0)
        return cs


# ── Task State (persisted per task in heartbeat_state.json) ──────────

@dataclass
class TaskState:
    """Extended task state — replaces the simple {"last_run": int} dict.

    last_run is a SCHEDULING watermark (rewritten on every skip/retry rewind);
    last_success is the TRUTH watermark (REQ-51) — set only when the task
    actually finished ok/idle. Starvation detection must read last_success:
    repos-sync timed out 19/19 runs while its fresh last_run kept the
    watermark report saying 'all channels within expected cadence'.
    """
    last_run: int = 0
    circuit: CircuitState = field(default_factory=CircuitState)
    engagement_rate: float = -1.0   # -1 = not enough data
    effective_interval: int = 0     # 0 = use default from HEARTBEAT.md
    last_success: int = 0           # epoch of last real ok/idle finish
    last_status: str = ""           # ok|idle|failed|parse_failed|timeout|pre_timeout|pre_error|empty_pre|killed

    def to_dict(self) -> dict:
        d = {"last_run": self.last_run}
        # Only persist non-default fields to keep state.json clean
        if self.circuit.total_runs > 0:
            d["circuit"] = self.circuit.to_dict()
        if self.engagement_rate >= 0:
            d["engagement_rate"] = self.engagement_rate
        if self.effective_interval > 0:
            d["effective_interval"] = self.effective_interval
        if self.last_success > 0:
            d["last_success"] = self.last_success
        if self.last_status:
            d["last_status"] = self.last_status
        return d

    @classmethod
    def from_dict(cls, d: dict) -> TaskState:
        ts = cls()
        ts.last_run = d.get("last_run", 0)
        if "circuit" in d:
            ts.circuit = CircuitState.from_dict(d["circuit"])
        ts.engagement_rate = d.get("engagement_rate", -1.0)
        ts.effective_interval = d.get("effective_interval", 0)
        ts.last_success = d.get("last_success", 0)
        ts.last_status = d.get("last_status", "")
        return ts


# ── Backward compatibility ───────────────────────────────────────────

def upgrade_state(state: dict) -> dict:
    """Ensure each task's state dict is compatible with TaskState.

    Old format: {"task-name": {"last_run": 12345}}
    New format: {"task-name": {"last_run": 12345, "circuit": {...}, ...}}

    This is a no-op upgrade: old dicts are valid input to TaskState.from_dict().
    """
    return state  # TaskState.from_dict handles missing keys gracefully
