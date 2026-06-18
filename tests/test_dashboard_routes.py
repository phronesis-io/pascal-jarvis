"""Tests for the dashboard upgrades (REQ-43~46).

Covers:
- _parse_trigger_when with naive AND aware datetimes (the /intentions 500)
- telemetry tail-reader: incremental appends, partial lines, rotation
- intent funnel computation against a seeded tmp SQLite (naive/aware rows,
  expired rows, leak counting) + re-arm
- bookmarks render path is read-only (GET must not advance surfaced_count)
- task-health aggregation from a seeded sched_events.jsonl (failure rate,
  skip reasons, silently-dead detection)
- route smoke tests: all 7 pages render 200 via FastAPI TestClient (NiceGUI
  registers pages on its FastAPI `app`; a manual add_run_config replaces
  ui.run so no server/event loop is needed)
- the 9 write API endpoints accept JSON bodies (the bare-`request` 422 fix)

Every test redirects dashboard.db.DB_PATH to tmp_path — NEVER the real
data/jarvis.db — and page-module JARVIS_DIR globals to tmp_path so no repo
state file is read or written.
"""

import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import dashboard.db as db_module
import dashboard.telemetry as telemetry
from core.timeutil import now_local

# ── NiceGUI app, prepared for TestClient (no server, no ui.run) ──────────
# Skip this whole module where nicegui isn't installed (e.g. CI, which only
# installs pyyaml+pytest) instead of erroring out and aborting collection for
# the entire suite. Runs in full wherever the dashboard deps exist (local).
pytest.importorskip("nicegui", exc_type=ImportError)

from nicegui import app as nicegui_app
from dashboard.app import create_app

create_app()  # registers all pages + API routes on the FastAPI app

if not nicegui_app.config.has_run_config:
    nicegui_app.config.add_run_config(
        reload=False, title="test", viewport="width=device-width, initial-scale=1",
        favicon=None, dark=None, language="en-US",
        binding_refresh_interval=0.1, reconnect_timeout=3.0,
        message_history_length=1000, tailwind=True, unocss=None, prod_js=True,
        show_welcome_message=False, markdown=False,
    )

from fastapi.testclient import TestClient  # noqa: E402

PAGES = ["/", "/tasks", "/bookmarks", "/settings", "/intentions",
         "/thinking", "/agent-calendar", "/engagement"]


@pytest.fixture
def test_db(tmp_path, monkeypatch):
    """Redirect dashboard.db (and core.intentions through it) to a tmp DB."""
    import core.intentions as intentions
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")
    if db_module._connection is not None:
        db_module._connection.close()
    db_module._connection = None
    intentions._table_ready = False  # force table creation on the fresh DB
    yield db_module
    if db_module._connection is not None:
        db_module._connection.close()
    db_module._connection = None
    intentions._table_ready = False


@pytest.fixture
def jarvis_tmp(tmp_path, monkeypatch):
    """Point every page module's JARVIS_DIR at tmp_path (no repo reads)."""
    from dashboard.pages import home, tasks, settings, agent_calendar, engagement
    for mod in (home, tasks, settings, agent_calendar, engagement):
        monkeypatch.setattr(mod, "JARVIS_DIR", tmp_path)
    # engagement_stats reads $JARVIS_DIR/engagement_log.jsonl
    monkeypatch.setenv("JARVIS_DIR", str(tmp_path))
    telemetry.reset_cache()
    return tmp_path


def _write_jsonl(path: Path, entries: list[dict]):
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n",
                    encoding="utf-8")


# ═════════════════════════════════════════════════════════════════════════
# (1) _parse_trigger_when — naive AND aware datetimes, no exception
# ═════════════════════════════════════════════════════════════════════════

