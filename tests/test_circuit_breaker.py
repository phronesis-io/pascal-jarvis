"""Tests for task circuit breaker — auto-disable tasks that fail repeatedly."""

import time
from core.task_protocol import CircuitState, TaskState


def test_circuit_stays_closed_on_few_failures():
    cs = CircuitState()
    for _ in range(4):  # below threshold of 5
        cs.record_failure()
    assert not cs.is_open


def test_circuit_opens_after_threshold():
    cs = CircuitState()
    for i in range(5):
        tripped = cs.record_failure()
    assert tripped is True
    assert cs.is_open


def test_circuit_closes_after_disable_duration():
    cs = CircuitState()
    cs.disabled_until = time.time() - 1  # already expired
    assert not cs.is_open


def test_circuit_reports_remaining_seconds():
    cs = CircuitState()
    cs.disabled_until = time.time() + 100
    remaining = cs.remaining_disable_seconds
    assert 95 <= remaining <= 100


def test_success_resets_consecutive_failures():
    cs = CircuitState()
    cs.record_failure()
    cs.record_failure()
    cs.record_failure()
    assert cs.consecutive_failures == 3
    cs.record_success()
    assert cs.consecutive_failures == 0


def test_escalation_doubles_disable_time():
    cs = CircuitState()
    # First trip
    for _ in range(5):
        cs.record_failure()
    first_duration = cs.disabled_until - time.time()
    cs.disabled_until = 0  # manually reset

    # Second trip
    for _ in range(5):
        cs.record_failure()
    second_duration = cs.disabled_until - time.time()

    # Second trip should be ~2x longer
    assert second_duration > first_duration * 1.5


def test_task_state_roundtrip():
    ts = TaskState()
    ts.last_run = 12345
    ts.circuit.consecutive_failures = 3
    ts.circuit.total_runs = 10
    ts.engagement_rate = 0.75

    d = ts.to_dict()
    ts2 = TaskState.from_dict(d)

    assert ts2.last_run == 12345
    assert ts2.circuit.consecutive_failures == 3
    assert ts2.circuit.total_runs == 10
    assert ts2.engagement_rate == 0.75


def test_task_state_backward_compatible():
    """Old state format {"last_run": 12345} should still work."""
    old_state = {"last_run": 12345}
    ts = TaskState.from_dict(old_state)
    assert ts.last_run == 12345
    assert ts.circuit.consecutive_failures == 0
    assert ts.engagement_rate == -1.0


def test_task_state_minimal_serialization():
    """Fresh TaskState should serialize minimally (no circuit/engagement noise)."""
    ts = TaskState()
    ts.last_run = 100
    d = ts.to_dict()
    assert d == {"last_run": 100}  # no circuit or engagement keys
