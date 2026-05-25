"""Tests for core.intentions — intent CRUD and lifecycle."""

import json
import sqlite3
import time
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture
def intent_db(tmp_path, monkeypatch):
    """Create an in-memory-like SQLite DB for intentions testing."""
    db_path = tmp_path / "data" / "jarvis.db"
    db_path.parent.mkdir(parents=True)

    # Patch _get_db to use our test DB
    import core.intentions as intentions_mod

    def _test_get_db():
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    monkeypatch.setattr(intentions_mod, "_get_db", _test_get_db)
    intentions_mod._ensure_table()
    return db_path


def test_create_and_get(intent_db):
    from core.intentions import create_intent, get_intent

    iid = create_intent(
        name="test intent",
        trigger_type="date",
        trigger_config={"datetime": "2026-12-31T09:00:00"},
        prompt="remind me",
    )
    assert iid
    intent = get_intent(iid)
    assert intent is not None
    assert intent["name"] == "test intent"
    assert intent["status"] == "pending"


def test_list_intents_filter(intent_db):
    from core.intentions import create_intent, list_intents

    create_intent(name="a", trigger_type="date", trigger_config={})
    create_intent(name="b", trigger_type="cron", trigger_config={})

    all_intents = list_intents()
    assert len(all_intents) == 2

    pending = list_intents(status="pending")
    assert len(pending) == 2


def test_cancel_intent(intent_db):
    from core.intentions import cancel_intent, create_intent, get_intent

    iid = create_intent(name="cancel me", trigger_type="date", trigger_config={})
    assert cancel_intent(iid, "changed my mind")
    intent = get_intent(iid)
    assert intent["status"] == "cancelled"


def test_cancel_already_executed(intent_db):
    from core.intentions import cancel_intent, create_intent, mark_executed, mark_triggered

    iid = create_intent(name="done", trigger_type="date", trigger_config={})
    mark_triggered(iid)
    mark_executed(iid, result="ok")
    assert not cancel_intent(iid)  # can't cancel executed intent


def test_mark_triggered_and_executed(intent_db):
    from core.intentions import create_intent, get_intent, mark_executed, mark_triggered

    iid = create_intent(name="lifecycle", trigger_type="date", trigger_config={})
    mark_triggered(iid)
    assert get_intent(iid)["status"] == "triggered"
    mark_executed(iid, result="done")
    assert get_intent(iid)["status"] == "executed"


def test_reset_stale_triggered(intent_db):
    from core.intentions import create_intent, mark_triggered, reset_stale_triggered, get_intent

    iid = create_intent(name="stuck", trigger_type="date", trigger_config={})
    mark_triggered(iid)

    # Won't reset — not stale yet
    count = reset_stale_triggered(stale_minutes=10)
    # The intent was just triggered, so it shouldn't be stale
    assert count == 0

    # Manually set triggered_at to old time
    import core.intentions as mod
    conn = mod._get_db()
    conn.execute("UPDATE intentions SET triggered_at = datetime('now', '-20 minutes') WHERE id = ?", (iid,))
    conn.commit()
    conn.close()

    count = reset_stale_triggered(stale_minutes=10)
    assert count == 1
    assert get_intent(iid)["status"] == "pending"


def test_intent_stats(intent_db):
    from core.intentions import create_intent, intent_stats, mark_triggered, mark_executed

    create_intent(name="pending1", trigger_type="date", trigger_config={})
    iid2 = create_intent(name="executed1", trigger_type="date", trigger_config={})
    mark_triggered(iid2)
    mark_executed(iid2)

    stats = intent_stats()
    assert stats["pending"] == 1
    assert stats["executed"] == 1


def test_format_due_intents_for_claude(intent_db):
    from core.intentions import create_intent, format_due_intents_for_claude

    create_intent(
        name="morning check",
        trigger_type="date",
        trigger_config={"datetime": "2026-01-01T09:00:00"},
        prompt="check inbox",
        purpose="stay on top of email",
    )

    # Get all pending intents and format
    from core.intentions import list_intents
    intents = list_intents(status="pending")
    result = format_due_intents_for_claude(intents)
    assert "morning check" in result
    assert "check inbox" in result
