"""Manifest-level heartbeat TASK health (2026-07-27 false-green incident).

Between 05:58 and 09:59 on 2026-07-27 the heartbeat process was alive, so the
`pgrep` check on heartbeat-loop was green and `python3 -m core.components`
reported 15/15 healthy — while activity-log failed every run, intention-check
had been wedged 11h, and memory-tidy / self-diagnostic had gone 16h / 15h
without a success. The daemon's brain-health path paged correctly; the
manifest, which PRODUCT.md counts on for "silent component outage duration",
never saw it.

These tests pin: the incident is caught, a healthy scheduler stays green,
post-wake grace is honored (the daemon's own false-positive guard), and the
check never writes the daemon's brain-state ledger.
"""

import json
import time
from pathlib import Path

from core.components import check_components

HOUR = 3600.0

REPO = Path(__file__).resolve().parent.parent


def _manifest(tmp_path: Path) -> Path:
    p = tmp_path / "components.yaml"
    p.write_text(
        "components:\n"
        "  - name: heartbeat-tasks\n"
        "    check: heartbeat_tasks\n"
        "    path: heartbeat_state.json\n"
        "    critical: false\n"
        "    requires_file: heartbeat_state.json\n"
    )
    return p


def _root(tmp_path: Path, state: dict, *, brain: dict | None = None,
          heartbeat_md: str | None = None) -> Path:
    """A synthetic Jarvis root. HEARTBEAT.md is copied from the repo so the
    test exercises the real task/interval table rather than a toy one."""
    (tmp_path / "heartbeat_state.json").write_text(json.dumps(state))
    (tmp_path / "HEARTBEAT.md").write_text(
        heartbeat_md if heartbeat_md is not None
        else (REPO / "HEARTBEAT.md").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    if brain is not None:
        (tmp_path / ".daemon_brain_state.json").write_text(json.dumps(brain))
    return tmp_path


def _ts(*, last_run, last_success, last_status="ok",
        total_failures=0, total_runs=0, consecutive_failures=0):
    return {
        "last_run": last_run,
        "last_success": last_success,
        "last_status": last_status,
        "circuit": {
            "total_failures": total_failures,
            "total_runs": total_runs,
            "disabled_until": 0,
            "consecutive_failures": consecutive_failures,
        },
    }


def _check(root: Path, manifest: Path) -> dict:
    (result,) = check_components(manifest_path=manifest, root=root)
    return result


# ── The incident ─────────────────────────────────────────────────────


def test_morning_2026_07_27_stall_is_no_longer_green(tmp_path):
    """The exact shape of the incident: process alive, tasks dead."""
    now = time.time()
    state = {
        # PRIORITY task that ran constantly and failed every time.
        "activity-log": _ts(last_run=now - 60, last_success=now - 4 * HOUR,
                            last_status="failed", consecutive_failures=9),
        # PRIORITY task wedged for 11h.
        "intention-check": _ts(last_run=now - 120, last_success=now - 11 * HOUR,
                               last_status="failed", consecutive_failures=12),
        # Non-priority starvation: 6h interval, 16h since a success.
        "memory-tidy": _ts(last_run=now - 300, last_success=now - 16 * HOUR,
                           last_status="failed"),
        # Non-priority starvation: 4h interval, 15h since a success.
        "self-diagnostic": _ts(last_run=now - 300, last_success=now - 15 * HOUR,
                               last_status="failed"),
    }
    root = _root(tmp_path, state)
    result = _check(root, _manifest(tmp_path))

    assert result["ok"] is False, "the 07-27 morning stall must not read green"
    assert "停摆" in result["detail"]
    # The operator must be told which task, not just that something is wrong.
    # Since 2026-08-24 alerts carry the shared plain-Chinese display name
    # instead of the raw task id (card-style contract).
    from core.textutil import task_display_name
    assert task_display_name("activity-log") in result["detail"]


def test_healthy_scheduler_stays_green(tmp_path):
    now = time.time()
    state = {
        "activity-log": _ts(last_run=now - 60, last_success=now - 60),
        "intention-check": _ts(last_run=now - 60, last_success=now - 60),
        "memory-tidy": _ts(last_run=now - 300, last_success=now - 300),
        "self-diagnostic": _ts(last_run=now - 300, last_success=now - 300),
    }
    result = _check(_root(tmp_path, state), _manifest(tmp_path))
    assert result["ok"] is True
    assert "none stalled" in result["detail"]


# ── False-positive guards (why this check can be trusted) ────────────


def test_post_wake_grace_is_honored(tmp_path):
    """A laptop that just woke must not read red here while the daemon holds.

    Stale last_success right after a sleep is expected, not brain-death. The
    daemon owns that decision; this check reads its persisted grace window
    instead of second-guessing it.
    """
    now = time.time()
    state = {
        "activity-log": _ts(last_run=now - 60, last_success=now - 11 * HOUR,
                            last_status="failed", consecutive_failures=12),
    }
    root = _root(tmp_path, state, brain={"grace_until": now + 20 * 60})
    result = _check(root, _manifest(tmp_path))

    assert result["ok"] is True
    assert "grace" in result["detail"]


def test_check_never_writes_the_daemon_brain_ledger(tmp_path):
    """The daemon is the only owner of brain state and the only pager."""
    now = time.time()
    brain = {"samples": {"activity-log": {"total_failures": 3, "total_runs": 3,
                                          "fail_windows": 1}},
             "last_alert": 123.0, "grace_until": 0}
    state = {"activity-log": _ts(last_run=now - 60, last_success=now - 4 * HOUR,
                                 last_status="failed", total_failures=9,
                                 total_runs=9, consecutive_failures=9)}
    root = _root(tmp_path, state, brain=brain)
    before = (root / ".daemon_brain_state.json").read_text()

    _check(root, _manifest(tmp_path))

    assert (root / ".daemon_brain_state.json").read_text() == before


def test_fresh_install_skips_instead_of_alarming(tmp_path):
    """No heartbeat_state.json yet — doctor.sh at install must not see a FAIL."""
    (tmp_path / "HEARTBEAT.md").write_text("", encoding="utf-8")
    result = _check(tmp_path, _manifest(tmp_path))
    assert result["ok"] is True and result["skipped"] is True


def test_shipped_manifest_actually_arms_the_check(tmp_path):
    """The synthetic-manifest tests above prove the checker works; this proves
    the real deployment uses it. Without this, deleting the components.yaml
    entry would restore the false green with every test still passing."""
    from core.components import load_manifest

    armed = [c for c in load_manifest() if c.get("check") == "heartbeat_tasks"]
    assert armed, "components.yaml must carry a heartbeat_tasks check"
    assert armed[0]["name"] == "heartbeat-tasks"


def test_unreadable_heartbeat_md_is_reported_not_swallowed(tmp_path):
    """An empty task table would silently make every stall invisible."""
    now = time.time()
    state = {"activity-log": _ts(last_run=now - 60, last_success=now - 60)}
    root = _root(tmp_path, state, heartbeat_md="")
    result = _check(root, _manifest(tmp_path))
    assert result["ok"] is False
    assert "HEARTBEAT.md" in result["detail"]
