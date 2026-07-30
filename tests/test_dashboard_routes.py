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

PAGES = ["/", "/items", "/items/missing", "/signals", "/eigenflux", "/matters", "/memorials", "/tasks", "/bookmarks", "/settings", "/intentions",
         "/thinking", "/agent-calendar", "/engagement", "/ops"]


def test_notify_safely_ignores_deleted_page_slot(monkeypatch):
    from dashboard import uiutil

    def deleted_slot(*_args, **_kwargs):
        raise RuntimeError(
            "The parent element this slot belongs to has been deleted.")

    monkeypatch.setattr(uiutil.ui, "notify", deleted_slot)
    assert uiutil.notify_safely("done", type="positive") is False


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
    from dashboard.pages import (home, items, signals, eigenflux, memorials, tasks, settings,
                                 agent_calendar, engagement, ops)
    for mod in (home, items, signals, eigenflux, memorials, tasks, settings,
                agent_calendar, engagement, ops):
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
        # 说人话前缀 + 原始表达式必须保留（创建表单之外唯一露 cron 的地方）
        assert "0 9 * * *" in _parse_trigger_when(
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

    def test_rearm_stale_callback_does_not_overwrite_cancellation(self, test_db):
        import core.intentions as intents
        from dashboard.pages.intentions import rearm_intent

        ids = self._seed()
        intents.cancel_intent(ids["leak1"], reason="owner cancelled")

        assert rearm_intent(ids["leak1"]) is False
        assert intents.get_intent(ids["leak1"])["status"] == "cancelled"

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

    def test_rearm_interval_preserves_created_at_and_sets_next_fire(
            self, test_db):
        """Re-arm moves only the schedule watermark, not creation history."""
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
        assert row["created_at"] == old
        next_fire = datetime.fromisoformat(row["next_fire_at"])
        delta = (
            next_fire - now_local().replace(tzinfo=None)
        ).total_seconds()
        assert 58 * 60 < delta < 62 * 60

    def test_rearm_malformed_cron_leaves_it_expired(self, test_db):
        import core.intentions as intents
        from dashboard.pages.intentions import rearm_intent

        iid = intents.create_intent(
            name="bad cron",
            trigger_type="cron",
            trigger_config={"expression": "0 9 * * *"},
        )
        db = intents._get_db()
        db.execute(
            "UPDATE intentions SET status='expired',trigger_config=?,"
            "next_fire_at=NULL WHERE id=?",
            (json.dumps({"expression": "x * * * *"}), iid),
        )
        db.commit()

        assert rearm_intent(iid) is False
        assert intents.get_intent(iid)["status"] == "expired"
        assert intents.get_due_intents() == []

    @pytest.mark.parametrize("trigger_type", ["date", "cron", "interval"])
    def test_rearm_schedule_is_one_atomic_update(
            self, test_db, monkeypatch, trigger_type):
        import core.intentions as intents
        from dashboard.pages.intentions import rearm_intent

        config = {
            "date": {"datetime": "2026-01-01T09:00:00"},
            "cron": {"expression": "0 9 * * *"},
            "interval": {"seconds": 3600},
        }[trigger_type]
        iid = intents.create_intent(
            name=f"dead {trigger_type}",
            trigger_type=trigger_type,
            trigger_config=config,
            source="test",
        )
        db = intents._get_db()
        db.execute(
            "UPDATE intentions SET status='expired',attempt=3,"
            "expires_at='2026-01-01T00:00:00' WHERE id=?",
            (iid,),
        )
        db.commit()
        monkeypatch.setattr(
            intents,
            "update_intent",
            lambda *_args, **_kwargs: pytest.fail(
                "re-arm must not expose a partially committed pending row"),
        )
        statements = []
        db.set_trace_callback(statements.append)
        try:
            assert rearm_intent(iid)
        finally:
            db.set_trace_callback(None)

        updates = [
            statement for statement in statements
            if statement.lstrip().upper().startswith(
                "UPDATE INTENTIONS SET")
        ]
        assert len(updates) == 1

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
            # beta never spawned but skips empty_pre every minute — the
            # scheduler runs it, there's just nothing to do. Counting only
            # spawns false-flagged 7 healthy tasks on 2026-07-08.
            {"name": "beta", "interval": 300},     # skipping → ALIVE
            {"name": "gamma", "interval": 900},    # no events at all → dead
            {"name": "weekly", "interval": 7 * 86400},  # interval ≥ 6h → exempt
        ]
        dead = {d["name"] for d in detect_silently_dead(hb_tasks, events, now_ts=now_ts)}
        assert dead == {"gamma"}

    def test_silently_dead_three_intervals(self, tmp_path):
        from dashboard.pages.agent_calendar import detect_silently_dead
        telemetry.reset_cache()
        now_ts = self._seed_events(tmp_path)
        events = telemetry.read_sched_events(tmp_path)
        # alpha's newest event is the timeout 9m ago: dead when 3×interval
        # < 9m, alive above
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

    def test_source_engagement_report_empty_log(self, tmp_path):
        from dashboard.pages.engagement import source_engagement_report

        report = source_engagement_report(tmp_path, days=7)
        assert report["sources"] == []
        assert report["totals"]["sent"] == 0

    def test_source_engagement_report_empty_jsonl(self, tmp_path):
        from dashboard.pages.engagement import source_engagement_report

        (tmp_path / "engagement_log.jsonl").write_text("", encoding="utf-8")
        report = source_engagement_report(tmp_path, days=7)
        assert report["sources"] == []

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
# (5c) ops board — logs/events/queues from live files
# ═════════════════════════════════════════════════════════════════════════

