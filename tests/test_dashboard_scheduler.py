"""Tests for the 2026-07-21 dashboard scheduler/db audit fixes.

Covers:
- tz-aware now_local() vs naive stored strings no longer TypeErrors the
  whole due-check (date triggers + not_already_done windows)
- per-task isolation: one poison row (bad JSON / bad cron) is skipped,
  the rest of the due-check survives
- validate_trigger registration-time gate (and db.task_register backstop)
- cron minute dedupe: a matching minute fires once, not once per ~10s poll
- prompt/script actions rejected at registration; legacy rows are not
  falsely marked executed by check_dynamic_tasks
- check_dynamic_tasks per-task isolation (bad action_config)
- mark_executed atomic UPDATE
- bookmark_search FTS operator input falls back to LIKE
- engagement_stats replied≤sent cap + mtime/size cache
- bookmark_pipeline spaced-repetition interval compares in LOCAL time
- db._db_path resolves JARVIS_DIR at call time (monkeypatched DB_PATH wins)
"""

import json
import os
import sys
import tempfile
import time
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import dashboard.db as db_module
from core.timeutil import now_local

LOCAL_FMT = "%Y-%m-%dT%H:%M:%S"


def setup_test_db():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    db_module.DB_PATH = Path(tmp.name)
    db_module._connection = None
    return tmp.name


def teardown_test_db(path):
    try:
        if db_module._connection:
            db_module._connection.close()
            db_module._connection = None
        db_module.DB_PATH = db_module._DEFAULT_DB_PATH
        os.unlink(path)
    except Exception:
        pass


def _insert_raw_task(task_id, trigger_type="interval", trigger_config='{"seconds": 1}',
                     action_type="notify", action_config='{"message": "hi"}',
                     conditions='[]', last_run_at=None):
    """Insert a row directly, bypassing registration-time validation —
    the shape of legacy/poison rows already in the live table."""
    db = db_module.get_db()
    db.execute(
        """INSERT OR REPLACE INTO scheduled_tasks
           (id, name, category, trigger_type, trigger_config, conditions,
            action_type, action_config, priority, enabled, created_at, last_run_at)
           VALUES (?, ?, 'user', ?, ?, ?, ?, ?, 5, 1, ?, ?)""",
        (task_id, task_id, trigger_type, trigger_config, conditions,
         action_type, action_config, now_local().strftime(LOCAL_FMT), last_run_at),
    )
    db.commit()


class SchedulerDBTest:
    def setup_method(self):
        self.db_path = setup_test_db()
        # per-process warn-once sets must not leak state between tests
        import dashboard.scheduler as sched
        import dashboard.heartbeat_bridge as hb
        sched._warned_task_ids.clear()
        hb._warned_task_ids.clear()

    def teardown_method(self):
        teardown_test_db(self.db_path)


# ── (1) timezone crash ───────────────────────────────────────────────────

