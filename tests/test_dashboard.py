"""Tests for the dashboard subsystem."""

import json
import os
import sys
import tempfile
import time
from pathlib import Path

# Ensure project root is importable
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

# Override DB path for tests
import dashboard.db as db_module


def setup_test_db():
    """Create a temp DB for testing."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    db_module.DB_PATH = Path(tmp.name)
    db_module._connection = None  # Reset singleton
    return tmp.name


def teardown_test_db(path):
    """Clean up test DB."""
    try:
        if db_module._connection:
            db_module._connection.close()
            db_module._connection = None
        os.unlink(path)
    except Exception:
        pass


class TestDatabase:
    def setup_method(self):
        self.db_path = setup_test_db()

    def teardown_method(self):
        teardown_test_db(self.db_path)

    def test_migrations_run(self):
        db = db_module.get_db()
        tables = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        table_names = {r[0] for r in tables}
        assert "scheduled_tasks" in table_names
        assert "bookmarks" in table_names
        assert "agent_log" in table_names
        assert "kv_store" in table_names
        assert "surface_handoffs" in table_names

    def test_bookmark_add_and_list(self):
        bm_id = db_module.bookmark_add("Test Article", "https://example.com", "test")
        assert bm_id > 0
        items = db_module.bookmark_list()
        assert len(items) == 1
        assert items[0]["title"] == "Test Article"
        assert items[0]["url"] == "https://example.com"

    def test_bookmark_dedup(self):
        id1 = db_module.bookmark_add("A", "https://same.url")
        id2 = db_module.bookmark_add("B", "https://same.url")
        assert id1 == id2
        items = db_module.bookmark_list()
        assert len(items) == 1

    def test_bookmark_search(self):
        db_module.bookmark_add("Machine Learning Paper", "https://ml.com",
                               summary="A paper about transformers")
        db_module.bookmark_add("Cooking Recipe", "https://food.com",
                               summary="How to make pasta")
        results = db_module.bookmark_search("transformers")
        assert len(results) == 1
        assert results[0]["title"] == "Machine Learning Paper"

    def test_bookmark_update(self):
        bm_id = db_module.bookmark_add("Test", "https://test.com")
        db_module.bookmark_update(bm_id, status="reading", tags=["ai", "test"])
        items = db_module.bookmark_list(status="reading")
        assert len(items) == 1
        assert json.loads(items[0]["tags"]) == ["ai", "test"]

    def test_kv_store(self):
        db_module.kv_set("test_key", "test_value")
        assert db_module.kv_get("test_key") == "test_value"
        assert db_module.kv_get("nonexistent", "default") == "default"

    def test_log_event(self):
        db_module.log_event("test", "Something happened", context={"detail": "x"})
        logs = db_module.log_list(source="test")
        assert len(logs) == 1
        assert logs[0]["message"] == "Something happened"

    def test_task_register_and_list(self):
        db_module.task_register(
            task_id="test_alarm",
            name="Wake up",
            trigger_type="date",
            trigger_config={"datetime": "2026-05-22T06:00:00"},
            action_type="notify",
            action_config={"message": "起床了"},
        )
        tasks = db_module.task_list()
        assert len(tasks) == 1
        assert tasks[0]["name"] == "Wake up"

    def test_engagement_stats(self, tmp_path, monkeypatch):
        # Stats read engagement_log.jsonl (the source of truth written by the
        # bot), NOT the engagement_events table — the table only ever received
        # writes from an uncalled dashboard endpoint and froze on 2026-05-21.
        import json as _json
        import time as _time
        monkeypatch.setenv("JARVIS_DIR", str(tmp_path))
        epoch = int(_time.time())
        ts = _time.strftime("%Y-%m-%d %H:%M", _time.localtime(epoch))
        lines = [
            {"ts": ts, "source": "checkin", "type": "sent", "epoch": epoch},
            {"ts": ts, "source": "checkin", "type": "sent", "epoch": epoch},
            {"ts": ts, "source": "checkin", "type": "response", "reaction": "engaged"},
            {"ts": ts, "source": "checkin", "type": "response", "reaction": "ignored"},
        ]
        (tmp_path / "engagement_log.jsonl").write_text(
            "\n".join(_json.dumps(e) for e in lines) + "\n")

        stats = db_module.engagement_stats(7)
        assert stats["total"] == 2
        assert stats["engaged"] == 1
        assert stats["rate"] == 50.0
        assert stats["by_source"] == [
            {"source": "checkin", "total": 2, "engaged_count": 1}]


class TestScheduler:
    def setup_method(self):
        self.db_path = setup_test_db()

    def teardown_method(self):
        teardown_test_db(self.db_path)

    def test_cron_matches(self):
        """STANDARD cron dow semantics: 0/7=Sunday, 1=Monday ... 6=Saturday.

        The old comparison used dt.weekday() (0=Monday) — every weekly
        schedule fired one day late (live misfire: int_fb4fcab91d
        '30 14 * * 2' executed on a Wednesday). This test pins the fix.
        """
        from dashboard.scheduler import cron_matches
        from datetime import datetime
        # Monday 9:00
        dt = datetime(2026, 5, 25, 9, 0)  # Monday
        assert cron_matches("0 9 * * *", dt)
        assert not cron_matches("0 10 * * *", dt)
        assert cron_matches("0 9 * * 1", dt)       # 1=Monday (standard cron)
        assert not cron_matches("0 9 * * 0", dt)   # 0=Sunday
        assert not cron_matches("0 9 * * 2", dt)   # 2=Tuesday
        # Tuesday 14:30 — the live misfire shape
        tue = datetime(2026, 6, 9, 14, 30)  # a Tuesday
        assert cron_matches("30 14 * * 2", tue)
        wed = datetime(2026, 6, 10, 14, 30)  # a Wednesday
        assert not cron_matches("30 14 * * 2", wed)
        # Sunday tolerance: both 0 and 7 mean Sunday
        sun = datetime(2026, 6, 14, 9, 0)  # a Sunday
        assert cron_matches("0 9 * * 0", sun)
        assert cron_matches("0 9 * * 7", sun)

    def test_cron_next(self):
        """REQ-32 catch-up primitive: next occurrence strictly after `after`."""
        from dashboard.scheduler import cron_next
        from datetime import datetime
        after = datetime(2026, 6, 9, 20, 30)  # Tuesday evening
        nxt = cron_next("0 21 * * *", after)
        assert nxt == datetime(2026, 6, 9, 21, 0)
        # Weekly: next Tuesday 14:30 after Tuesday 15:00 is +7 days
        nxt = cron_next("30 14 * * 2", datetime(2026, 6, 9, 15, 0))
        assert nxt == datetime(2026, 6, 16, 14, 30)
        # Malformed expressions return None, never raise
        assert cron_next("not a cron", after) is None
        assert cron_next("0 21 * *", after) is None

    def test_cron_wildcards(self):
        from dashboard.scheduler import cron_matches
        from datetime import datetime
        dt = datetime(2026, 5, 21, 14, 30)
        assert cron_matches("* * * * *", dt)
        assert cron_matches("*/5 * * * *", dt)  # 30 is divisible by 5
        assert not cron_matches("*/7 * * * *", dt)  # 30 not divisible by 7

    def test_check_conditions_time_window(self):
        from dashboard.scheduler import check_conditions
        # Mock current time would be needed for thorough testing
        conditions = [{"type": "time_window", "start": "00:00", "end": "23:59"}]
        assert check_conditions(conditions, {})

    def test_get_due_tasks_interval(self):
        from dashboard.scheduler import get_due_tasks
        # Register a task with interval trigger, never run before
        db_module.task_register(
            task_id="test_interval",
            name="Test Interval",
            trigger_type="interval",
            trigger_config={"seconds": 60},
            action_type="notify",
            action_config={"message": "hello"},
        )
        due = get_due_tasks()
        assert len(due) == 1
        assert due[0]["name"] == "Test Interval"

    def test_mark_executed(self):
        from dashboard.scheduler import mark_executed
        db_module.task_register(
            task_id="test_exec",
            name="Test Exec",
            trigger_type="interval",
            trigger_config={"seconds": 60},
        )
        mark_executed("test_exec", result="ok")
        tasks = db_module.task_list()
        assert tasks[0]["run_count"] == 1
        assert tasks[0]["last_run_at"] is not None


class TestBookmarkPipeline:
    def setup_method(self):
        self.db_path = setup_test_db()

    def teardown_method(self):
        teardown_test_db(self.db_path)

    def test_capture(self):
        from dashboard.bookmark_pipeline import capture
        bm_id = capture("Test Article", "https://example.com", source="test")
        assert bm_id > 0
        items = db_module.bookmark_list()
        assert len(items) == 1

    def test_resurface_empty(self):
        from dashboard.bookmark_pipeline import get_resurface_candidates
        candidates = get_resurface_candidates(5)
        assert candidates == []

    def test_resurface_with_items(self):
        from dashboard.bookmark_pipeline import capture, get_resurface_candidates
        for i in range(10):
            capture(f"Article {i}", f"https://example.com/{i}")
        candidates = get_resurface_candidates(5)
        assert len(candidates) <= 5
        assert len(candidates) > 0

    def test_migrate_from_jsonl(self):
        from dashboard.bookmark_pipeline import migrate_from_jsonl
        # Create a temp jsonl
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False)
        tmp.write(json.dumps({"title": "Old Item", "url": "https://old.com"}) + "\n")
        tmp.write(json.dumps({"title": "Old Item 2", "url": "https://old2.com"}) + "\n")
        tmp.close()
        count = migrate_from_jsonl(tmp.name)
        assert count == 2
        items = db_module.bookmark_list()
        assert len(items) == 2
        os.unlink(tmp.name)


class TestHeartbeatBridge:
    def setup_method(self):
        self.db_path = setup_test_db()

    def teardown_method(self):
        teardown_test_db(self.db_path)

    def test_register_from_action(self):
        from dashboard.heartbeat_bridge import register_from_action
        msg = register_from_action("name=morning_alarm|type=date|config=2026-05-22T06:00|action=notify|message=起床了！")
        assert "morning_alarm" in msg
        tasks = db_module.task_list()
        assert len(tasks) == 1
        assert tasks[0]["name"] == "morning_alarm"

    def test_check_dynamic_tasks_with_due_task(self):
        from dashboard.heartbeat_bridge import check_dynamic_tasks
        # Register a task that's immediately due (interval, never run)
        db_module.task_register(
            task_id="test_due",
            name="Test Due",
            trigger_type="interval",
            trigger_config={"seconds": 1},
            action_type="notify",
            action_config={"message": "hello from dynamic"},
        )
        result = check_dynamic_tasks()
        assert result
        data = json.loads(result)
        assert len(data["tasks"]) == 1
        assert data["tasks"][0]["action_config"]["message"] == "hello from dynamic"