class TestOpsBoard:
    def test_tail_log_flags_failure_signatures(self, tmp_path):
        from dashboard.pages.ops import tail_log

        log = tmp_path / "jarvis.log"
        log.write_text(
            "[ts] normal\n"
            "[ts] Script tasks/repos_sync_pre.sh timed out (60s)\n"
            "[ts] multi-task JSON parse failed\n"
            "[ts] Claude CLI not found\n",
            encoding="utf-8",
        )

        result = tail_log(log, lines=20)
        assert result["flagged_count"] == 3
        flagged = [row["text"] for row in result["lines"] if row["flagged"]]
        assert any("timed out" in row for row in flagged)
        assert any("Claude CLI not found" in row for row in flagged)

    def test_tail_log_info_stderr_transcript_not_flagged(self, tmp_path):
        """info 级的 'Script X stderr:' 转录是例行诊断——曾一天 40+ 条误报标红。"""
        import json as _json
        from dashboard.pages.ops import tail_log

        log = tmp_path / "jarvis.log"
        rows = [
            # 例行：脚本往 stderr 打的跳过说明，info 级 → 不红
            {"ts": "t", "level": "info", "component": "heartbeat",
             "msg": "Script tasks/intentions_pre.sh stderr: [calendar-intents] skip prep"},
            # 慢性 warn 无硬签名 → 不红（红=动手）
            {"ts": "t", "level": "warn", "msg": "tier_truncated"},
            # error 级必红
            {"ts": "t", "level": "error", "msg": "boom"},
            # info 级命中硬签名照红
            {"ts": "t", "level": "info", "msg": "subprocess exited with code 1"},
        ]
        text = "\n".join(_json.dumps(r) for r in rows)
        # 非结构化行沿用签名匹配（bot.sh 风格）
        text += "\n[ts] worker stderr: Traceback most recent\n"
        log.write_text(text + "\n", encoding="utf-8")

        result = tail_log(log, lines=20)
        flagged = [row["text"] for row in result["lines"] if row["flagged"]]
        assert result["flagged_count"] == 3
        assert not any("intentions_pre" in row for row in flagged)
        assert not any("tier_truncated" in row for row in flagged)
        assert any('"error"' in row for row in flagged)
        assert any("exited with code 1" in row for row in flagged)
        assert any("Traceback" in row for row in flagged)

    def test_tail_log_missing_file(self, tmp_path):
        from dashboard.pages.ops import tail_log

        result = tail_log(tmp_path / "nope.log")
        assert result["missing"] is True
        assert result["lines"] == []

    def test_tail_log_small_file_returns_all(self, tmp_path):
        from dashboard.pages.ops import tail_log

        log = tmp_path / "small.log"
        log.write_text("line1\nline2\nline3\n", encoding="utf-8")
        result = tail_log(log, lines=100)
        assert len(result["lines"]) == 3
        assert result["lines"][0]["text"] == "line1"
        assert result["missing"] is False

    def test_tail_log_grep_filter(self, tmp_path):
        from dashboard.pages.ops import tail_log

        log = tmp_path / "filtered.log"
        log.write_text("alpha\nbeta\nalpha again\ngamma\n", encoding="utf-8")
        result = tail_log(log, lines=100, grep="alpha")
        assert len(result["lines"]) == 2
        assert all("alpha" in r["text"] for r in result["lines"])

    def test_age_minutes_all_formats(self):
        from dashboard.pages.ops import _age_minutes
        from datetime import datetime

        now = datetime(2026, 6, 19, 12, 0, 0)
        assert _age_minutes("2026-06-19 11:30:00", now=now) == 30
        assert _age_minutes("2026-06-19 11:30", now=now) == 30
        assert _age_minutes("2026-06-19T11:30:00", now=now) == 30

    def test_age_minutes_malformed(self):
        from dashboard.pages.ops import _age_minutes

        assert _age_minutes("not-a-date") is None
        assert _age_minutes("") is None

    def test_queue_overview_reads_night_breach_jobs_and_delivery(self, tmp_path):
        from dashboard.pages.ops import queue_overview

        (tmp_path / "night_queue.jsonl").write_text(
            json.dumps({"ts": "2026-06-18 10:00", "source": "feed", "text": "x" * 300})
            + "\n",
            encoding="utf-8",
        )
        (tmp_path / "data").mkdir()
        (tmp_path / "data" / ".intent_breach_queue.jsonl").write_text(
            json.dumps({"id": "int_abc", "name": "missed"}) + "\n",
            encoding="utf-8",
        )
        (tmp_path / "jobs").mkdir()
        (tmp_path / "jobs" / "registry.json").write_text(json.dumps({
            "j1": {"status": "running", "description": "research", "pid": 123,
                   "started_at": "2026-06-18 10:00"},
            "j2": {"status": "completed", "description": "done"},
        }), encoding="utf-8")
        (tmp_path / ".delivery_state.json").write_text(
            json.dumps({"consec_fails": 2}), encoding="utf-8")

        result = queue_overview(tmp_path)

        assert len(result["night_queue"]) == 1
        assert len(result["night_queue"][0]["text"]) == 180
        assert result["breach_queue"][0]["id"] == "int_abc"
        assert result["jobs"]["counts"] == {"running": 1, "completed": 1}
        assert result["jobs"]["running"][0]["id"] == "j1"
        assert result["delivery_state"]["consec_fails"] == 2

    def test_ops_snapshot_summarizes_failures(self, tmp_path):
        from dashboard.pages.ops import ops_snapshot

        (tmp_path / "jarvis.log").write_text(
            "[ts] stderr: something bad\n", encoding="utf-8")
        (tmp_path / "daemon.log").write_text("[ts] ok\n", encoding="utf-8")
        _write_jsonl(tmp_path / "sched_events.jsonl", [
            {"ts": "2026-06-18 10:00:00", "event": "task_finish",
             "task": "alpha", "status": "ok"},
            {"ts": "2026-06-18 10:01:00", "event": "task_finish",
             "task": "beta", "status": "failed"},
            {"ts": "2026-06-18 10:02:00", "event": "task_timeout",
             "task": "gamma"},
            {"ts": "2026-06-18 10:03:00", "event": "task_skip",
             "task": "delta", "reason": "empty_pre"},
            {"ts": "2026-06-18 10:04:00", "event": "task_skip",
             "task": "epsilon", "reason": "pre_timeout"},
        ])

        snap = ops_snapshot(tmp_path)

        assert snap["flagged_count"] == 1
        assert len(snap["events"]) == 5
        assert [e["task"] for e in snap["failed_events"]] == ["beta", "gamma", "epsilon"]