class TestTimezoneCoercion(SchedulerDBTest):
    def test_naive_past_date_trigger_is_due_not_typeerror(self):
        from dashboard.scheduler import get_due_tasks
        past = (now_local() - timedelta(minutes=5)).strftime(LOCAL_FMT)  # naive
        db_module.task_register(
            task_id="d1", name="naive date", trigger_type="date",
            trigger_config={"datetime": past}, action_type="notify",
            action_config={"message": "x"})
        due = get_due_tasks()
        assert [t["id"] for t in due] == ["d1"]

    def test_naive_future_date_trigger_not_due(self):
        from dashboard.scheduler import get_due_tasks
        future = (now_local() + timedelta(days=1)).strftime(LOCAL_FMT)
        db_module.task_register(
            task_id="d2", name="future", trigger_type="date",
            trigger_config={"datetime": future}, action_type="notify",
            action_config={"message": "x"})
        assert get_due_tasks() == []

    def test_one_naive_date_task_does_not_kill_other_tasks(self):
        """The audited failure shape: the TypeError killed the WHOLE check."""
        from dashboard.scheduler import get_due_tasks
        past = (now_local() - timedelta(minutes=5)).strftime(LOCAL_FMT)
        db_module.task_register(
            task_id="d3", name="naive", trigger_type="date",
            trigger_config={"datetime": past}, action_type="notify",
            action_config={"message": "x"})
        db_module.task_register(
            task_id="i1", name="interval", trigger_type="interval",
            trigger_config={"seconds": 1}, action_type="notify",
            action_config={"message": "y"})
        assert {t["id"] for t in get_due_tasks()} == {"d3", "i1"}

    def test_not_already_done_window_with_naive_last_run(self):
        from dashboard.scheduler import check_conditions
        last = (now_local() - timedelta(minutes=10)).strftime(LOCAL_FMT)
        task = {"last_run_at": last}
        # ran 10 min ago: inside a 1h window, outside a 5m window
        assert not check_conditions([{"type": "not_already_done", "window": "1h"}], task)
        assert check_conditions([{"type": "not_already_done", "window": "5m"}], task)


# ── (2) per-task isolation + validate_trigger ────────────────────────────

class TestPerTaskIsolation(SchedulerDBTest):
    def test_poison_rows_do_not_kill_due_check(self):
        from dashboard.scheduler import get_due_tasks
        _insert_raw_task("bad_json", trigger_config="not json at all")
        _insert_raw_task("bad_cron", trigger_type="cron",
                         trigger_config='{"expression": "a b c d e"}')
        _insert_raw_task("bad_date", trigger_type="date",
                         trigger_config='{"datetime": "yesterday-ish"}')
        _insert_raw_task("good", trigger_type="interval",
                         trigger_config='{"seconds": 1}')
        due = get_due_tasks()
        assert [t["id"] for t in due] == ["good"]
        # and it stays stable on repeat calls (warn-once, no crash)
        assert [t["id"] for t in get_due_tasks()] == ["good"]

    def test_validate_trigger(self):
        from dashboard.scheduler import validate_trigger
        # good
        assert validate_trigger("cron", {"expression": "0 9 * * 1"}) is None
        assert validate_trigger("cron", '{"expression": "*/5 * * * *"}') is None
        assert validate_trigger("interval", {"seconds": 60}) is None
        assert validate_trigger("date", {"datetime": "2026-07-21T08:00:00"}) is None
        # bad
        assert validate_trigger("cron", {"expression": "a b c d e"})
        assert validate_trigger("cron", {"expression": "0 9 * *"})
        assert validate_trigger("cron", {})
        assert validate_trigger("interval", {"seconds": -5})
        assert validate_trigger("interval", {"seconds": "soon"})
        assert validate_trigger("date", {"datetime": "not-a-date"})
        assert validate_trigger("date", {})
        assert validate_trigger("cron", "not json")
        assert validate_trigger("cron", ["not", "a", "dict"])
        assert validate_trigger("carrier_pigeon", {})
        # out-of-range values register-then-never-fire without bounds checks
        assert validate_trigger("cron", {"expression": "60 * * * *"})
        assert validate_trigger("cron", {"expression": "1-100 * * * *"})
        assert validate_trigger("cron", {"expression": "0 24 * * *"})
        assert validate_trigger("cron", {"expression": "0 0 0 * *"})
        # cron's 7==Sunday tolerance must survive the bounds check
        assert validate_trigger("cron", {"expression": "0 9 * * 7"}) is None

    def test_task_register_rejects_poison_row(self):
        import pytest
        with pytest.raises(ValueError):
            db_module.task_register(
                task_id="p1", name="poison", trigger_type="cron",
                trigger_config={"expression": "a b c d e"},
                action_type="notify", action_config={"message": "x"})
        assert db_module.task_list() == []


# ── (3) cron minute dedupe ───────────────────────────────────────────────

