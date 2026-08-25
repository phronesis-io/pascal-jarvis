from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from core.runtime_hygiene import (
    clean_private_temp,
    enforce_private_modes,
    maintain_sqlite,
    memory_git_gc,
)


def test_private_mode_migration_is_allowlisted_and_skips_symlinks(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    private = data / "state.db"
    private.write_text("x", encoding="utf-8")
    private.chmod(0o644)
    public = tmp_path / "README.md"
    public.write_text("public", encoding="utf-8")
    public.chmod(0o644)
    link = data / "link"
    link.symlink_to(public)

    result = enforce_private_modes(tmp_path)

    assert result["changed"] >= 1
    assert private.stat().st_mode & 0o777 == 0o600
    assert public.stat().st_mode & 0o777 == 0o644
    assert link.is_symlink()


def test_sqlite_retention_keeps_envelopes_and_open_audit_evidence(tmp_path):
    path = tmp_path / "runtime.db"
    old = 1.0
    with sqlite3.connect(path) as db:
        db.executescript("""
        CREATE TABLE schedule_events(id INTEGER PRIMARY KEY, created_epoch REAL);
        CREATE TABLE delivery_envelopes(id TEXT PRIMARY KEY, state TEXT, updated_epoch REAL);
        CREATE TABLE delivery_attempts(id INTEGER PRIMARY KEY, delivery_id TEXT);
        CREATE TABLE delivery_events(id INTEGER PRIMARY KEY, delivery_id TEXT);
        CREATE TABLE audit_runs(id INTEGER PRIMARY KEY, started_at TEXT);
        CREATE TABLE audit_issues(id INTEGER PRIMARY KEY, run_id INTEGER, status TEXT);
        CREATE TABLE conversation_events(id INTEGER PRIMARY KEY, run_id INTEGER);
        CREATE TABLE session_messages(id INTEGER PRIMARY KEY, run_id INTEGER);
        """)
        db.execute("INSERT INTO schedule_events VALUES (1,?)", (old,))
        db.execute("INSERT INTO delivery_envelopes VALUES ('done','delivered',?)", (old,))
        db.execute("INSERT INTO delivery_envelopes VALUES ('live','queued',?)", (old,))
        db.execute("INSERT INTO delivery_attempts VALUES (1,'done')")
        db.execute("INSERT INTO delivery_attempts VALUES (2,'live')")
        db.execute("INSERT INTO delivery_events VALUES (1,'done')")
        db.execute("INSERT INTO audit_runs VALUES (1,'2000-01-01T00:00:00+00:00')")
        db.execute("INSERT INTO audit_runs VALUES (2,'2000-01-01T00:00:00+00:00')")
        db.execute("INSERT INTO audit_issues VALUES (1,1,'resolved')")
        db.execute("INSERT INTO audit_issues VALUES (2,2,'open')")
        db.execute("INSERT INTO conversation_events VALUES (1,1)")
        db.execute("INSERT INTO conversation_events VALUES (2,2)")
        db.execute("INSERT INTO session_messages VALUES (1,1)")

    result = maintain_sqlite(path, now=2_000_000_000)

    assert result["status"] == "ok"
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT count(*) FROM schedule_events").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM delivery_envelopes").fetchone()[0] == 2
        assert db.execute("SELECT delivery_id FROM delivery_attempts").fetchall() == [("live",)]
        assert db.execute("SELECT id FROM audit_runs ORDER BY id").fetchall() == [(2,)]
        assert db.execute("SELECT status FROM audit_issues").fetchall() == [("open",)]


def test_temp_cleanup_removes_only_old_owned_regular_allowlisted_files(tmp_path):
    old = tmp_path / "jarvis-audit-lark-old.json"
    old.write_text("private", encoding="utf-8")
    recent = tmp_path / "jarvis-admin-new.html"
    recent.write_text("private", encoding="utf-8")
    unrelated = tmp_path / "notes.txt"
    unrelated.write_text("keep", encoding="utf-8")
    os.utime(old, (1, 1))

    result = clean_private_temp(tmp_path, now=10 * 86400, min_age_days=7)

    assert result["removed"] == [old.name]
    assert not old.exists()
    assert recent.exists() and unrelated.exists()


def test_memory_git_gc_is_bounded_and_uses_auto(tmp_path):
    (tmp_path / ".git").mkdir()
    calls = []

    class Result:
        returncode = 0

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return Result()

    assert memory_git_gc(tmp_path, runner=runner)["status"] == "ok"
    assert calls[0][0][-2:] == ["gc", "--auto"]
    assert calls[0][1]["timeout"] == 60


def test_runtime_hygiene_cli_returns_structured_result(tmp_path):
    runtime = tmp_path / "runtime"
    temporary = tmp_path / "tmp"
    runtime.mkdir()
    temporary.mkdir()
    result = subprocess.run(
        [sys.executable, "-m", "core.runtime_hygiene",
         "--root", str(runtime), "--temp-root", str(temporary)],
        capture_output=True,
        text=True,
        check=True,
    )

    payload = json.loads(result.stdout)
    assert payload["status"] == "ok"
    assert payload["databases"] == []
