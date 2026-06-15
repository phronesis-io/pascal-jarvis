"""Tests for core.selfmon (REQ-67) — self-monitoring from LIVE sources.

Per PRD v2 these metrics are computed from the live JSONL/state files and the
intent DB, NOT from the dead dashboard tables. Every test seeds tmp files and
a tmp sqlite DB; the real production data is never read (the tmp dir is the
jarvis_dir, and the DB path is monkeypatched).
"""

import json
import sqlite3
import time
from pathlib import Path

import pytest

from core import selfmon


# ── helpers ─────────────────────────────────────────────────────────────────

def _ts(epoch: float, sep: str = " ") -> str:
    """Local-time string matching the live-file shapes."""
    return time.strftime(f"%Y-%m-%d{sep}%H:%M:%S", time.localtime(epoch))


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows)
                    + "\n", encoding="utf-8")


@pytest.fixture
def jdir(tmp_path):
    """A fake jarvis_dir with a data/ subdir. Nothing real is touched."""
    (tmp_path / "data").mkdir()
    return tmp_path


@pytest.fixture
def seed_intent_db(jdir, monkeypatch):
    """Seed a tmp intent DB and point selfmon at it (mode=ro is preserved).

    Mirrors the monkeypatched-_get_db pattern from test_intentions.py, but
    seeds the `intentions` table by raw SQL so we can stamp closure_status /
    timestamps directly without going through create_intent's validation.
    """
    db_path = jdir / "data" / "jarvis.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE intentions (
            id TEXT PRIMARY KEY,
            name TEXT,
            closure_status TEXT,
            created_at TEXT,
            triggered_at TEXT,
            executed_at TEXT
        );
    """)
    conn.commit()
    conn.close()
    monkeypatch.setattr(selfmon, "_intent_db_path", lambda _jd: db_path)

    def _insert(iid, name, closure_status, executed_at):
        c = sqlite3.connect(str(db_path))
        c.execute(
            "INSERT INTO intentions (id, name, closure_status, created_at, "
            "triggered_at, executed_at) VALUES (?,?,?,?,?,?)",
            (iid, name, closure_status, executed_at, executed_at, executed_at))
        c.commit()
        c.close()

    return _insert


# ── metric 1: noise card count ──────────────────────────────────────────────

def test_noise_card_count_by_source(jdir):
    now = time.time()
    rows = [
        # calendar-sync: 3 sent, all ignored → low-engagement noise
        {"ts": _ts(now - 3600), "source": "calendar-sync", "type": "sent",
         "epoch": now - 3600},
        {"ts": _ts(now - 3000), "source": "calendar-sync", "type": "sent",
         "epoch": now - 3000},
        {"ts": _ts(now - 2400), "source": "calendar-sync", "type": "sent",
         "epoch": now - 2400},
        # checkin: 2 sent, 2 engaged → healthy
        {"ts": _ts(now - 1800), "source": "checkin", "type": "sent",
         "epoch": now - 1800},
        {"ts": _ts(now - 1700), "source": "checkin", "type": "response",
         "reaction": "engaged"},
        {"ts": _ts(now - 1200), "source": "checkin", "type": "sent",
         "epoch": now - 1200},
        {"ts": _ts(now - 1100), "source": "checkin", "type": "response",
         "reaction": "engaged"},
        # outside the window — must be ignored
        {"ts": _ts(now - 200000), "source": "old", "type": "sent",
         "epoch": now - 200000},
    ]
    _write_jsonl(jdir / "engagement_log.jsonl", rows)

    res = selfmon.noise_card_count(jdir, since_epoch=now - 24 * 3600,
                                   window_hours=24)
    assert res["total_sent"] == 5          # 3 + 2, old one excluded
    assert res["by_source"]["calendar-sync"]["sent"] == 3
    assert res["by_source"]["checkin"]["sent"] == 2
    # calendar-sync got zero replies → flagged; checkin fully engaged → not
    assert "calendar-sync" in res["low_engagement_sources"]
    assert "checkin" not in res["low_engagement_sources"]
    assert "old" not in res["by_source"]


# ── metric 2: same-intent re-fires (triple-nag) ─────────────────────────────

def test_same_intent_refire_detection(jdir):
    now = time.time()
    rows = []
    # int_storm: 4 fires within ~12 minutes → triple-nag offender
    for i, off in enumerate((0, 240, 480, 720)):
        rows.append({"ts": _ts(now - 3600 + off), "event": "intent_fired",
                     "task": "int_storm", "name": "小时报", "attempt": i})
    # int_calm: 2 fires far apart → not an offender
    rows.append({"ts": _ts(now - 7200), "event": "intent_fired",
                 "task": "int_calm", "name": "ok"})
    rows.append({"ts": _ts(now - 600), "event": "intent_fired",
                 "task": "int_calm", "name": "ok"})
    _write_jsonl(jdir / "sched_events.jsonl", rows)

    res = selfmon.same_intent_refires(jdir, since_epoch=now - 24 * 3600)
    assert res["offender_count"] == 1
    assert "int_storm" in res["offenders"]
    assert res["offenders"]["int_storm"]["max_in_window"] >= selfmon.REFIRE_MIN_COUNT
    assert res["offenders"]["int_storm"]["name"] == "小时报"
    assert "int_calm" not in res["offenders"]


def test_same_intent_refire_counts_retries(jdir):
    """intent_retry counts toward the storm, not just intent_fired."""
    now = time.time()
    rows = [{"ts": _ts(now - 1800 + i * 60), "event": "intent_retry",
             "task": "int_cron", "name": "cron job", "kind": "cron"}
            for i in range(5)]
    _write_jsonl(jdir / "sched_events.jsonl", rows)
    res = selfmon.same_intent_refires(jdir, since_epoch=now - 24 * 3600)
    assert "int_cron" in res["offenders"]


# ── metric 3: closure overdue (seeded DB, mode=ro) ──────────────────────────

def test_closure_overdue_from_seeded_db(jdir, seed_intent_db):
    now = time.time()
    # awaiting & old (5 days) → zombie
    seed_intent_db("int_old", "小明 dinner", "awaiting",
                   _ts(now - 5 * 86400, sep="T"))
    # awaiting & fresh (1 day) → not overdue yet
    seed_intent_db("int_fresh", "fresh ask", "awaiting",
                   _ts(now - 1 * 86400, sep="T"))
    # done → never overdue regardless of age
    seed_intent_db("int_done", "closed", "done",
                   _ts(now - 10 * 86400, sep="T"))

    res = selfmon.closure_overdue(jdir, now=now, overdue_days=3)
    assert res["db_available"] is True
    assert res["overdue_count"] == 1
    ids = {o["id"] for o in res["overdue"]}
    assert ids == {"int_old"}
    assert res["overdue"][0]["age_days"] >= 3


def test_closure_overdue_db_missing(jdir):
    """No DB file → graceful n/a, never raises or creates the file."""
    res = selfmon.closure_overdue(jdir, now=time.time())
    assert res["db_available"] is False
    assert res["overdue_count"] == 0
    assert not (jdir / "data" / "jarvis.db").exists()


def test_intent_db_opened_read_only(jdir, seed_intent_db):
    """The mode=ro contract: a write through selfmon's connection must fail."""
    seed_intent_db("int_x", "x", "awaiting", _ts(time.time() - 86400, sep="T"))
    conn = selfmon._open_intent_db_ro(jdir)
    assert conn is not None
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("UPDATE intentions SET name = 'mutated' WHERE id = 'int_x'")
        conn.commit()
    conn.close()