class TestCronMinuteDedupe(SchedulerDBTest):
    def _task(self, last_run_at):
        return {
            "id": "c1", "trigger_type": "cron",
            "trigger_config": '{"expression": "* * * * *"}',
            "conditions": "[]", "last_run_at": last_run_at,
        }

    def test_fires_once_per_matching_minute(self):
        from dashboard.scheduler import _task_is_due
        now = now_local()
        now_ts = int(time.time())
        # never run → due
        assert _task_is_due(self._task(None), now, now_ts)
        # already ran THIS minute → not due again (the ~10s poll dedupe)
        same_minute = now.strftime(LOCAL_FMT)
        assert not _task_is_due(self._task(same_minute), now, now_ts)
        # ran the previous minute → due again
        prev_minute = (now - timedelta(minutes=1)).strftime(LOCAL_FMT)
        assert _task_is_due(self._task(prev_minute), now, now_ts)

    def test_end_to_end_mark_executed_suppresses_refire(self):
        from dashboard.scheduler import get_due_tasks, mark_executed
        db_module.task_register(
            task_id="c2", name="every minute", trigger_type="cron",
            trigger_config={"expression": "* * * * *"},
            action_type="notify", action_config={"message": "x"})
        first = get_due_tasks()
        assert [t["id"] for t in first] == ["c2"]
        mark_executed("c2")
        # immediately re-polling within the same minute must NOT re-fire.
        # (Only flaky if the wall clock crosses a minute boundary between the
        # two calls — retry once on a fresh minute in that case.)
        before = now_local().minute
        again = get_due_tasks()
        if now_local().minute == before:
            assert again == []


# ── (4) prompt/script honesty ────────────────────────────────────────────

class TestActionTypeHonesty(SchedulerDBTest):
    def test_register_from_action_rejects_non_notify(self):
        from dashboard.heartbeat_bridge import register_from_action
        msg = register_from_action(
            "name=sneaky|type=interval|config=60|action=prompt|message=x")
        assert "no" in msg and "executor" in msg
        assert db_module.task_list() == []

    def test_register_from_action_surfaces_bad_trigger(self):
        from dashboard.heartbeat_bridge import register_from_action
        msg = register_from_action(
            "name=badcron|type=cron|config=a b c d e|action=notify|message=x")
        assert msg.startswith("Cannot register")
        assert db_module.task_list() == []

    def test_legacy_prompt_row_not_marked_executed(self):
        from dashboard.heartbeat_bridge import check_dynamic_tasks
        _insert_raw_task("legacy_prompt", action_type="prompt",
                         action_config='{"prompt": "do things"}')
        result = check_dynamic_tasks()
        assert result == ""  # nothing executable
        row = db_module.get_db().execute(
            "SELECT run_count, last_run_at FROM scheduled_tasks WHERE id='legacy_prompt'"
        ).fetchone()
        assert row[0] == 0 and row[1] is None  # NOT falsely marked executed

    def test_bad_action_config_does_not_kill_other_due_tasks(self):
        from dashboard.heartbeat_bridge import check_dynamic_tasks
        _insert_raw_task("aaa_bad", action_config="not json")  # sorts first
        _insert_raw_task("zzz_good", action_config='{"message": "hello"}')
        result = check_dynamic_tasks()
        data = json.loads(result)
        assert [t["id"] for t in data["tasks"]] == ["zzz_good"]
        # the poison row was not marked executed
        row = db_module.get_db().execute(
            "SELECT run_count FROM scheduled_tasks WHERE id='aaa_bad'").fetchone()
        assert row[0] == 0


# ── (8) mark_executed atomic ─────────────────────────────────────────────

