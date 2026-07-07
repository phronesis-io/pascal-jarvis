"""Tests for core.sched_events — the scheduler event log (replay trail).

Covers: event writes from the real scheduler paths (spawn/finish/timeout/
skip/flush), the never-raise contract on write failure, size rotation, and
the query/timeline replay entry point. All file I/O goes through tmp_path —
the conftest guard forbids touching live repo state.
"""

import fcntl
import json
import subprocess
import time
from pathlib import Path

import pytest

import core.heartbeat_loop as hbl
import core.sched_events as se
from core.heartbeat import HeartbeatRunner
from core.heartbeat_loop import _flush_night_queue, _queue_for_morning
from core.sched_events import SCHED_EVENT_FILE, emit, format_timeline, main, query


def _read_events(jarvis_dir: Path) -> list[dict]:
    path = jarvis_dir / SCHED_EVENT_FILE
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


# ── emit: basic writes ────────────────────────────────────────────────

def test_emit_writes_required_fields(tmp_path):
    emit(tmp_path, "task_spawn", task="daily-plan", run_id="abc123", batch=2)
    events = _read_events(tmp_path)
    assert len(events) == 1
    e = events[0]
    assert e["event"] == "task_spawn"
    assert e["task"] == "daily-plan"
    assert e["run_id"] == "abc123"
    assert e["batch"] == 2
    # ts is local time with second precision, parseable
    time.strptime(e["ts"], se.TS_FMT)


def test_emit_appends_one_line_per_event(tmp_path):
    emit(tmp_path, "task_spawn", task="a", run_id="r1")
    emit(tmp_path, "task_finish", task="a", run_id="r1", status="ok", duration_s=1.5)
    events = _read_events(tmp_path)
    assert [e["event"] for e in events] == ["task_spawn", "task_finish"]
    assert events[1]["status"] == "ok"


# ── emit: never-raise contract ───────────────────────────────────────

def test_emit_never_raises_on_unwritable_path(tmp_path, capsys):
    # A directory squatting on the log filename makes open(..., "a") fail.
    (tmp_path / SCHED_EVENT_FILE).mkdir()
    emit(tmp_path, "task_spawn", task="x", run_id="r")  # must not raise
    assert "sched_events" in capsys.readouterr().err


def test_emit_never_raises_when_dir_missing(tmp_path, capsys):
    emit(tmp_path / "nonexistent" / "deep", "task_spawn", task="x")  # must not raise
    assert "sched_events" in capsys.readouterr().err


def test_cycle_survives_broken_event_log(tmp_path, monkeypatch):
    """A failing event log must not affect the scheduling main path."""
    runner = _make_runner(tmp_path, "### t\n- interval: 1h\n- prompt: hi\n")
    (runner.jarvis_dir / SCHED_EVENT_FILE).mkdir()  # every emit() will fail
    monkeypatch.setattr(runner, "claude_call", lambda p: "real output")
    assert runner.run_cycle(force=True) == "real output"


# ── emit: size rotation ──────────────────────────────────────────────

def test_rotation_keeps_one_generation(tmp_path, monkeypatch):
    monkeypatch.setattr(se, "MAX_BYTES", 150)
    for i in range(10):
        emit(tmp_path, "task_spawn", task=f"task-{i}", run_id="r")
    live = tmp_path / SCHED_EVENT_FILE
    rotated = tmp_path / (SCHED_EVENT_FILE + ".1")
    assert rotated.exists()
    # Size guard: the live file never grows past threshold + one entry
    assert live.stat().st_size <= 150 + 200
    # The newest event always lands in the live file (rotation drops only
    # the OLDEST generation — one .1 backup is kept by design)
    assert _read_events(tmp_path)[-1]["task"] == "task-9"
    # query() replays live + rotated generations, in chronological order
    tasks = [e["task"] for e in query(tmp_path)]
    assert tasks == sorted(tasks, key=lambda t: int(t.split("-")[1]))
    assert "task-9" in tasks


