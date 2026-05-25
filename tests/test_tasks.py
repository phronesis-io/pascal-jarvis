"""Tests for core.tasks — TaskManager CRUD, lifecycle, capacity, praxis."""

import json
from pathlib import Path

import pytest

from core.tasks import TaskManager


def _tm(tmp_path) -> TaskManager:
    (tmp_path / "system").mkdir()
    return TaskManager(tmp_path)


def test_capture_creates_task(tmp_path):
    tm = _tm(tmp_path)
    t = tm.capture("Write report", type="poiesis", energy="high", time_est_min=60)
    assert t["title"] == "Write report"
    assert t["status"] == "inbox"
    assert t["type"] == "poiesis"
    assert t["energy"] == "high"
    assert t["time_est_min"] == 60
    assert t["resolution"] is None


def test_capture_assigns_unique_ids(tmp_path):
    tm = _tm(tmp_path)
    t1 = tm.capture("task 1")
    t2 = tm.capture("task 2")
    assert t1["id"] != t2["id"]


def test_commit(tmp_path):
    tm = _tm(tmp_path)
    t = tm.capture("do something")
    assert tm.commit(t["id"])
    tasks = tm.active()
    assert tasks[0]["status"] == "committed"


def test_done(tmp_path):
    tm = _tm(tmp_path)
    t = tm.capture("finish this")
    assert tm.done(t["id"])
    active = tm.active()
    assert len(active) == 0  # done tasks are not active


def test_reject(tmp_path):
    tm = _tm(tmp_path)
    t = tm.capture("nope")
    assert tm.reject(t["id"], reason="not relevant")
    active = tm.active()
    assert len(active) == 0


def test_defer(tmp_path):
    tm = _tm(tmp_path)
    t = tm.capture("later")
    assert tm.defer(t["id"], "2026-12-31")
    tasks = tm.inbox()
    assert tasks[0]["when"] == "2026-12-31"


def test_decay(tmp_path):
    tm = _tm(tmp_path)
    t = tm.capture("stale task")
    tm.touch(t["id"])
    tm.touch(t["id"])
    tm.touch(t["id"])
    assert tm.ready_to_decay(threshold=3)
    assert tm.decay(t["id"], reason="stale")
    assert len(tm.active()) == 0


def test_inbox_only_returns_inbox(tmp_path):
    tm = _tm(tmp_path)
    t1 = tm.capture("inbox task")
    t2 = tm.capture("committed task")
    tm.commit(t2["id"])
    inbox = tm.inbox()
    assert len(inbox) == 1
    assert inbox[0]["id"] == t1["id"]


def test_today_capacity(tmp_path):
    tm = _tm(tmp_path)
    t = tm.capture("meeting prep", time_est_min=120)
    tm.commit(t["id"])
    cap = tm.today_capacity()
    assert cap["committed_min"] == 120
    assert cap["remaining"] == 300 - 120  # DAILY_BUDGET_MIN = 300


def test_stale_inbox(tmp_path, monkeypatch):
    tm = _tm(tmp_path)
    t = tm.capture("old task")
    # Manually set created time to 3 days ago (use now_local for consistency)
    tasks = tm._load_tasks()
    from core.timeutil import now_local
    from datetime import timedelta
    tasks[0]["created"] = (now_local() - timedelta(hours=72)).isoformat()
    tm._save_tasks(tasks)
    stale = tm.stale_inbox(hours=48)
    assert len(stale) == 1


def test_praxis_lifecycle(tmp_path):
    tm = _tm(tmp_path)
    px = tm.praxis_add("Stretch", frequency="daily", preferred_time="08:00", duration_min=15)
    assert px["streak_current"] == 0
    tm.praxis_done(px["id"])
    items = tm.praxis_list()
    assert items[0]["streak_current"] == 1
    assert items[0]["streak_best"] == 1
    assert tm.praxis_remove(px["id"])
    assert len(tm.praxis_list()) == 0


def test_praxis_done_idempotent_same_day(tmp_path):
    tm = _tm(tmp_path)
    px = tm.praxis_add("Meditate")
    tm.praxis_done(px["id"])
    tm.praxis_done(px["id"])  # same day — should not increment
    items = tm.praxis_list()
    assert items[0]["streak_current"] == 1


def test_invalid_state_transitions(tmp_path):
    tm = _tm(tmp_path)
    t = tm.capture("task")
    tm.done(t["id"])
    # Can't commit a done task
    assert not tm.commit(t["id"])
    # Can't reject a resolved task
    assert not tm.reject(t["id"])