# ═════════════════════════════════════════════════════════════════════════
# (5d) decision-first home + memorial inbox
# ═════════════════════════════════════════════════════════════════════════

class TestDecisionSurface:
    def test_home_hides_routine_polling_and_duplicate_card_deliveries(self, tmp_path):
        from dashboard.pages.home import build_activity_feed
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        _write_jsonl(tmp_path / "sched_events.jsonl", [
            {"ts": now, "event": "task_skip", "task": "poll",
             "reason": "empty_pre"},
            {"ts": now, "event": "task_skip", "task": "batch",
             "reason": "queued_quiet_hours"},
            {"ts": now, "event": "task_timeout", "task": "mail"},
            {"ts": now, "event": "intent_retry", "task": "int_internal"},
        ])
        _write_jsonl(tmp_path / "heartbeat_outbox.jsonl", [
            {"ts": now[:16], "text": "**📜 📡 card** body"},
            {"ts": now[:16], "text": "一条真正的非卡片变化"},
        ])
        feed = build_activity_feed(tmp_path)
        assert [e["message"] for e in feed] == [
            "mail 超时，系统会继续重试", "一条真正的非卡片变化"]

    def test_memorial_inbox_folds_pending_and_uses_specific_display_title(self, tmp_path):
        from dashboard.pages.memorials import memorial_states
        from dashboard.uiutil import memorial_display_title, memorial_option_label
        now = int(time.time())
        _write_jsonl(tmp_path / "memorials.jsonl", [
            {"ev": "create", "id": "m1", "epoch": now, "ts": "2026-07-12 09:00",
             "source": "eigenflux-feed-triage", "title": "EigenFlux",
             "body": "OpenAI 发布新的语音模型。后面是详情", "options": [],
             "extra_buttons": [], "context": ""},
        ])
        states = memorial_states(tmp_path)
        assert states[0]["status"] == "pending"
        assert memorial_display_title(states[0]) == "OpenAI 发布新的语音模型"
        assert memorial_option_label("重要，持续盯") == "标为重点"

    def test_all_time_signal_reader_folds_archives_and_live_ledger(
            self, tmp_path):
        from dashboard.telemetry import memorial_states_all, reset_cache

        _write_jsonl(tmp_path / "memorials.2026-05.jsonl", [
            {
                "ev": "create",
                "id": "archived",
                "epoch": 1,
                "ts": "2026-05-01 09:00",
                "source": "eigenflux-feed-triage",
                "title": "历史信号",
                "body": "归档后仍应可检索",
                "options": [],
                "extra_buttons": [],
                "context": "",
            },
        ])
        _write_jsonl(tmp_path / "memorials.jsonl", [
            {
                "ev": "create",
                "id": "live",
                "epoch": 2,
                "ts": "2026-07-25 09:00",
                "source": "metrics-digest",
                "title": "当前信号",
                "body": "仍在活动账本",
                "options": [],
                "extra_buttons": [],
                "context": "",
            },
        ])
        reset_cache()

        states = {
            state["id"]: state for state in memorial_states_all(tmp_path)
        }
        assert set(states) == {"archived", "live"}
        assert states["archived"]["_archived"] is True
        assert states["live"]["_archived"] is False

    def test_legacy_title_framing_is_hidden_without_mutating_the_ledger(self):
        from dashboard.uiutil import memorial_display_body, memorial_display_title
        state = {
            "title": "TITLE: 明天14:00 WAIC复盘",
            "body": "TITLE: 明天14:00 WAIC复盘 明天有一场会议。",
        }
        assert memorial_display_title(state) == "明天14:00 WAIC复盘"
        assert memorial_display_body(state) == "明天有一场会议。"
        generic = {
            "title": "Intent",
            "body": "TITLE: 10点心理咨询可带三个近况\n\n带一个真实样本。",
        }
        assert memorial_display_title(generic) == "10点心理咨询可带三个近况"
        assert memorial_display_body(generic) == "带一个真实样本。"

    def test_internal_eigenflux_cursor_error_is_humanized(self):
        from dashboard.uiutil import memorial_display_body

        body = memorial_display_body({
            "body": (
                "「Hesmos(赫斯墨斯)」的好友关系读取失败；"
                "可恢复记录已保留：friend readback pagination cursor repeated"
            ),
        })

        assert body == (
            "「Hesmos(赫斯墨斯)」的好友关系当时未核验完成；"
            "系统已保留进度并自动重试，不需要重复操作。"
        )
        assert "cursor" not in body

    def test_personal_memorials_rank_ahead_of_newer_ambient_feed(self):
        from dashboard.uiutil import memorial_attention_rank
        rows = [
            {"source": "eigenflux-feed-triage", "epoch": 200},
            {"source": "daily-reflect", "epoch": 100},
            {"source": "mail", "epoch": 50},
        ]
        rows.sort(key=memorial_attention_rank, reverse=True)
        assert [r["source"] for r in rows] == [
            "mail", "daily-reflect", "eigenflux-feed-triage"]

    def test_only_explicit_choices_count_as_pending_decisions(self):
        from dashboard.uiutil import (memorial_is_notice, memorial_is_pending,
                                      memorial_surface_label,
                                      memorial_visible_options)
        base = {"status": "pending", "delivery_status": "delivered",
                "extra_buttons": []}
        notice = {**base, "options": [
            {"key": "read", "label": "已阅"},
            {"key": "watch", "label": "标为重点"},
        ]}
        decision = {**base, "options": [
            {"key": "approve", "label": "同意"},
        ]}
        assert memorial_is_pending(notice) is False
        assert memorial_is_notice(notice) is True
        assert memorial_is_pending(decision) is True
        assert memorial_is_notice(decision) is False
        assert memorial_surface_label({
            **decision, "review_surface": "phone"
        }) == "手机集中批"
        assert memorial_surface_label({
            **decision, "review_surface": "lark"
        }) == "飞书即时批"
        assert memorial_surface_label({
            **notice, "attention": "alert"
        }) == "飞书提醒 · 无需批"
        invented_notice = {
            **notice,
            "attention": "notice",
            "options": [
                {"key": "r1", "label": "继续调研"},
                {"key": "r2", "label": "先别动"},
            ],
        }
        assert memorial_visible_options(invented_notice) == []
        assert [option["key"] for option in memorial_visible_options(notice)] == [
            "read", "watch"]

    def test_corrupt_epoch_does_not_blank_the_decision_surface(self):
        from dashboard.uiutil import memorial_attention_rank
        rows = [
            {"source": "mail", "epoch": "not-a-number"},
            {"source": "mail", "epoch": 50},
        ]
        rows.sort(key=memorial_attention_rank, reverse=True)  # must not raise
        assert rows[0]["epoch"] == 50


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

    @staticmethod
    def owner_headers():
        from core.mobile_access import consume_pair_code, create_pair_code
        paired = consume_pair_code(create_pair_code("owner test device")["code"])
        assert paired is not None
        return {"Authorization": f"Bearer {paired['token']}"}

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

    def test_matter_detail_page_renders(self, client):
        from core.matters import create_matter
        matter = create_matter("详情页", next_action="继续测试")
        r = client.get(f"/matters/{matter['id']}")
        assert r.status_code == 200

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

    def test_api_matter_lifecycle_and_links(self, client):
        created = client.post("/api/matters", json={
            "title": "API 事项", "next_action": "接入会话", "priority": 8,
        })
        assert created.status_code == 200
        matter_id = created.json()["id"]

        updated = client.patch(f"/api/matters/{matter_id}", json={
            "status": "waiting", "summary": "等待外部结果",
        })
        assert updated.status_code == 200
        assert updated.json()["status"] == "waiting"

        linked = client.post(f"/api/matters/{matter_id}/links", json={
            "entity_type": "session", "provider": "codex",
            "entity_id": "api-session", "title": "测试会话",
        })
        assert linked.status_code == 200
        loaded = client.get(f"/api/matters/{matter_id}")
        assert loaded.status_code == 200
        assert loaded.json()["links"][0]["entity_id"] == "api-session"

        removed = client.delete(
            f"/api/matters/{matter_id}/links/{linked.json()['id']}")
        assert removed.status_code == 200
        assert client.get(f"/api/matters/{matter_id}").json()["links"] == []

    def test_api_matter_validation_and_conflict(self, client):
        assert client.post("/api/matters", json={"title": ""}).status_code == 400
        first = client.post("/api/matters", json={"title": "甲"}).json()
        second = client.post("/api/matters", json={"title": "乙"}).json()
        payload = {"entity_type": "session", "provider": "claude",
                   "entity_id": "one-session"}
        assert client.post(f"/api/matters/{first['id']}/links", json=payload).status_code == 200
        conflict = client.post(f"/api/matters/{second['id']}/links", json=payload)
        assert conflict.status_code == 409
        assert client.get("/api/matters?status=not-real").status_code == 400

    def test_api_matter_context_binding_artifact_and_close_guard(self, client, tmp_path):
        from core import intentions
        from core.matter_bridge import bind_conversation

        matter = client.post("/api/matters", json={"title": "连续工作"}).json()
        matter_id = matter["id"]
        bind_conversation("api-owner", matter_id, destination_id="api-owner")
        bindings = client.get(f"/api/matters/{matter_id}/bindings")
        assert bindings.status_code == 200
        assert "openId=api-owner" in bindings.json()["items"][0]["deep_link"]

        context = client.get(f"/api/matters/{matter_id}/context?format=markdown")
        assert context.status_code == 200 and matter_id in context.text

        artifact = tmp_path / "result.txt"
        artifact.write_text("done", encoding="utf-8")
        link = client.post(f"/api/matters/{matter_id}/links", json={
            "entity_type": "artifact", "provider": "file",
            "entity_id": str(artifact), "title": "result.txt",
        }).json()
        downloaded = client.get(f"/api/matters/{matter_id}/artifacts/{link['id']}")
        assert downloaded.status_code == 200 and downloaded.text == "done"

        intentions.create_intent(
            name="等待确认", trigger_type="date",
            trigger_config={"datetime": "2027-01-01T08:00:00+08:00"},
            matter_id=matter_id,
        )
        blocked = client.patch(f"/api/matters/{matter_id}", json={"status": "done"})
        assert blocked.status_code == 409
        assert blocked.json()["detail"]["open_items"][0]["title"] == "等待确认"
        forced = client.patch(f"/api/matters/{matter_id}", json={
            "status": "done", "force": True,
        })
        assert forced.status_code == 200 and forced.json()["status"] == "done"

    def test_api_mobile_pairing_and_revocation(self, client):
        assert client.post(
            "/api/mobile/pair", json={"ttl": "bad"}
        ).status_code == 401
        headers = self.owner_headers()
        assert client.post(
            "/api/mobile/pair", json={"ttl": "bad"}, headers=headers
        ).status_code == 400
        paired = client.post(
            "/api/mobile/pair",
            json={"label": "test phone", "ttl": 5},
            headers=headers,
        )
        assert paired.status_code == 200
        from core.mobile_access import consume_pair_code
        device = consume_pair_code(paired.json()["code"])
        assert device is not None
        status = client.get("/api/mobile/status")
        assert status.status_code == 200
        assert "test phone" in {
            item["label"] for item in status.json()["devices"]
        }
        revoked = client.delete(
            f"/api/mobile/devices/{device['device_id']}",
            headers=headers,
        )
        assert revoked.status_code == 200

    def test_api_items_decision_and_delivery_confirmation(
            self, client, jarvis_tmp, monkeypatch):
        from core import memorial
        from core.delivery import DeliveryEnvelope, DeliveryPipeline

        monkeypatch.setattr(memorial, "JARVIS_DIR", jarvis_tmp)
        memorial_id, accepted = memorial.create(
            "test-items-api",
            "选择下一步",
            "请在手机上集中处理。",
            preset="decision",
            review_at="phone",
        )
        assert accepted is True

        listed = client.get("/api/items?mode=pending&time_window=all")
        assert listed.status_code == 200
        assert [row["id"] for row in listed.json()["items"]] == [memorial_id]

        decided = client.post(
            f"/api/items/{memorial_id}/decide",
            json={"option": "approve"},
            headers=self.owner_headers(),
        )
        assert decided.status_code == 200
        assert memorial.get_memorial(memorial_id)["status"] == "decided"

        pipeline = DeliveryPipeline(jarvis_tmp)
        sent = pipeline.deliver(DeliveryEnvelope(
            source="api-test",
            kind="web",
            payload={"text": "delivery state"},
            requested_channel="web",
            metadata={"bypass_throttle": True, "bypass_dedup": True},
        ))
        deliveries = client.get("/api/deliveries?state=delivered")
        assert deliveries.status_code == 200
        assert sent.delivery_id in {
            row["id"] for row in deliveries.json()["items"]
        }
        confirmed = client.post(
            f"/api/deliveries/{sent.delivery_id}/confirm",
            json={"state": "read"},
        )
        assert confirmed.status_code == 200
        assert confirmed.json()["state"] == "read"

    def test_owner_mutations_reject_direct_worker_http(
            self, client, jarvis_tmp, monkeypatch):
        from core.delegations import DelegationStore
        from core.iteration_loop import IterationStore

        monkeypatch.setenv("USER_ID", "owner")
        captured = client.post(
            "/api/delegations",
            json={
                "principal_id": "spoofed-owner",
                "source": "worker-http",
                "source_ref": "cannot-self-authorize",
                "title": "Publish externally",
                "operation": "public_publish",
                "risk_tier": 3,
                "target_type": "feed",
                "target_id": "public",
                "authority": "feed",
                "verification_policy": {"verifier": "feed"},
                "authorized": True,
            },
        )
        assert captured.status_code == 200
        assert captured.json()["delegation"]["principal_id"] == "owner"
        assert captured.json()["delegation"]["authorized"] == 0
        assert captured.json()["delegation"]["status"] == "needs_user"

        delegation_store = DelegationStore(root=jarvis_tmp)
        delegation, _ = delegation_store.create(
            principal_id="owner",
            source="test",
            source_ref="api-owner-boundary",
            title="Publish",
            operation="public_publish",
            risk_tier=3,
            target_type="feed",
            target_id="public",
            authority="feed",
            verification_policy={"verifier": "feed"},
        )
        iteration_store = IterationStore(root=jarvis_tmp)
        signal = iteration_store.record_signal(
            source="components",
            category="health",
            key="owner-boundary",
            severity="critical",
            summary="Owner review required",
            evidence={"ok": False},
        )
        proposal, _ = iteration_store.propose_from_signal(signal)

        assert client.post(
            f"/api/delegations/{delegation['id']}/confirm",
            json={"expected_version": 1, "principal_id": "owner"},
        ).status_code == 401
        assert client.post(
            f"/api/iteration/proposals/{proposal['id']}/review",
            json={"decision": "reject"},
        ).status_code == 401

        headers = self.owner_headers()
        confirmed = client.post(
            f"/api/delegations/{delegation['id']}/confirm",
            json={"expected_version": 1, "principal_id": "owner"},
            headers=headers,
        )
        rejected = client.post(
            f"/api/iteration/proposals/{proposal['id']}/review",
            json={"decision": "reject"},
            headers=headers,
        )
        assert confirmed.status_code == 200
        assert confirmed.json()["status"] == "bound"
        assert rejected.status_code == 200
        assert rejected.json()["status"] == "rejected"

    def test_api_retry_resolves_failed_delegation_attention(
            self, client, jarvis_tmp, monkeypatch):
        from core import memorial
        from core.delegation_reconcile import sync_attention_item
        from core.delegations import DelegationStore

        monkeypatch.setattr(memorial, "JARVIS_DIR", jarvis_tmp)
        store = DelegationStore(root=jarvis_tmp)
        delegation, _ = store.create(
            principal_id="owner",
            source="test",
            source_ref="api-retry-attention",
            title="Retry failed send",
            operation="message_send",
            target_type="agent",
            target_id="agent-1",
            authority="message_service",
            verification_policy={"verifier": "message"},
            authorized=True,
        )
        step = store.add_step(
            delegation["id"],
            expected_version=1,
            sequence=1,
            kind="message_send",
            executor="worker",
        )
        store.claim_step(
            delegation["id"],
            step["id"],
            expected_version=1,
            owner="worker",
        )
        store.record_attempt(
            delegation["id"],
            step["id"],
            expected_version=1,
            owner="worker",
            succeeded=False,
            error_code="provider_unavailable",
        )
        detail = store.get(delegation["id"])
        memorial_id = sync_attention_item(
            detail,
            store=store,
            send=False,
        )

        response = client.post(
            f"/api/delegations/{delegation['id']}/retry",
            json={"expected_version": 1},
            headers=self.owner_headers(),
        )

        assert response.status_code == 200
        assert response.json()["status"] == "bound"
        assert memorial.get_memorial(memorial_id)["resolved_label"] == "已处理"

    def test_api_cross_device_handoff_lifecycle(
            self, client, jarvis_tmp, monkeypatch):
        from core import memorial
        monkeypatch.setattr(memorial, "JARVIS_DIR", jarvis_tmp)
        memorial_id, _ = memorial.create(
            "api", "回电脑继续", "完整背景", preset="fyi", send=False)
        created = client.post("/api/handoffs", json={
            "entity_type": "memorial",
            "entity_id": memorial_id,
            "from_surface": "mobile",
            "to_surface": "desktop",
            "title": "回电脑继续",
        }, headers={"X-Jarvis-Device": "dev_test_phone"})
        assert created.status_code == 200
        handoff = created.json()
        assert handoff["created"] is True
        assert handoff["created_by"] == "dev_test_phone"

        duplicate = client.post("/api/handoffs", json={
            "entity_type": "memorial",
            "entity_id": memorial_id,
            "from_surface": "mobile",
            "to_surface": "desktop",
        }, headers={"X-Jarvis-Device": "dev_test_phone"})
        assert duplicate.status_code == 200
        assert duplicate.json()["id"] == handoff["id"]
        assert duplicate.json()["created"] is False

        listed = client.get(
            "/api/handoffs?target_surface=desktop&status=active")
        assert listed.status_code == 200
        assert [row["id"] for row in listed.json()["items"]] == [handoff["id"]]
        wrong = client.post(
            f"/api/handoffs/{handoff['id']}/claim",
            json={"surface": "mobile"},
        )
        assert wrong.status_code == 403
        claimed = client.post(
            f"/api/handoffs/{handoff['id']}/claim",
            json={"surface": "desktop"},
        )
        assert claimed.status_code == 200
        assert claimed.json()["status"] == "claimed"
        wrong_complete = client.post(
            f"/api/handoffs/{handoff['id']}/complete",
            json={},
            headers={"X-Jarvis-Device": "dev_test_phone"},
        )
        assert wrong_complete.status_code == 403
        completed = client.post(
            f"/api/handoffs/{handoff['id']}/complete", json={})
        assert completed.status_code == 200
        assert completed.json()["status"] == "completed"

        invalid = client.post("/api/handoffs", json={
            "entity_type": "memorial",
            "entity_id": "mem_invalid",
            "from_surface": "mobile",
            "to_surface": "mobile",
        }, headers={"X-Jarvis-Device": "dev_test_phone"})
        assert invalid.status_code == 400
        spoofed = client.post("/api/handoffs", json={
            "entity_type": "memorial",
            "entity_id": memorial_id,
            "from_surface": "mobile",
            "to_surface": "desktop",
        })
        assert spoofed.status_code == 403
        missing = client.post("/api/handoffs", json={
            "entity_type": "memorial",
            "entity_id": "mem_missing",
            "from_surface": "desktop",
            "to_surface": "mobile",
        })
        assert missing.status_code == 404

    def test_api_work_sessions(self, client, monkeypatch):
        import core.work_sessions as work_sessions
        monkeypatch.setattr(work_sessions, "discover_sessions", lambda **kwargs: [{
            "provider": kwargs.get("provider") or "codex", "session_id": "s1",
        }])
        response = client.get("/api/work-sessions?provider=codex&limit=3")
        assert response.status_code == 200
        assert response.json()["items"][0]["session_id"] == "s1"

    def test_pwa_assets_are_served(self, client):
        manifest = client.get("/static/manifest.webmanifest")
        assert manifest.status_code == 200
        assert manifest.json()["start_url"] == "/items"
        worker = client.get("/sw.js")
        assert worker.status_code == 200
        assert worker.headers["service-worker-allowed"] == "/"

    # ── write-route hardening: no auth on :3457, so a foreign webpage could
    # blind-POST via a CORS "simple request" (text/plain, no preflight) and
    # e.g. inject attacker text into Lark through a notify task. ──

    def test_write_rejects_non_json_content_type(self, client):
        r = client.post("/api/log", content='{"source": "evil", "message": "x"}',
                        headers={"Content-Type": "text/plain"})
        assert r.status_code == 415
        r = client.post("/api/tasks",
                        content="id=x&name=y",
                        headers={"Content-Type": "application/x-www-form-urlencoded"})
        assert r.status_code == 415
        assert db_module.log_list(source="evil") == []

    def test_write_rejects_foreign_origin(self, client):
        r = client.post("/api/log",
                        json={"source": "evil", "message": "x"},
                        headers={"Origin": "https://evil.example.com"})
        assert r.status_code == 403
        # DELETE has no Content-Type but must still honor the Origin check
        r = client.delete("/api/bookmarks/1",
                          headers={"Origin": "https://evil.example.com"})
        assert r.status_code == 403
        assert db_module.log_list(source="evil") == []

    def test_write_allows_local_origin_and_no_origin(self, client):
        # browser on the dashboard itself
        r = client.post("/api/log", json={"source": "ok1", "message": "x"},
                        headers={"Origin": "http://127.0.0.1:3457"})
        assert r.status_code == 200
        r = client.post("/api/log", json={"source": "ok2", "message": "x"},
                        headers={"Origin": "http://localhost:3457"})
        assert r.status_code == 200
        # curl / bot.sh: no Origin at all (the json= above already covers it,
        # but pin the DELETE shape: no body, no Content-Type)
        bm = db_module.bookmark_add("t", "https://del.example")
        r = client.delete(f"/api/bookmarks/{bm}")
        assert r.status_code == 200

    # ── registration-time validation: poison triggers and executor-less
    # action types must never reach the table. ──

    def test_api_task_register_rejects_bad_cron(self, client):
        r = client.post("/api/tasks", json={
            "id": "bad1", "name": "bad", "trigger_type": "cron",
            "trigger_config": {"expression": "a b c d e"}})
        assert r.status_code == 400
        assert "cron" in r.json()["detail"]
        assert db_module.task_list() == []

    def test_api_task_register_rejects_bad_json_config(self, client):
        r = client.post("/api/tasks", json={
            "id": "bad2", "name": "bad", "trigger_type": "interval",
            "trigger_config": "not a dict"})
        assert r.status_code == 400
        assert db_module.task_list() == []

    def test_api_task_register_rejects_prompt_action(self, client):
        r = client.post("/api/tasks", json={
            "id": "bad3", "name": "bad", "trigger_type": "interval",
            "trigger_config": {"seconds": 60}, "action_type": "prompt"})
        assert r.status_code == 400
        assert "executor" in r.json()["detail"]
        r = client.post("/api/tasks/recurring", json={
            "name": "bad", "cron": "0 9 * * *", "action_type": "script"})
        assert r.status_code == 400
        assert db_module.task_list() == []

    def test_api_alarm_rejects_bad_datetime(self, client):
        r = client.post("/api/tasks/alarm", json={
            "name": "bad", "datetime": "tomorrow-ish", "message": "x"})
        assert r.status_code == 400
        assert db_module.task_list() == []

    def test_api_health_reports_real_db_state(self, client):
        r = client.get("/api/health")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        assert body["db"] == "connected"

    def test_api_provider_health_exposes_status_without_credentials(
        self, client, monkeypatch
    ):
        import core.provider_health as provider_health

        monkeypatch.setattr(
            provider_health,
            "snapshot",
            lambda: {
                "version": 1,
                "updated_at": "2026-07-24T12:00:00+08:00",
                "providers": [
                    {
                        "id": "backup1",
                        "label": "Claude backup",
                        "status": "healthy",
                        "requested_model": "relay-opus",
                        "actual_model": "relay-opus",
                        "checked_at": "2026-07-24T12:00:00+08:00",
                        "detail": "bounded canary answered",
                    }
                ],
            },
        )

        response = client.get("/api/provider-health")

        assert response.status_code == 200
        assert response.json()["providers"][0]["status"] == "healthy"
        assert "token" not in response.text.lower()

    def test_push_test_targets_the_authenticated_device(
            self, client, monkeypatch):
        from core import mobile_access

        headers = self.owner_headers()
        token = headers["Authorization"].split(" ", 1)[1]
        device_id = token.split(".", 1)[0]
        headers["X-Jarvis-Device"] = device_id
        captured = {}

        def fake_send_push(*_args, **kwargs):
            captured.update(kwargs)
            return {"sent": 1, "failed": 0, "disabled": 0}

        monkeypatch.setattr(mobile_access, "send_push", fake_send_push)
        response = client.post(
            "/api/mobile/push-test",
            json={"body": "test"},
            headers=headers,
        )

        assert response.status_code == 200
        assert captured["device_id"] == device_id
