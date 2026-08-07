"""The 3-day self-improve cycle: gate discipline and spawn hygiene.

Owner authorization 2026-08-07:「你可以自己定时每几天根据你给我提供的价值，
进行进步」. The heartbeat only hosts the schedule — these tests pin that the
gate can't double-fire, can't overlap a live round, and that a crash after
spawn can't turn into a spawn loop (stamp is written in the same call).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import core.self_improve_cycle as sic  # noqa: E402


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(sic, "JARVIS_DIR", tmp_path)
    monkeypatch.setattr(sic, "WORK_DIR", tmp_path)
    monkeypatch.setattr(sic, "LOG_FILE", str(tmp_path / "cycle.log"))
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "self_improve_prompt.md").write_text(
        "self improve — 按价值数据挖题", encoding="utf-8")
    yield


def _stamp(tmp_path, spawned_at, pid=0):
    (tmp_path / "data").mkdir(exist_ok=True)
    (tmp_path / "data" / "self_improve_cycle.json").write_text(
        json.dumps({"spawned_at": spawned_at, "pid": pid}), encoding="utf-8")


def test_due_when_never_spawned():
    assert sic.due(now_epoch=1_000_000) is True


def test_not_due_inside_the_three_day_window(tmp_path):
    _stamp(tmp_path, 1_000_000)
    assert sic.due(now_epoch=1_000_000 + sic.CYCLE_S - 60) is False
    assert sic.due(now_epoch=1_000_000 + sic.CYCLE_S + 60) is True


def test_not_due_while_previous_round_is_alive(tmp_path):
    # Our own pid is definitely alive — a live round blocks a new spawn even
    # long past the window (no overlapping self-improve sessions).
    _stamp(tmp_path, 0, pid=os.getpid())
    assert sic.due(now_epoch=10 * sic.CYCLE_S) is False


def test_spawn_stamps_and_detaches(tmp_path):
    calls = []

    def popen(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(pid=777)

    pid = sic.spawn(popen=popen, now_epoch=2_000_000)
    assert pid == 777
    (argv, kwargs), = calls
    assert "--dangerously-skip-permissions" in argv
    assert argv[-2] == "-p" and "价值数据" in argv[-1]
    assert kwargs["start_new_session"] is True
    assert kwargs["cwd"] == str(tmp_path)
    state = json.loads(
        (tmp_path / "data" / "self_improve_cycle.json").read_text())
    assert state == {"spawned_at": 2_000_000, "pid": 777}
    # Stamp written → immediately not due again (crash-loop guard).
    assert sic.due(now_epoch=2_000_100) is False


def test_missing_prompt_spawns_nothing(tmp_path):
    (tmp_path / "scripts" / "self_improve_prompt.md").unlink()
    called = []
    assert sic.spawn(popen=lambda *a, **k: called.append(1)) == 0
    assert called == []


def test_heartbeat_registers_the_cycle_task():
    from core.heartbeat import parse_heartbeat
    tasks = {t["name"]: t for t in parse_heartbeat(ROOT / "HEARTBEAT.md")}
    task = tasks["self-improve-cycle"]
    assert task["pre"] == "tasks/self_improve_cycle_pre.sh"
    assert task["interval"] == 12 * 3600