class TestParseTriggerWhen:
    def _intent(self, dt_str):
        return {"trigger_type": "date",
                "trigger_config": json.dumps({"datetime": dt_str})}

    def test_naive_datetime_no_exception(self):
        from dashboard.pages.intentions import _parse_trigger_when
        out = _parse_trigger_when(self._intent("2026-09-25T09:00:00"))
        assert isinstance(out, str) and out

    def test_aware_datetime_no_exception(self):
        # The live-DB shape that 500'd: aware target vs naive arithmetic
        from dashboard.pages.intentions import _parse_trigger_when
        out = _parse_trigger_when(self._intent("2026-06-13T12:30:00+08:00"))
        assert isinstance(out, str) and out

    def test_past_naive_shows_expired(self):
        from dashboard.pages.intentions import _parse_trigger_when
        past = (now_local() - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%S")
        assert "已过期" in _parse_trigger_when(self._intent(past))

    def test_future_aware_relative(self):
        from dashboard.pages.intentions import _parse_trigger_when
        future = (now_local() + timedelta(minutes=30)).isoformat()  # aware
        assert "分钟后" in _parse_trigger_when(self._intent(future))

    def test_malformed_datetime_falls_back_to_raw(self):
        from dashboard.pages.intentions import _parse_trigger_when
        assert _parse_trigger_when(self._intent("not-a-date")) == "not-a-date"

    def test_cron_and_interval_unchanged(self):
        from dashboard.pages.intentions import _parse_trigger_when
        assert "cron" in _parse_trigger_when(
            {"trigger_type": "cron", "trigger_config": '{"expression": "0 9 * * *"}'})
        assert "每" in _parse_trigger_when(
            {"trigger_type": "interval", "trigger_config": '{"seconds": 600}'})


# ═════════════════════════════════════════════════════════════════════════
# (2) telemetry tail-reader — incremental appends + rotation
# ═════════════════════════════════════════════════════════════════════════

class TestTelemetryTail:
    def test_initial_read(self, tmp_path):
        telemetry.reset_cache()
        p = tmp_path / "ev.jsonl"
        _write_jsonl(p, [{"n": 1}, {"n": 2}])
        assert [e["n"] for e in telemetry.read_jsonl_tail(p)] == [1, 2]

    def test_appends_picked_up_incrementally(self, tmp_path):
        telemetry.reset_cache()
        p = tmp_path / "ev.jsonl"
        _write_jsonl(p, [{"n": 1}])
        assert len(telemetry.read_jsonl_tail(p)) == 1
        offset_after_first = telemetry._tail_cache[str(p)]["offset"]
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps({"n": 2}) + "\n")
        events = telemetry.read_jsonl_tail(p)
        assert [e["n"] for e in events] == [1, 2]
        # offset advanced — the second read consumed only appended bytes
        assert telemetry._tail_cache[str(p)]["offset"] > offset_after_first

    def test_partial_line_not_consumed_until_complete(self, tmp_path):
        telemetry.reset_cache()
        p = tmp_path / "ev.jsonl"
        _write_jsonl(p, [{"n": 1}])
        telemetry.read_jsonl_tail(p)
        with open(p, "a", encoding="utf-8") as f:
            f.write('{"n": 2')  # writer mid-append, no newline
        assert len(telemetry.read_jsonl_tail(p)) == 1
        with open(p, "a", encoding="utf-8") as f:
            f.write('}\n')
        assert [e["n"] for e in telemetry.read_jsonl_tail(p)] == [1, 2]

    def test_rotation_shrink_rereads_whole_file(self, tmp_path):
        telemetry.reset_cache()
        p = tmp_path / "ev.jsonl"
        _write_jsonl(p, [{"n": i} for i in range(10)])
        assert len(telemetry.read_jsonl_tail(p)) == 10
        _write_jsonl(p, [{"n": 99}])  # rotated: new, smaller live file
        events = telemetry.read_jsonl_tail(p)
        assert [e["n"] for e in events] == [99]

    def test_rotation_by_rename_rereads_on_inode_change(self, tmp_path):
        """FIX 6: sched_events rotates by RENAME — the live file is replaced by
        a FRESH inode that may be LARGER than the cached offset. A size-only
        reset (size < offset) would keep reading from the stale offset into the
        new file and miss/garble a generation. A changed inode forces a full
        re-read."""
        import os
        telemetry.reset_cache()
        p = tmp_path / "ev.jsonl"
        # gen 1: small file, fully consumed (cache offset advances).
        _write_jsonl(p, [{"n": 1}, {"n": 2}])
        assert [e["n"] for e in telemetry.read_jsonl_tail(p)] == [1, 2]
        ino_before = telemetry._tail_cache[str(p)]["ino"]

        # Rotate by RENAME: replace the live path with a brand-new, LARGER file
        # whose inode differs (the size grew, so a size-only check would NOT
        # trigger a reset and would mis-read).
        os.replace(p, tmp_path / "ev.jsonl.1")
        _write_jsonl(p, [{"n": 10}, {"n": 11}, {"n": 12}, {"n": 13}])
        assert p.stat().st_ino != ino_before          # genuinely a new inode
        assert p.stat().st_size > 0

        events = telemetry.read_jsonl_tail(p)
        assert [e["n"] for e in events] == [10, 11, 12, 13]
        assert telemetry._tail_cache[str(p)]["ino"] == p.stat().st_ino

    def test_missing_file_and_bad_lines(self, tmp_path):
        telemetry.reset_cache()
        assert telemetry.read_jsonl_tail(tmp_path / "nope.jsonl") == []
        p = tmp_path / "ev.jsonl"
        p.write_text('{"n": 1}\nnot json at all\n{"n": 2}\n')
        assert [e["n"] for e in telemetry.read_jsonl_tail(p)] == [1, 2]

    def test_read_json_ttl(self, tmp_path):
        telemetry.reset_cache()
        p = tmp_path / "state.json"
        p.write_text('{"a": 1}')
        assert telemetry.read_json(p, ttl=60) == {"a": 1}
        p.write_text('{"a": 2}')
        assert telemetry.read_json(p, ttl=60) == {"a": 1}  # cached
        assert telemetry.read_json(p, ttl=0) == {"a": 2}   # ttl expired
        assert telemetry.read_json(tmp_path / "nope.json", default={}) == {}

    def test_read_sched_events_merges_rotated_generation(self, tmp_path):
        telemetry.reset_cache()
        _write_jsonl(tmp_path / "sched_events.jsonl.1", [{"n": 1}])
        _write_jsonl(tmp_path / "sched_events.jsonl", [{"n": 2}])
        assert [e["n"] for e in telemetry.read_sched_events(tmp_path)] == [1, 2]