class TestMarkExecuted(SchedulerDBTest):
    def test_increments_and_stamps(self):
        from dashboard.scheduler import mark_executed
        _insert_raw_task("m1")
        mark_executed("m1", result="ok")
        mark_executed("m1", result="ok2")
        row = db_module.get_db().execute(
            "SELECT run_count, last_run_at, last_result FROM scheduled_tasks WHERE id='m1'"
        ).fetchone()
        assert row[0] == 2
        assert row[1] and row[2] == "ok2"

    def test_null_run_count_coalesced(self):
        from dashboard.scheduler import mark_executed
        _insert_raw_task("m2")
        db_module.get_db().execute(
            "UPDATE scheduled_tasks SET run_count = NULL WHERE id='m2'")
        db_module.get_db().commit()
        mark_executed("m2")
        row = db_module.get_db().execute(
            "SELECT run_count FROM scheduled_tasks WHERE id='m2'").fetchone()
        assert row[0] == 1

    def test_unknown_task_is_noop(self):
        from dashboard.scheduler import mark_executed
        mark_executed("nonexistent")  # must not raise


# ── (6) FTS operator input ───────────────────────────────────────────────

class TestSearchFallback(SchedulerDBTest):
    def test_operator_input_falls_back_to_like(self):
        db_module.bookmark_add("Learn C++ fast", "https://cpp.example")
        db_module.bookmark_add("Rust intro", "https://rust.example")
        results = db_module.bookmark_search("c++")  # FTS syntax error shape
        assert [r["title"] for r in results] == ["Learn C++ fast"]

    def test_unbalanced_quote_does_not_raise(self):
        db_module.bookmark_add('He said "hello', "https://q.example")
        results = db_module.bookmark_search('said "hello')
        assert len(results) == 1

    def test_like_fallback_escapes_wildcards(self):
        db_module.bookmark_add("100% legit", "https://pct.example")
        db_module.bookmark_add("100X legit", "https://x.example")
        # '%' is an FTS syntax error → LIKE path; the literal % must not
        # match "100X legit" as a wildcard
        results = db_module.bookmark_search("100% legit")
        assert [r["title"] for r in results] == ["100% legit"]

    def test_normal_fts_query_still_ranked(self):
        db_module.bookmark_add("Transformers paper", summary="attention")
        db_module.bookmark_add("Cooking", summary="pasta")
        assert [r["title"] for r in db_module.bookmark_search("attention")] == [
            "Transformers paper"]


# ── (7) engagement_stats cap + cache ─────────────────────────────────────

