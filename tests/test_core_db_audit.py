"""Tests for the 2026-07-21 shared-DB audit fixes that stayed live in core.db.

Carried over from the retired dashboard scheduler suite (2026-08-21). The
SQLite `scheduled_tasks` execution loop and bookmark spaced-repetition
pipeline were deleted with the :3457 dashboard; what remains covered here:

- bookmark_search FTS operator input falls back to LIKE
- engagement_stats replied≤sent cap + mtime/size cache
- db._db_path resolves JARVIS_DIR at call time (monkeypatched DB_PATH wins)
"""

import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import core.db as db_module


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


class SharedDBTest:
    def setup_method(self):
        self.db_path = setup_test_db()

    def teardown_method(self):
        teardown_test_db(self.db_path)


class TestSearchFallback(SharedDBTest):
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


class TestEngagementStatsHonesty(SharedDBTest):
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
