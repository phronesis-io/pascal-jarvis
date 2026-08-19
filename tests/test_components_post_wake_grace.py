"""Post-wake grace is one window, read by every age-derived verdict.

2026-08-18/19 the MacBook was shut for ~39h (38 sleep/wake gaps, 39.4h total
by daemon.log). `heartbeat-tasks` read the daemon's persisted grace window and
correctly held green. Every other age-based check in this module did not know
the host had been asleep, so `ef-stream` — process alive and owned the whole
time — went critical on "protocol health stale". That became the ONLY alert
that reached Pascal in 39 hours, and it named the wrong thing: it said
EigenFlux was not running when the truth was that the machine was not awake.

These tests pin the shared window: staleness is excused while the daemon
holds, liveness and real failures never are, and the hold expires.
"""

import json
import os
import sqlite3
import time
from pathlib import Path

import pytest

from core import components as C


def _brain(root: Path, grace_until: float) -> Path:
    (root / ".daemon_brain_state.json").write_text(
        json.dumps({"grace_until": grace_until}))
    return root


# ── the incident ────────────────────────────────────────────────────

def _ef_root(tmp_path: Path, age_s: float) -> Path:
    (tmp_path / "data").mkdir(exist_ok=True)
    (tmp_path / "data" / "ef_stream_health.json").write_text(json.dumps({
        "status": "active", "quiet_streak": 0, "failures": 0,
        "updated_epoch": time.time() - age_s,
    }))
    return tmp_path


EF_COMP = {"name": "ef-stream", "check": "ef_stream", "critical": True,
           "path": "data/ef_stream_health.json", "max_age_seconds": 2400}


@pytest.fixture
def live_process(monkeypatch):
    """The 8/18 reading: the stream process was alive and owned throughout."""
    monkeypatch.setattr(C, "_check_pgrep",
                        lambda comp, root: (True, "pids ['65554'] owned by 65484"))


def test_ef_stream_stale_only_because_the_host_slept_is_not_critical(
        tmp_path, live_process):
    root = _brain(_ef_root(tmp_path, age_s=8.6 * 3600), time.time() + 19 * 60)
    ok, detail = C._check_ef_stream(EF_COMP, root)

    assert ok is True
    assert "grace" in detail
    assert "stale" not in detail          # do not page for a closed lid
    assert "owned by" in detail           # still says what it actually saw


def test_ef_stream_genuinely_stale_on_an_awake_host_still_pages(
        tmp_path, live_process):
    """Without a grace window the incident reading must stay critical —
    the fix must not turn a real stream outage green."""
    ok, detail = C._check_ef_stream(EF_COMP, _ef_root(tmp_path, age_s=8.6 * 3600))

    assert ok is False
    assert "stale" in detail


def test_grace_expires(tmp_path, live_process):
    """Bounded hold: a component still stale 30min after the host is back is
    stuck, not sleeping."""
    root = _brain(_ef_root(tmp_path, age_s=8.6 * 3600), time.time() - 1)
    ok, _ = C._check_ef_stream(EF_COMP, root)

    assert ok is False


# ── grace covers staleness, never liveness or real failure ──────────

def test_a_process_that_died_during_sleep_is_still_dead_on_wake(
        tmp_path, monkeypatch):
    monkeypatch.setattr(C, "_check_pgrep",
                        lambda comp, root: (False, "no process matching pattern"))
    root = _brain(_ef_root(tmp_path, age_s=8.6 * 3600), time.time() + 19 * 60)
    ok, detail = C._check_ef_stream(EF_COMP, root)

    assert ok is False
    assert "no process" in detail


def _delivery_root(tmp_path: Path, rows: list[tuple]) -> Path:
    (tmp_path / "data").mkdir(exist_ok=True)
    db = sqlite3.connect(tmp_path / "data" / "jarvis.db")
    db.execute("CREATE TABLE delivery_envelopes (state TEXT, attempts INT, "
               "created_epoch REAL, next_attempt_epoch REAL)")
    db.executemany("INSERT INTO delivery_envelopes VALUES (?,?,?,?)", rows)
    db.commit()
    db.close()
    return tmp_path