class TestEngagementStatsHonesty(SchedulerDBTest):
    def _write_log(self, tmp_path, entries):
        (tmp_path / "engagement_log.jsonl").write_text(
            "\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")

    def test_replied_capped_at_sent(self, tmp_path, monkeypatch):
        monkeypatch.setenv("JARVIS_DIR", str(tmp_path))
        epoch = int(time.time())
        self._write_log(tmp_path, [
            {"source": "heartbeat", "type": "sent", "epoch": epoch},
            {"source": "heartbeat", "type": "response", "reaction": "engaged",
             "epoch": epoch},
            {"source": "heartbeat", "type": "response", "reaction": "late_reply",
             "epoch": epoch},  # historical duplicate credit
        ])
        stats = db_module.engagement_stats(7)
        assert stats["total"] == 1
        assert stats["engaged"] == 1  # capped, not 2
        assert stats["rate"] == 100.0  # never >100
        assert stats["by_source"] == [
            {"source": "heartbeat", "total": 1, "engaged_count": 1}]

    def test_cache_hits_on_unchanged_file_and_refreshes_on_change(self, tmp_path, monkeypatch):
        monkeypatch.setenv("JARVIS_DIR", str(tmp_path))
        epoch = int(time.time())
        self._write_log(tmp_path, [
            {"source": "a", "type": "sent", "epoch": epoch}])
        first = db_module.engagement_stats(7)
        assert first["total"] == 1
        # unchanged file → cached object comes back (identity check)
        assert db_module.engagement_stats(7) is first
        # append (size changes) → recomputed
        with open(tmp_path / "engagement_log.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps({"source": "a", "type": "sent", "epoch": epoch}) + "\n")
        assert db_module.engagement_stats(7)["total"] == 2

    def test_missing_file_returns_zeros(self, tmp_path, monkeypatch):
        monkeypatch.setenv("JARVIS_DIR", str(tmp_path))
        stats = db_module.engagement_stats(7)
        assert stats == {"total": 0, "engaged": 0, "rate": 0, "by_source": []}


# ── (10) bookmark_pipeline local-time intervals ──────────────────────────

class TestSpacedRepetitionLocalTime(SchedulerDBTest):
    def test_due_item_surfaces_without_utc_skew(self):
        """25h since last surface with a 1-day interval must be due. Under the
        old julianday('now') (UTC) vs local-naive string comparison, every
        interval ran 8h long in +08:00 (due only after 32h)."""
        from dashboard.bookmark_pipeline import get_resurface_candidates
        bm_id = db_module.bookmark_add("Spaced item", "https://spaced.example")
        db_module.bookmark_update(
            bm_id, status="triaged", surfaced_count=1,
            last_surfaced_at=(now_local() - timedelta(hours=25)).strftime(LOCAL_FMT))
        assert bm_id in {c["id"] for c in get_resurface_candidates(5)}

    def test_not_due_item_stays_hidden(self):
        from dashboard.bookmark_pipeline import get_resurface_candidates
        # 'reading' is only reachable via the spaced branch ('triaged' would
        # also qualify for the random serendipity pick)
        bm_id = db_module.bookmark_add("Fresh item", "https://fresh.example")
        db_module.bookmark_update(
            bm_id, status="reading", surfaced_count=1,
            last_surfaced_at=(now_local() - timedelta(hours=2)).strftime(LOCAL_FMT))
        assert bm_id not in {c["id"] for c in get_resurface_candidates(5)}


# ── (11) call-time DB path ───────────────────────────────────────────────

class TestDbPathResolution:
    def test_jarvis_dir_honored_at_call_time(self, tmp_path, monkeypatch):
        monkeypatch.setattr(db_module, "DB_PATH", db_module._DEFAULT_DB_PATH)
        monkeypatch.delenv("JARVIS_DB_PATH", raising=False)
        monkeypatch.setenv("JARVIS_DIR", str(tmp_path))
        assert db_module._db_path() == tmp_path / "data" / "jarvis.db"

    def test_monkeypatched_db_path_wins_over_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("JARVIS_DIR", str(tmp_path / "env_dir"))
        monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "patched.db")
        assert db_module._db_path() == tmp_path / "patched.db"

    def test_jarvis_db_path_wins_over_runtime_root(self, tmp_path, monkeypatch):
        monkeypatch.setattr(db_module, "DB_PATH", db_module._DEFAULT_DB_PATH)
        monkeypatch.setenv("JARVIS_DIR", str(tmp_path / "runtime"))
        monkeypatch.setenv("JARVIS_DB_PATH", str(tmp_path / "shared.db"))
        assert db_module._db_path() == tmp_path / "shared.db"

    def test_default_without_env(self, monkeypatch):
        monkeypatch.setattr(db_module, "DB_PATH", db_module._DEFAULT_DB_PATH)
        monkeypatch.delenv("JARVIS_DIR", raising=False)
        monkeypatch.delenv("JARVIS_DB_PATH", raising=False)
        assert db_module._db_path() == db_module._DEFAULT_DB_PATH

    def test_get_db_creates_under_jarvis_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr(db_module, "DB_PATH", db_module._DEFAULT_DB_PATH)
        monkeypatch.delenv("JARVIS_DB_PATH", raising=False)
        monkeypatch.setenv("JARVIS_DIR", str(tmp_path))
        old_conn, db_module._connection = db_module._connection, None
        try:
            db_module.get_db()
            assert (tmp_path / "data" / "jarvis.db").exists()
        finally:
            if db_module._connection is not None:
                db_module._connection.close()
            db_module._connection = old_conn