# ═════════════════════════════════════════════════════════════════════════
# (3) intent funnel — seeded tmp SQLite, naive/aware + expired rows
# ═════════════════════════════════════════════════════════════════════════

class TestIntentFunnel:
    def _seed(self):
        """5 moments in-window: pending(naive), pending(aware), executed,
        leak-expired ×2 (retries-exhausted + storm-class). Returns ids."""
        import core.intentions as intents
        now = now_local()
        ids = {}
        ids["naive"] = intents.create_intent(
            name="naive future", trigger_type="date",
            trigger_config={"datetime": (now + timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%S")},
            source="test")
        ids["aware"] = intents.create_intent(
            name="aware future", trigger_type="date",
            trigger_config={"datetime": (now + timedelta(days=5)).isoformat()},
            source="test")
        ids["executed"] = intents.create_intent(
            name="done one", trigger_type="date",
            trigger_config={"datetime": (now - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%S")},
            source="test")
        ids["leak1"] = intents.create_intent(
            name="leaked retries", trigger_type="date",
            trigger_config={"datetime": (now - timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%S")},
            source="test")
        ids["leak2"] = intents.create_intent(
            name="leaked storm", trigger_type="date",
            trigger_config={"datetime": "2026-06-01T09:00:00+08:00"},  # aware past
            source="test")
        db = intents._get_db()
        ts = now.strftime("%Y-%m-%dT%H:%M:%S")
        db.execute("UPDATE intentions SET status='executed', triggered_at=?, executed_at=?, "
                   "closure_status='awaiting' WHERE id=?", (ts, ts, ids["executed"]))
        db.execute("UPDATE intentions SET status='expired', triggered_at=?, attempt=3, "
                   "last_error='expired after 3 attempts — breach notification queued' "
                   "WHERE id=?", (ts, ids["leak1"]))
        db.execute("UPDATE intentions SET status='expired', triggered_at=?, "
                   "last_error='auto-expired: stuck in triggered, trigger >24h past (storm class)' "
                   "WHERE id=?", (ts, ids["leak2"]))
        db.commit()
        return ids

    def test_funnel_counts(self, test_db):
        from dashboard.pages.intentions import compute_funnel
        self._seed()
        f = compute_funnel(7)
        assert f["created"] == 5
        assert f["fired"] == 3        # triggered_at set on executed + 2 expired
        assert f["executed"] == 1
        assert f["expired"] == 2
        assert f["leaked"] == 2       # both last_error patterns counted
        assert f["closure_asked"] == 1

    def test_funnel_closed_counts_closed_at(self, test_db):
        import core.intentions as intents
        from dashboard.pages.intentions import compute_funnel
        ids = self._seed()
        db = intents._get_db()
        db.execute("UPDATE intentions SET closure_status='done', closed_at=? WHERE id=?",
                   (now_local().strftime("%Y-%m-%dT%H:%M:%S"), ids["executed"]))
        db.commit()
        f = compute_funnel(7)
        assert f["closed"] == 1

    def test_funnel_window_excludes_old_rows(self, test_db):
        import core.intentions as intents
        from dashboard.pages.intentions import compute_funnel
        ids = self._seed()
        old = (now_local() - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%S")
        db = intents._get_db()
        db.execute("UPDATE intentions SET created_at=?, triggered_at=NULL WHERE id=?",
                   (old, ids["naive"]))
        db.commit()
        assert compute_funnel(7)["created"] == 4

    def test_expired_autopsy_lists_expired_with_due_time(self, test_db):
        from dashboard.pages.intentions import expired_autopsy
        self._seed()
        rows = expired_autopsy()
        assert len(rows) == 2
        names = {r["name"] for r in rows}
        assert names == {"leaked retries", "leaked storm"}
        for r in rows:
            assert r["was_due"] not in ("", "?")
            assert r["last_error"]

    def test_rearm_resets_state_and_schedules_10min_out(self, test_db):
        import core.intentions as intents
        from dashboard.pages.intentions import rearm_intent
        ids = self._seed()
        assert rearm_intent(ids["leak1"])
        it = intents.get_intent(ids["leak1"])
        assert it["status"] == "pending"
        assert it["attempt"] == 0
        cfg = json.loads(it["trigger_config"])
        new_dt = datetime.fromisoformat(cfg["datetime"])
        delta_s = (new_dt - now_local().replace(tzinfo=None)).total_seconds()
        assert 8 * 60 < delta_s < 12 * 60
        # re-armed row leaves the autopsy list and re-enters the due pipeline
        from dashboard.pages.intentions import expired_autopsy
        assert ids["leak1"] not in {r["id"] for r in expired_autopsy()}

    def test_rearm_unknown_id_is_safe(self, test_db):
        from dashboard.pages.intentions import rearm_intent
        assert rearm_intent("int_nonexistent") is False

    def test_rearm_clears_past_expires_at_survives_cleanup(self, test_db):
        """FIX 1: a re-armed expired DATE intent must NOT silently re-expire.
        rearm clears expires_at; get_due_intents() runs cleanup_expired() first
        (WHERE expires_at < now), so a leftover PAST expires_at would flip the
        row straight back to 'expired' before it could ever fire."""
        import core.intentions as intents
        from dashboard.pages.intentions import rearm_intent
        ids = self._seed()
        # Stamp a PAST expires_at on the expired row (the live-DB shape).
        past = (now_local() - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S")
        db = intents._get_db()
        db.execute("UPDATE intentions SET expires_at=? WHERE id=?",
                   (past, ids["leak1"]))
        db.commit()
        assert rearm_intent(ids["leak1"])
        assert intents.get_intent(ids["leak1"])["expires_at"] is None
        # cleanup_expired() must leave the re-armed row pending, not re-kill it.
        intents.cleanup_expired()
        assert intents.get_intent(ids["leak1"])["status"] == "pending"

    def test_rearm_cron_recomputes_future_next_fire_at(self, test_db):
        """FIX 2: a re-armed CRON intent re-anchors next_fire_at to the next
        match (future), so a stale-past watermark neither fires instantly nor
        gets skipped as >CRON_STALENESS late. The expression is preserved."""
        import core.intentions as intents
        from dashboard.pages.intentions import rearm_intent
        iid = intents.create_intent(
            name="daily cron", trigger_type="cron",
            trigger_config={"expression": "0 9 * * *"}, source="test")
        db = intents._get_db()
        # Make it look dead: expired + stale-past next_fire_at + spent retries.
        stale = (now_local() - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%S")
        db.execute("UPDATE intentions SET status='expired', attempt=3, "
                   "next_fire_at=?, expires_at=? WHERE id=?",
                   (stale, stale, iid))
        db.commit()
        result = rearm_intent(iid)
        assert result  # honest, non-date toast string
        row = intents.get_intent(iid)
        assert row["status"] == "pending"
        assert row["attempt"] == 0
        assert row["expires_at"] is None
        # expression untouched; next_fire_at now strictly in the future.
        assert json.loads(row["trigger_config"])["expression"] == "0 9 * * *"
        nfa = datetime.fromisoformat(row["next_fire_at"])
        assert nfa.replace(tzinfo=None) > now_local().replace(tzinfo=None)

    def test_rearm_interval_reanchors_created_at(self, test_db):
        """FIX 2: a re-armed INTERVAL intent re-anchors created_at=now (the
        interval due-check anchors on created_at), so it fires within one
        interval instead of instantly off a stale anchor."""
        import core.intentions as intents
        from dashboard.pages.intentions import rearm_intent
        iid = intents.create_intent(
            name="every hour", trigger_type="interval",
            trigger_config={"seconds": 3600}, source="test")
        db = intents._get_db()
        old = (now_local() - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%S")
        db.execute("UPDATE intentions SET status='expired', created_at=? WHERE id=?",
                   (old, iid))
        db.commit()
        assert rearm_intent(iid)
        row = intents.get_intent(iid)
        assert row["status"] == "pending"
        anchor = datetime.fromisoformat(row["created_at"])
        # re-anchored to ~now (within a couple minutes), not 5 days ago.
        delta = abs((now_local().replace(tzinfo=None) - anchor).total_seconds())
        assert delta < 120

    def test_awaiting_age_zombie_flag(self, test_db):
        from dashboard.pages.intentions import awaiting_age_days
        old = {"executed_at": (now_local() - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%S")}
        fresh = {"executed_at": now_local().strftime("%Y-%m-%dT%H:%M:%S")}
        assert awaiting_age_days(old) > 3
        assert awaiting_age_days(fresh) < 1


# ═════════════════════════════════════════════════════════════════════════
# (4) bookmarks render path is read-only
# ═════════════════════════════════════════════════════════════════════════

class TestBookmarksReadOnly:
    def _sum_surfaced(self):
        db = db_module.get_db()
        return db.execute("SELECT COALESCE(SUM(surfaced_count), 0) FROM bookmarks").fetchone()[0]

    def test_resurface_candidates_do_not_mutate(self, test_db):
        from dashboard.bookmark_pipeline import capture, get_resurface_candidates
        for i in range(8):
            capture(f"Article {i}", f"https://example.com/{i}")
        before = self._sum_surfaced()
        for _ in range(3):  # the corruption shape: repeated GET renders
            assert get_resurface_candidates(5)
        assert self._sum_surfaced() == before == 0

    def test_mark_surfaced_is_the_explicit_advance(self, test_db):
        from dashboard.bookmark_pipeline import capture, mark_surfaced
        bm_id = capture("Article", "https://example.com/x")
        mark_surfaced([bm_id])
        row = db_module.get_db().execute(
            "SELECT surfaced_count, last_surfaced_at FROM bookmarks WHERE id=?",
            (bm_id,)).fetchone()
        assert row[0] == 1
        assert row[1]  # last_surfaced_at stamped
        mark_surfaced([bm_id, 99999])  # unknown id is a no-op, not an error
        assert db_module.get_db().execute(
            "SELECT surfaced_count FROM bookmarks WHERE id=?", (bm_id,)).fetchone()[0] == 2


# ═════════════════════════════════════════════════════════════════════════
# (5) task-health aggregation + silently-dead detection
# ═════════════════════════════════════════════════════════════════════════

class TestTaskHealth:
    NOW = "2026-06-13 12:00:00"

    def _seed_events(self, tmp_path):
        def ts(mins_ago):
            base = time.mktime(time.strptime(self.NOW, "%Y-%m-%d %H:%M:%S"))
            return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(base - mins_ago * 60))
        events = [
            # alpha: 2 ok + 1 failed + 1 timeout
            {"ts": ts(120), "event": "task_spawn", "task": "alpha", "run_id": "r1"},
            {"ts": ts(119), "event": "task_finish", "task": "alpha", "run_id": "r1",
             "status": "ok", "duration_s": 10.0},
            {"ts": ts(60), "event": "task_spawn", "task": "alpha", "run_id": "r2"},
            {"ts": ts(59), "event": "task_finish", "task": "alpha", "run_id": "r2",
             "status": "failed", "duration_s": 30.0},
            {"ts": ts(30), "event": "task_spawn", "task": "alpha", "run_id": "r3"},
            {"ts": ts(29), "event": "task_finish", "task": "alpha", "run_id": "r3",
             "status": "ok", "duration_s": 20.0},
            {"ts": ts(10), "event": "task_spawn", "task": "alpha", "run_id": "r4"},
            {"ts": ts(9), "event": "task_timeout", "task": "alpha", "run_id": "r4"},
            # beta: 12× empty_pre skips, never spawned
            *[{"ts": ts(5 + i), "event": "task_skip", "task": "beta",
               "run_id": f"s{i}", "reason": "empty_pre"} for i in range(12)],
            # noise the aggregator must ignore
            {"ts": ts(3), "event": "batch_flush", "task": "", "count": 2},
            {"ts": ts(2), "event": "intent_fired", "task": "int_abc", "attempt": 1},
        ]
        _write_jsonl(tmp_path / "sched_events.jsonl", events)
        return time.mktime(time.strptime(self.NOW, "%Y-%m-%d %H:%M:%S"))

    def test_aggregation(self, tmp_path):
        from dashboard.pages.agent_calendar import aggregate_task_health
        telemetry.reset_cache()
        now_ts = self._seed_events(tmp_path)
        health = aggregate_task_health(
            telemetry.read_sched_events(tmp_path), window_s=86400, now_ts=now_ts)

        assert set(health) == {"alpha", "beta"}
        a = health["alpha"]
        assert a["finishes"] == 3
        assert a["failures"] == 1
        assert a["timeouts"] == 1
        assert a["failure_rate"] == pytest.approx(2 / 4)  # (1 fail + 1 timeout) / 4
        assert a["p50_s"] == pytest.approx(20.0)
        assert a["max_s"] == pytest.approx(30.0)
        assert "29:" in a["last_finish"] or a["last_finish_ts"] == pytest.approx(now_ts - 29 * 60)

        b = health["beta"]
        assert b["skip_reasons"] == {"empty_pre": 12}
        assert b["finishes"] == 0 and b["last_finish_ts"] is None

    def test_window_excludes_old_events(self, tmp_path):
        from dashboard.pages.agent_calendar import aggregate_task_health
        telemetry.reset_cache()
        now_ts = self._seed_events(tmp_path)
        health = aggregate_task_health(
            telemetry.read_sched_events(tmp_path), window_s=15 * 60, now_ts=now_ts)
        # only the timeout (9m ago) is inside the 15min window for alpha
        a = health["alpha"]
        assert a["finishes"] == 0 and a["timeouts"] == 1
        # last REAL run stays all-time even outside the window
        assert a["last_finish_ts"] is not None

    def test_silently_dead_detection(self, tmp_path):
        from dashboard.pages.agent_calendar import detect_silently_dead
        telemetry.reset_cache()
        now_ts = self._seed_events(tmp_path)
        events = telemetry.read_sched_events(tmp_path)
        hb_tasks = [
            {"name": "alpha", "interval": 600},    # spawned 10m ago → alive
            {"name": "beta", "interval": 300},     # never spawned → dead
            {"name": "gamma", "interval": 900},    # no events at all → dead
            {"name": "weekly", "interval": 7 * 86400},  # interval ≥ 6h → exempt
        ]
        dead = {d["name"] for d in detect_silently_dead(hb_tasks, events, now_ts=now_ts)}
        assert dead == {"beta", "gamma"}

    def test_silently_dead_three_intervals(self, tmp_path):
        from dashboard.pages.agent_calendar import detect_silently_dead
        telemetry.reset_cache()
        now_ts = self._seed_events(tmp_path)
        events = telemetry.read_sched_events(tmp_path)
        # alpha last spawned 10m ago: dead for 3×interval < 10m, alive above
        assert detect_silently_dead(
            [{"name": "alpha", "interval": 150}], events, now_ts=now_ts)
        assert not detect_silently_dead(
            [{"name": "alpha", "interval": 600}], events, now_ts=now_ts)

    def test_heartbeat_md_parsing_path(self, tmp_path):
        from dashboard.pages.agent_calendar import _load_heartbeat_tasks
        (tmp_path / "HEARTBEAT.md").write_text(
            "### alpha\n- interval: 5m\n- prompt: do alpha\n"
            "### beta\n- interval: 2h\n- prompt: do beta\n", encoding="utf-8")
        tasks = {t["name"]: t["interval"] for t in _load_heartbeat_tasks(tmp_path)}
        assert tasks == {"alpha": 300, "beta": 7200}


# ═════════════════════════════════════════════════════════════════════════
# (5b) engagement board — source-level ROI from engagement_log.jsonl
# ═════════════════════════════════════════════════════════════════════════

class TestEngagementBoard:
    def test_source_engagement_report_counts_sent_read_replied_and_ignored(self, tmp_path):
        from dashboard.pages.engagement import source_engagement_report

        now = int(time.time())
        _write_jsonl(tmp_path / "engagement_log.jsonl", [
            {"ts": "2026-06-18 10:00", "source": "checkin", "type": "sent",
             "epoch": now - 600, "message_ids": ["m1"]},
            {"ts": "2026-06-18 10:01", "source": "checkin", "type": "sent",
             "epoch": now - 500, "message_ids": ["m2"]},
            {"ts": "2026-06-18 10:02", "type": "read", "epoch": now - 490,
             "message_ids": ["m1", "m2"]},
            {"ts": "2026-06-18 10:03", "source": "checkin", "type": "response",
             "reaction": "engaged", "epoch": now - 480, "gap_seconds": 120},
            {"ts": "2026-06-18 10:04", "source": "checkin", "type": "response",
             "reaction": "ignored", "epoch": now - 470, "gap_seconds": 1900},
            {"ts": "2026-06-18 10:05", "source": "feed", "type": "sent",
             "epoch": now - 460, "message_ids": ["m3"]},
            {"ts": "2026-06-18 10:06", "source": "feed", "type": "response",
             "reaction": "late_reply", "epoch": now - 450, "gap_seconds": 1200},
            {"ts": "2026-06-18 10:07", "source": "conversation", "type": "response",
             "reaction": "conversation", "epoch": now - 440},
        ])

        report = source_engagement_report(tmp_path, days=7)
        by_source = {r["source"]: r for r in report["sources"]}

        assert report["totals"]["sent"] == 3
        assert report["totals"]["read"] == 2
        assert report["totals"]["replied"] == 2
        assert report["totals"]["ignored"] == 1
        assert by_source["checkin"]["sent"] == 2
        assert by_source["checkin"]["read"] == 2
        assert by_source["checkin"]["replied"] == 1
        assert by_source["checkin"]["ignored"] == 1
        assert by_source["checkin"]["reply_rate"] == 50.0
        assert by_source["checkin"]["read_rate"] == 100.0
        assert by_source["checkin"]["median_gap_s"] == 120
        assert by_source["feed"]["late_reply"] == 1
        assert "conversation" not in by_source

    def test_source_engagement_report_filters_old_rows(self, tmp_path):
        from dashboard.pages.engagement import source_engagement_report

        now = int(time.time())
        _write_jsonl(tmp_path / "engagement_log.jsonl", [
            {"source": "old", "type": "sent", "epoch": now - 20 * 86400},
            {"source": "fresh", "type": "sent", "epoch": now - 60},
        ])

        report = source_engagement_report(tmp_path, days=7)
        assert [r["source"] for r in report["sources"]] == ["fresh"]

    def test_source_engagement_report_caps_historical_duplicate_reply_rate(self, tmp_path):
        from dashboard.pages.engagement import source_engagement_report

        now = int(time.time())
        _write_jsonl(tmp_path / "engagement_log.jsonl", [
            {"source": "heartbeat", "type": "sent", "epoch": now - 600},
            {"source": "heartbeat", "type": "response", "reaction": "engaged",
             "epoch": now - 590, "gap_seconds": 10},
            {"source": "heartbeat", "type": "response", "reaction": "late_reply",
             "epoch": now - 580, "gap_seconds": 600},
        ])

        report = source_engagement_report(tmp_path, days=7)
        row = report["sources"][0]
        assert row["sent"] == 1
        assert row["replied"] == 1
        assert row["reply_rate"] == 100.0
        assert report["totals"]["reply_rate"] == 100.0


# ═════════════════════════════════════════════════════════════════════════
# Route smoke tests — all pages 200 + write APIs accept JSON bodies
# ═════════════════════════════════════════════════════════════════════════

class TestRoutes:
    @pytest.fixture
    def client(self, test_db, jarvis_tmp):
        """TestClient with DB + JARVIS_DIR fully redirected to tmp."""
        import core.intentions as intents
        now = now_local()
        # seed live-ish telemetry files in the tmp jarvis dir
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        _write_jsonl(jarvis_tmp / "sched_events.jsonl", [
            {"ts": ts, "event": "task_spawn", "task": "alpha", "run_id": "r1"},
            {"ts": ts, "event": "task_finish", "task": "alpha", "run_id": "r1",
             "status": "ok", "duration_s": 1.5},
            {"ts": ts, "event": "task_skip", "task": "beta", "run_id": "r2",
             "reason": "empty_pre"},
        ])
        _write_jsonl(jarvis_tmp / "heartbeat_outbox.jsonl", [
            {"role": "assistant", "text": "hello", "ts": ts[:16], "source": "heartbeat"},
        ])
        (jarvis_tmp / "heartbeat_state.json").write_text(json.dumps({
            "alpha": {"last_run": int(time.time()),
                      "circuit": {"consecutive_failures": 0, "disabled_until": 0}},
        }))
        (jarvis_tmp / "HEARTBEAT.md").write_text(
            "### alpha\n- interval: 10m\n- prompt: x\n", encoding="utf-8")
        # both datetime formats present in the live DB — pins the 500 fix
        intents.create_intent(
            name="naive", trigger_type="date", source="test",
            trigger_config={"datetime": (now + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S")})
        intents.create_intent(
            name="aware", trigger_type="date", source="test",
            trigger_config={"datetime": (now + timedelta(days=2)).isoformat()})
        return TestClient(nicegui_app)

    @pytest.mark.parametrize("path", PAGES)
    def test_page_renders_200(self, client, path):
        r = client.get(path)
        assert r.status_code == 200, f"{path} -> {r.status_code}"

    def test_bookmarks_get_does_not_mutate_db(self, client):
        from dashboard.bookmark_pipeline import capture
        for i in range(6):
            capture(f"A{i}", f"https://e.com/{i}")
        db = db_module.get_db()
        before = db.execute("SELECT COALESCE(SUM(surfaced_count),0), COUNT(*) FROM bookmarks").fetchone()
        assert client.get("/bookmarks").status_code == 200
        after = db.execute("SELECT COALESCE(SUM(surfaced_count),0), COUNT(*) FROM bookmarks").fetchone()
        assert tuple(after) == tuple(before)

    # ── the 9 write endpoints: bare `request` made FastAPI treat the body
    # as a query param → universal 422. Each must now parse a JSON body. ──

    def test_api_bookmark_add_and_patch(self, client):
        r = client.post("/api/bookmarks", json={"title": "T", "url": "https://t.co"})
        assert r.status_code == 200 and r.json()["status"] == "ok"
        bm_id = r.json()["id"]
        r = client.patch(f"/api/bookmarks/{bm_id}", json={"status": "reading"})
        assert r.status_code == 200
        assert db_module.bookmark_list(status="reading")[0]["id"] == bm_id

    def test_api_log_event(self, client):
        r = client.post("/api/log", json={"source": "test", "message": "hi"})
        assert r.status_code == 200
        assert db_module.log_list(source="test")[0]["message"] == "hi"

    def test_api_task_register_and_execute(self, client):
        r = client.post("/api/tasks", json={
            "id": "t1", "name": "T1", "trigger_type": "interval",
            "trigger_config": {"seconds": 60}})
        assert r.status_code == 200
        r = client.post("/api/tasks/t1/execute", json={"result": "done"})
        assert r.status_code == 200
        assert db_module.task_list()[0]["run_count"] == 1

    def test_api_alarm_and_recurring(self, client):
        r = client.post("/api/tasks/alarm", json={
            "name": "wake", "datetime": "2027-01-01T08:00:00", "message": "起床"})
        assert r.status_code == 200 and r.json()["id"].startswith("alarm_")
        r = client.post("/api/tasks/recurring", json={"name": "rec", "cron": "0 9 * * *"})
        assert r.status_code == 200 and r.json()["id"].startswith("recurring_")

    def test_api_kv_set(self, client):
        r = client.post("/api/kv/somekey", json={"value": "v1"})
        assert r.status_code == 200
        assert db_module.kv_get("somekey") == "v1"

    def test_api_engagement_record(self, client):
        r = client.post("/api/engagement", json={"event_type": "sent", "source": "test"})
        assert r.status_code == 200
        row = db_module.get_db().execute(
            "SELECT COUNT(*) FROM engagement_events").fetchone()
        assert row[0] == 1