DELIVERY_COMP = {"name": "unified-delivery", "check": "delivery",
                 "path": "data/jarvis.db"}


def test_a_queue_that_went_overdue_during_sleep_is_held(tmp_path):
    now = time.time()
    root = _delivery_root(tmp_path, [("queued", 1, now - 300, now - 30 * 3600)])
    _brain(root, now + 19 * 60)
    ok, detail = C._check_delivery(DELIVERY_COMP, root)

    assert ok is True
    assert "grace" in detail
    assert "due item(s)" in detail        # the backlog is still reported


def test_a_delivery_failure_streak_is_never_excused_by_sleep(tmp_path):
    """Envelopes that were ATTEMPTED and failed are evidence the host was
    awake enough to try. Grace must not swallow the 8/17 keychain outage."""
    now = time.time()
    root = _delivery_root(tmp_path, [
        ("failed", 3, now - 60, 0), ("failed", 3, now - 120, 0),
        ("failed", 3, now - 180, 0),
    ])
    _brain(root, now + 19 * 60)
    ok, detail = C._check_delivery(DELIVERY_COMP, root)

    assert ok is False
    assert "unavailable" in detail


# ── the same window for the other age-based checks ──────────────────

def test_file_age_reads_the_same_window(tmp_path):
    now = time.time()
    stamp = tmp_path / "backup.stamp"
    stamp.write_text("x")
    os.utime(stamp, (now - 50 * 3600, now - 50 * 3600))
    comp = {"path": "backup.stamp", "max_age_hours": 48}

    assert C._check_file_age(comp, tmp_path)[0] is False

    _brain(tmp_path, now + 19 * 60)
    ok, detail = C._check_file_age(comp, tmp_path)

    assert ok is True
    assert "grace" in detail
    assert "50.0h" in detail               # the real age stays visible


def test_audit_age_reads_the_same_window(tmp_path):
    now = time.time()
    from core.conversation_audit import connect
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(now - 50 * 3600))
    db = connect(tmp_path / "conversation_audit.db")
    db.execute("INSERT INTO audit_runs (started_at, since, completed_at) "
               "VALUES (?,?,?)", (stamp, stamp, stamp))
    db.commit()
    db.close()
    comp = {"path": "conversation_audit.db", "max_age_hours": 48}

    assert C._check_audit_age(comp, tmp_path)[0] is False

    _brain(tmp_path, now + 19 * 60)
    ok, detail = C._check_audit_age(comp, tmp_path)

    assert ok is True
    assert "grace" in detail


def test_no_brain_state_means_no_grace(tmp_path, live_process):
    """A fresh install with no daemon ledger must not be silently graced."""
    assert C._post_wake_grace(tmp_path) is None


# ── the alert must not diagnose what it did not check ────────────────

def test_a_running_but_silent_component_is_not_reported_as_not_running(
        tmp_path, live_process):
    """2026-08-18 02:16, the only alert that reached Pascal in 39 hours:
    「⚠️ 组件失联：EigenFlux 实时消息接收没有在运行。」 The process was alive
    and owned throughout — the daemon flattened every red into that one
    sentence. Restarting on it would have been wasted work."""
    import daemon

    ok, detail = C._check_ef_stream(EF_COMP, _ef_root(tmp_path, age_s=8.6 * 3600))
    assert ok is False
    assert C.ALIVE_BUT_SILENT in detail

    text = daemon._component_down_text("EigenFlux 实时消息接收", detail)
    assert "没有在运行" not in text
    assert "进程还活着" in text


def test_a_genuinely_dead_component_still_reads_as_down(tmp_path, monkeypatch):
    import daemon

    monkeypatch.setattr(C, "_check_pgrep",
                        lambda comp, root: (False, "no process matching pattern"))
    ok, detail = C._check_ef_stream(EF_COMP, _ef_root(tmp_path, age_s=60))
    assert ok is False
    assert C.ALIVE_BUT_SILENT not in detail

    text = daemon._component_down_text("EigenFlux 实时消息接收", detail)
    assert "组件失联" in text and "没有在运行" in text