def test_rotation_skipped_while_another_writer_holds_lock(tmp_path, monkeypatch):
    """Concurrent-rotation guard: emit() has multi-process writers (resident
    loop delivery emits, bot.sh rotation-path cycle, overlap_lock skips), and
    a double rotation would clobber the fresh `.1` archive with a near-empty
    live file. While another writer holds the rotation lock, rotation is
    skipped — but the event itself must still be appended."""
    monkeypatch.setattr(se, "MAX_BYTES", 10)
    live = tmp_path / SCHED_EVENT_FILE
    live.write_text(json.dumps({"ts": "2026-06-12 08:00:00", "event": "task_spawn",
                                "task": "old", "run_id": "r"}) + "\n")
    assert live.stat().st_size > 10
    with open(tmp_path / (SCHED_EVENT_FILE + ".lock"), "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        emit(tmp_path, "task_spawn", task="new", run_id="r2")
    assert not (tmp_path / (SCHED_EVENT_FILE + ".1")).exists()  # no rotation
    assert [e["task"] for e in _read_events(tmp_path)] == ["old", "new"]


# ── query / replay ───────────────────────────────────────────────────

def _seed(tmp_path):
    rows = [
        {"ts": "2026-06-12 08:12:59", "event": "task_spawn", "task": "daily-plan", "run_id": "68e08db6"},
        {"ts": "2026-06-12 08:13:41", "event": "task_finish", "task": "daily-plan", "run_id": "68e08db6",
         "status": "ok", "duration_s": 42.0},
        {"ts": "2026-06-12 08:13:41", "event": "task_skip", "task": "daily-plan", "run_id": "",
         "reason": "queued_quiet_hours"},
        {"ts": "2026-06-12 10:00:07", "event": "batch_flush", "task": "", "run_id": "", "count": 3},
        {"ts": "2026-06-12 11:00:00", "event": "task_spawn", "task": "checkin", "run_id": "deadbeef"},
    ]
    with open(tmp_path / SCHED_EVENT_FILE, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def test_query_filters_by_task(tmp_path):
    _seed(tmp_path)
    events = query(tmp_path, task="daily-plan")
    assert len(events) == 3
    assert {e["event"] for e in events} == {"task_spawn", "task_finish", "task_skip"}


def test_query_filters_by_event(tmp_path):
    _seed(tmp_path)
    events = query(tmp_path, event="batch_flush")
    assert len(events) == 1
    assert events[0]["count"] == 3


def test_query_time_window_prefix_semantics(tmp_path):
    _seed(tmp_path)
    # since includes 08:13:41 (>= "...08:13"); until is minute-inclusive
    events = query(tmp_path, since="2026-06-12 08:13", until="2026-06-12 10:00")
    assert [e["ts"] for e in events] == [
        "2026-06-12 08:13:41", "2026-06-12 08:13:41", "2026-06-12 10:00:07"]


def test_query_matches_comma_separated_multi_source_task(tmp_path):
    with open(tmp_path / SCHED_EVENT_FILE, "w") as f:
        f.write(json.dumps({"ts": "2026-06-12 09:00:00", "event": "task_skip",
                            "task": "checkin,daily-plan", "run_id": "",
                            "reason": "duplicate_send"}) + "\n")
    assert len(query(tmp_path, task="daily-plan")) == 1
    assert len(query(tmp_path, task="checkin")) == 1
    assert query(tmp_path, task="other") == []


def test_query_reads_rotated_generation_first(tmp_path):
    (tmp_path / (SCHED_EVENT_FILE + ".1")).write_text(
        json.dumps({"ts": "2026-06-11 23:00:00", "event": "task_spawn",
                    "task": "old", "run_id": "r0"}) + "\n")
    _seed(tmp_path)
    events = query(tmp_path)
    assert events[0]["task"] == "old"  # rotated file comes first → chronological
    assert len(events) == 6


def test_query_skips_malformed_lines(tmp_path):
    with open(tmp_path / SCHED_EVENT_FILE, "w") as f:
        f.write("{broken json\n")
        f.write(json.dumps({"ts": "2026-06-12 08:00:00", "event": "task_spawn",
                            "task": "a", "run_id": "r"}) + "\n")
    assert len(query(tmp_path)) == 1


def test_query_missing_file_returns_empty(tmp_path):
    assert query(tmp_path) == []


def test_cli_prints_timeline(tmp_path, capsys):
    _seed(tmp_path)
    assert main(["--dir", str(tmp_path), "--task", "daily-plan"]) == 0
    out = capsys.readouterr().out
    assert "08:12:59" in out
    assert "task_spawn" in out
    assert "run=68e08db6" in out
    assert "checkin" not in out


def test_cli_limit_and_empty_result(tmp_path, capsys):
    _seed(tmp_path)
    main(["--dir", str(tmp_path), "--limit", "1"])
    out = capsys.readouterr().out.strip()
    assert len(out.splitlines()) == 1
    assert "checkin" in out
    main(["--dir", str(tmp_path), "--task", "no-such-task"])
    assert "no matching" in capsys.readouterr().out


def test_format_timeline_inlines_extra_fields():
    line = format_timeline([{"ts": "2026-06-12 10:00:07", "event": "batch_flush",
                             "task": "", "run_id": "", "count": 3,
                             "sources": ["a", "b"]}])
    assert "count=3" in line
    assert 'sources=["a", "b"]' in line


# ── scheduler integration: real write points ─────────────────────────

def _make_runner(tmp_path, heartbeat_content: str, **kwargs) -> HeartbeatRunner:
    hb = tmp_path / "HEARTBEAT.md"
    hb.write_text(heartbeat_content)
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir(exist_ok=True)
    jarvis_dir = tmp_path / "jarvis"
    jarvis_dir.mkdir(exist_ok=True)
    kwargs.setdefault("idle_judge", False)
    return HeartbeatRunner(
        jarvis_dir=jarvis_dir,
        heartbeat_file=hb,
        state_file=tmp_path / "state.json",
        memory_dir=memory_dir,
        model="sonnet",
        **kwargs,
    )


def test_cycle_emits_spawn_and_finish_ok(tmp_path, monkeypatch):
    runner = _make_runner(tmp_path, "### t\n- interval: 1h\n- prompt: hi\n")
    monkeypatch.setattr(runner, "claude_call", lambda p: "hello Pascal")
    assert runner.run_cycle(force=True) == "hello Pascal"
    events = _read_events(runner.jarvis_dir)
    spawn = [e for e in events if e["event"] == "task_spawn"]
    finish = [e for e in events if e["event"] == "task_finish"]
    assert spawn and finish
    assert spawn[0]["task"] == "t"
    assert finish[0]["status"] == "ok"
    assert finish[0]["duration_s"] >= 0
    # run_id correlates spawn↔finish and is the cycle id
    assert spawn[0]["run_id"] == finish[0]["run_id"] != ""


def test_cycle_emits_idle_finish_on_heartbeat_ok(tmp_path, monkeypatch):
    runner = _make_runner(tmp_path, "### t\n- interval: 1h\n- prompt: hi\n")
    monkeypatch.setattr(runner, "claude_call", lambda p: "HEARTBEAT_OK")
    runner.run_cycle(force=True)
    finish = [e for e in _read_events(runner.jarvis_dir) if e["event"] == "task_finish"]
    assert finish[0]["status"] == "idle"


def test_cycle_emits_task_timeout(tmp_path, monkeypatch):
    """A TimeoutExpired in the real claude_call surfaces as task_timeout."""
    runner = _make_runner(tmp_path, "### t\n- interval: 1h\n- prompt: hi\n")

    def fake_run(*a, **kw):
        raise subprocess.TimeoutExpired(cmd="claude", timeout=1)
    import core.heartbeat as hb_mod
    monkeypatch.setattr(hb_mod.subprocess, "run", fake_run)
    runner.run_cycle(force=True)
    events = _read_events(runner.jarvis_dir)
    timeouts = [e for e in events if e["event"] == "task_timeout"]
    assert len(timeouts) == 1
    assert timeouts[0]["task"] == "t"
    assert timeouts[0]["timeout_s"] == runner.claude_timeout
    assert not [e for e in events if e["event"] == "task_finish"]


def test_cycle_emits_failed_finish_on_empty_response(tmp_path, monkeypatch):
    runner = _make_runner(tmp_path, "### t\n- interval: 1h\n- prompt: hi\n")
    monkeypatch.setattr(runner, "claude_call", lambda p: "")
    runner.run_cycle(force=True)
    finish = [e for e in _read_events(runner.jarvis_dir) if e["event"] == "task_finish"]
    assert finish[0]["status"] == "failed"
    assert not [e for e in _read_events(runner.jarvis_dir) if e["event"] == "task_timeout"]


def test_silent_task_output_logged_as_skip(tmp_path, monkeypatch):
    """SILENT_TASKS suppression leaves a task_skip(silent_output) trail."""
    runner = _make_runner(tmp_path, "### daily-plan\n- interval: 1h\n- prompt: plan\n")
    monkeypatch.setattr(runner, "claude_call", lambda p: "🌅 plan card")
    assert runner.run_cycle(force=True) == ""  # suppressed
    skips = [e for e in _read_events(runner.jarvis_dir) if e["event"] == "task_skip"]
    assert [(e["task"], e["reason"]) for e in skips] == [("daily-plan", "silent_output")]


def test_empty_pre_script_emits_skip(tmp_path, monkeypatch):
    runner = _make_runner(
        tmp_path, "### t\n- interval: 1h\n- pre: tasks/x.sh\n- prompt: hi\n")
    monkeypatch.setattr(runner, "run_script", lambda *a, **kw: "")
    runner.run_cycle(force=True)
    skips = [e for e in _read_events(runner.jarvis_dir) if e["event"] == "task_skip"]
    assert skips[0]["task"] == "t"
    assert skips[0]["reason"] == "empty_pre"


def test_overlap_lock_emits_skip(tmp_path, monkeypatch):
    runner = _make_runner(tmp_path, "### t\n- interval: 1h\n- prompt: hi\n")
    lock_path = runner.state_file.with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert runner.run_cycle(force=True, only_task="t") == ""
    skips = [e for e in _read_events(runner.jarvis_dir) if e["event"] == "task_skip"]
    assert skips[0]["reason"] == "overlap_lock"
    assert skips[0]["task"] == "t"


# ── delivery-layer integration: queue + flush ────────────────────────

def test_queue_for_morning_emits_queued_skip(tmp_path, monkeypatch):
    monkeypatch.setattr(hbl, "_in_quiet_hours", lambda *a, **kw: True)
    (tmp_path / ".heartbeat_last_source").write_text("content-recommend")
    _queue_for_morning("late-night note", tmp_path)
    skips = [e for e in _read_events(tmp_path) if e["event"] == "task_skip"]
    assert skips[0]["task"] == "content-recommend"
    assert skips[0]["reason"] == "queued_quiet_hours"
    assert (tmp_path / hbl.NIGHT_QUEUE_FILE).exists()


def test_queue_for_morning_silent_drop_emits_skip(tmp_path):
    (tmp_path / ".heartbeat_last_source").write_text("daily-plan")
    _queue_for_morning("🌅 plan card", tmp_path)
    skips = [e for e in _read_events(tmp_path) if e["event"] == "task_skip"]
    assert skips[0]["reason"] == "silent_output"
    assert not (tmp_path / hbl.NIGHT_QUEUE_FILE).exists()


def test_flush_emits_batch_flush_with_count(tmp_path, monkeypatch):
    # fresh ts so the backlog-#4 age expiry doesn't eat the entries first
    ts = hbl.now_local_str("%Y-%m-%d %H:%M")
    queue = tmp_path / hbl.NIGHT_QUEUE_FILE
    with open(queue, "w") as f:
        f.write(json.dumps({"ts": ts, "text": "a", "source": "checkin"}) + "\n")
        f.write(json.dumps({"ts": ts, "text": "b", "source": "content-recommend"}) + "\n")
    monkeypatch.setattr(hbl, "_lark_send_text", lambda t, u: True)
    assert _flush_night_queue(tmp_path, "ou_test")
    flushes = [e for e in _read_events(tmp_path) if e["event"] == "batch_flush"]
    assert len(flushes) == 1
    assert flushes[0]["count"] == 2
    assert flushes[0]["sources"] == ["checkin", "content-recommend"]


def test_failed_flush_emits_no_batch_flush(tmp_path, monkeypatch):
    queue = tmp_path / hbl.NIGHT_QUEUE_FILE
    queue.write_text(json.dumps({"ts": hbl.now_local_str("%Y-%m-%d %H:%M"),
                                 "text": "a", "source": "checkin"}) + "\n")
    monkeypatch.setattr(hbl, "_lark_send_text", lambda t, u: False)
    monkeypatch.setattr(hbl.time, "sleep", lambda s: None)
    assert _flush_night_queue(tmp_path, "ou_test") == hbl.FLUSH_RETRYABLE
    assert [e for e in _read_events(tmp_path) if e["event"] == "batch_flush"] == []