# ── metric 4: model crash / skip ────────────────────────────────────────────

def test_model_crash_or_skip(jdir):
    now = time.time()
    rows = [
        {"ts": _ts(now - 600), "event": "task_finish", "task": "daily-plan",
         "status": "failed"},
        {"ts": _ts(now - 500), "event": "task_finish", "task": "daily-plan",
         "status": "parse_failed"},
        {"ts": _ts(now - 400), "event": "task_finish", "task": "checkin",
         "status": "ok"},                                    # healthy → ignored
        {"ts": _ts(now - 300), "event": "task_timeout", "task": "repos-sync"},
        {"ts": _ts(now - 200), "event": "circuit_tripped", "task": "mail"},
    ]
    _write_jsonl(jdir / "sched_events.jsonl", rows)
    res = selfmon.model_crash_or_skip(jdir, since_epoch=now - 24 * 3600,
                                      window_hours=24)
    # 2 daily-plan + 1 timeout + 1 circuit_trip = 4
    assert res["total"] == 4
    assert res["by_status"]["failed"] == 1
    assert res["by_status"]["parse_failed"] == 1
    assert res["by_status"]["timeout"] == 1
    assert res["circuit_trips"]["mail"] == 1
    assert "checkin" not in res["by_task"]


# ── metric 5: silent failures ───────────────────────────────────────────────

def test_silent_failures_from_log(jdir):
    now = time.time()
    rows = [
        {"ts": _ts(now - 600, sep="T"), "level": "info", "component": "heartbeat",
         "msg": "Script tasks/foo.py timed out (60s)"},
        {"ts": _ts(now - 500, sep="T"), "level": "info", "component": "heartbeat",
         "msg": "JSON parse failed for daily-plan response"},
        {"ts": _ts(now - 400, sep="T"), "level": "info", "component": "heartbeat",
         "msg": "all good here"},                            # no signature
        {"ts": _ts(now - 200000, sep="T"), "level": "info", "component": "x",
         "msg": "exited with code 1"},                       # outside window
    ]
    _write_jsonl(jdir / "jarvis.log", rows)
    res = selfmon.silent_failures(jdir, since_epoch=now - 24 * 3600)
    assert res["total"] == 2
    assert res["by_signature"]["timed out (60s)"] == 1
    assert res["by_signature"]["JSON parse failed"] == 1
    assert "exited with code 1" not in res["by_signature"]   # old one excluded


