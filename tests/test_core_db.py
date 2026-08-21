"""Tests for the shared SQLite layer (core.db) and cron helpers (core.cron).

Carried over from the retired dashboard suite (2026-08-21): the base schema,
bookmark/kv/log/task stores, and the cron primitives stay live under core/
after the :3457 NiceGUI dashboard was deleted.
"""

import json
import os
import sys
import tempfile
from pathlib import Path

# Ensure project root is importable
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

# Override DB path for tests
import core.db as db_module


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
        # writes from an uncalled (now retired) dashboard endpoint and froze
        # on 2026-05-21.
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


class TestCron:
    def test_cron_matches(self):
        """STANDARD cron dow semantics: 0/7=Sunday, 1=Monday ... 6=Saturday.

        The old comparison used dt.weekday() (0=Monday) — every weekly
        schedule fired one day late (live misfire: int_fb4fcab91d
        '30 14 * * 2' executed on a Wednesday). This test pins the fix.
        """
        from datetime import datetime

        from core.cron import cron_matches
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
        from datetime import datetime

        from core.cron import cron_next
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
        from datetime import datetime

        from core.cron import cron_matches
        dt = datetime(2026, 5, 21, 14, 30)
        assert cron_matches("* * * * *", dt)
        assert cron_matches("*/5 * * * *", dt)  # 30 is divisible by 5
        assert not cron_matches("*/7 * * * *", dt)  # 30 not divisible by 7

    def test_check_conditions_time_window(self):
        from core.cron import check_conditions
        conditions = [{"type": "time_window", "start": "00:00", "end": "23:59"}]
        assert check_conditions(conditions, {})