# ── liveness assertion ──────────────────────────────────────────────────────

def test_liveness_ok_when_events_recent(jdir):
    now = time.time()
    _write_jsonl(jdir / "sched_events.jsonl",
                 [{"ts": _ts(now - 60), "event": "task_finish",
                   "task": "checkin", "status": "ok"}])
    ok, reason = selfmon.selfmon_liveness_ok(jdir)
    assert ok is True
    assert "sched_events" in reason


def test_liveness_dead_when_silent_but_alive(jdir):
    """No sched_events in 24h while a session is active → DEAD."""
    now = time.time()
    # sched_events present but all stale (>24h old)
    _write_jsonl(jdir / "sched_events.jsonl",
                 [{"ts": _ts(now - 48 * 3600), "event": "task_finish",
                   "task": "checkin", "status": "ok"}])
    # corroborating liveness: active session transcripts exist
    (jdir / "active_sessions.json").write_text(
        json.dumps({"ou_x": {"session_id": "abc", "counter": 1}}),
        encoding="utf-8")
    ok, reason = selfmon.selfmon_liveness_ok(jdir)
    assert ok is False
    assert "self-monitoring dead" in reason
    assert "no sched_events in 24h" in reason


def test_liveness_dead_when_silent_but_heartbeat_recent(jdir):
    """No sched_events but heartbeat_state shows a recent run → DEAD."""
    now = time.time()
    _write_jsonl(jdir / "sched_events.jsonl", [])
    (jdir / "heartbeat_state.json").write_text(
        json.dumps({"checkin": {"last_run": now - 120, "last_status": "ok"}}),
        encoding="utf-8")
    ok, reason = selfmon.selfmon_liveness_ok(jdir)
    assert ok is False
    assert "self-monitoring dead" in reason


def test_liveness_idle_box_not_flagged(jdir):
    """No events AND no corroborating signals → don't cry wolf (idle box)."""
    # empty/absent everything
    ok, reason = selfmon.selfmon_liveness_ok(jdir)
    assert ok is True
    assert "idle" in reason


# ── aggregate + report ──────────────────────────────────────────────────────

def test_compute_selfmon_aggregates(jdir, seed_intent_db):
    now = time.time()
    _write_jsonl(jdir / "engagement_log.jsonl",
                 [{"ts": _ts(now - 600), "source": "calendar-sync",
                   "type": "sent", "epoch": now - 600},
                  {"ts": _ts(now - 500), "source": "calendar-sync",
                   "type": "sent", "epoch": now - 500}])
    _write_jsonl(jdir / "sched_events.jsonl",
                 [{"ts": _ts(now - 400), "event": "task_finish",
                   "task": "daily-plan", "status": "failed"}])
    seed_intent_db("int_old", "zombie", "awaiting",
                   _ts(now - 5 * 86400, sep="T"))

    m = selfmon.compute_selfmon(jdir, window_hours=24)
    assert m["noise_card_count"]["total_sent"] == 2
    assert m["model_crash_or_skip"]["total"] == 1
    assert m["closure_overdue"]["overdue_count"] == 1
    assert set(m) >= {"noise_card_count", "same_intent_refires",
                      "closure_overdue", "model_crash_or_skip",
                      "silent_failures", "window_hours"}


def test_selfmon_report_emits_warning_lines(jdir, seed_intent_db):
    """Anomalies must surface as ⚠️ lines so REQ-39 can pick them up."""
    now = time.time()
    seed_intent_db("int_old", "zombie", "awaiting",
                   _ts(now - 5 * 86400, sep="T"))
    _write_jsonl(jdir / "sched_events.jsonl",
                 [{"ts": _ts(now - 400), "event": "task_finish",
                   "task": "daily-plan", "status": "failed"}])
    report = selfmon.selfmon_report(jdir, window_hours=24)
    assert "⚠️" in report
    assert "Self-monitoring" in report


def test_selfmon_report_clean_window(jdir):
    """A quiet window emits the ✓ line, no ⚠️."""
    report = selfmon.selfmon_report(jdir, window_hours=24)
    assert "⚠️" not in report
    assert "✓" in report


def test_compute_selfmon_never_raises_on_garbage(jdir):
    """Corrupt live files must degrade to empty shapes, not raise."""
    (jdir / "engagement_log.jsonl").write_text("{not json\n!!!", encoding="utf-8")
    (jdir / "sched_events.jsonl").write_text("garbage\n", encoding="utf-8")
    (jdir / "jarvis.log").write_text("\x00\x01 broken", encoding="utf-8")
    m = selfmon.compute_selfmon(jdir, window_hours=24)
    assert m["noise_card_count"]["total_sent"] == 0
    assert m["model_crash_or_skip"]["total"] == 0
