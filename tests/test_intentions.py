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
    """New contract: cancel_intent retires from ANY state, so executed residue
    (e.g. the 2026-06-08 junk-intent storm) can be cleared without a raw DELETE.
    Previously executed intents were uncancellable."""
    from core.intentions import cancel_intent, create_intent, get_intent, mark_executed, mark_triggered

    iid = create_intent(name="done", trigger_type="date", trigger_config={})
    mark_triggered(iid)
    mark_executed(iid, result="ok")
    assert cancel_intent(iid)  # executed intents are now cancellable
    assert get_intent(iid)["status"] == "cancelled"
    # Idempotent on already-cancelled; False only when the intent doesn't exist.
    assert cancel_intent(iid)
    assert not cancel_intent("int_does_not_exist")


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


def test_reset_stale_overdue_oneshot_expires_not_resurrects(intent_db):
    """Regression (2026-06-08 resurrection loop): a one-shot `date` intent whose
    trigger time is in the PAST must NOT be reset to pending when stuck in
    triggered — get_due_intents would re-fire it instantly and it would re-stick,
    reappearing every cycle forever. It must be marked 'expired' instead.
    """
    from core.intentions import (create_intent, mark_triggered,
                                   reset_stale_triggered, get_intent)

    overdue = create_intent(name="junk", trigger_type="date",
                            trigger_config={"datetime": "2026-01-01T09:00:00"})
    future = create_intent(name="real", trigger_type="date",
                           trigger_config={"datetime": "2999-01-01T09:00:00"})
    mark_triggered(overdue)
    mark_triggered(future)

    import core.intentions as mod
    conn = mod._get_db()
    conn.execute("UPDATE intentions SET triggered_at = datetime('now', '-20 minutes')")
    conn.commit()
    conn.close()

    # Overdue one-shot → expired (NOT counted as reset). Future one-shot → pending.
    count = reset_stale_triggered(stale_minutes=10)
    assert count == 1                                    # only the future one recovered
    assert get_intent(overdue)["status"] == "expired"   # loop terminated
    assert get_intent(future)["status"] == "pending"    # legit crash recovery


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


def test_get_due_interval_does_not_crash(intent_db):
    """Regression: an interval intent stores a naive created_at, while
    now_local() is tz-aware. Subtracting them used to raise TypeError and
    crash the ENTIRE due-check (no intents fired that cycle). _coerce fixes it.
    """
    from core.intentions import create_intent, get_due_intents

    iid = create_intent(name="interval", trigger_type="interval",
                        trigger_config={"seconds": 0})
    due_ids = {d["id"] for d in get_due_intents()}
    assert iid in due_ids


def test_get_due_date_naive_target_fires(intent_db):
    """Regression: a date intent whose trigger datetime has no tz offset
    (e.g. '2020-01-01T09:00:00', as ACTION:intent_create typically emits) must
    still be detected as due — previously the aware/naive compare raised
    TypeError, was swallowed by try/except, and the intent never fired.
    """
    from core.intentions import create_intent, get_due_intents

    past = create_intent(name="past", trigger_type="date",
                        trigger_config={"datetime": "2020-01-01T09:00:00"})
    future = create_intent(name="future", trigger_type="date",
                          trigger_config={"datetime": "2999-01-01T09:00:00"})
    due_ids = {d["id"] for d in get_due_intents()}
    assert past in due_ids
    assert future not in due_ids


# ===========================================================================
# Closure model (Intent 系统重做) — Input/Decision/Output + closure lifecycle.
# ===========================================================================

def test_migrate_adds_columns_idempotent(intent_db):
    """_ensure_table() runs _migrate(); calling it again is a no-op, columns
    present either way. (CREATE TABLE IF NOT EXISTS would NOT add them.)"""
    import core.intentions as mod
    mod._ensure_table()  # second call — must not raise
    conn = mod._get_db()
    cols = {r[1] for r in conn.execute("PRAGMA table_info(intentions)").fetchall()}
    conn.close()
    for c in ("category", "input_ctx", "decision", "closure_question",
              "closure_status", "closure_result", "closure_touches",
              "closure_followup_id", "parent_intent_id"):
        assert c in cols, f"missing closure column {c}"


def test_stats_keeps_five_keys(intent_db):
    """closure_status is orthogonal — intent_stats stays the canonical 5 keys."""
    from core.intentions import intent_stats
    assert set(intent_stats()) == {"pending", "triggered", "executed", "expired", "cancelled"}


def test_create_validates_action_type_and_category(intent_db):
    """The dirty-data class (action_type='cron') is rejected at the source."""
    from core.intentions import create_intent
    with pytest.raises(ValueError):
        create_intent(name="x", trigger_type="date", trigger_config={}, action_type="cron")
    with pytest.raises(ValueError):
        create_intent(name="x", trigger_type="date", trigger_config={}, category="bogus")


def test_snap_to_golden():
    from core.intentions import snap_to_golden
    from datetime import datetime
    assert snap_to_golden(datetime(2026, 6, 10, 14, 0)).hour == 14          # in window → unchanged
    assert snap_to_golden(datetime(2026, 6, 10, 9, 30)).hour == 11          # early → same-day 11
    d = snap_to_golden(datetime(2026, 6, 10, 22, 0))                        # dead zone → next day 11
    assert d.hour == 11 and d.day == 11
    d2 = snap_to_golden(datetime(2026, 6, 10, 5, 0))                        # 05:00 → same-day 11
    assert d2.hour == 11 and d2.day == 10


def _fire(iid):
    """Run an intent through trigger → executed."""
    from core.intentions import mark_triggered, mark_executed
    mark_triggered(iid)
    mark_executed(iid, result="fired")


def test_external_closure_spawns_notify_followup(intent_db):
    """One-shot external moment → on execute spawns an awaiting follow-up that
    cards (may_notify), linked both ways, deterministic id."""
    from core.intentions import create_intent, get_intent
    pid = create_intent(name="约学妹", trigger_type="date",
                        trigger_config={"datetime": "2026-06-11T10:00:00"},
                        category="external", closure_question="约上了吗？")
    _fire(pid)
    parent = get_intent(pid)
    assert parent["closure_status"] == "awaiting"
    fu_id = parent["closure_followup_id"]
    assert fu_id == f"{pid}__fu"
    fu = get_intent(fu_id)
    assert fu is not None
    assert fu["action_type"] == "notify"          # external may_notify
    assert fu["parent_intent_id"] == pid
    assert fu["closure_question"] == "约上了吗？"


def test_healing_closure_followup_is_silent(intent_db):
    """Healing follow-up must be silent (prompt) — never a card."""
    from core.intentions import create_intent, get_intent
    pid = create_intent(name="读 x402", trigger_type="date",
                        trigger_config={"datetime": "2026-06-10T20:00:00"},
                        category="healing", closure_question="选中率多少？")
    _fire(pid)
    fu = get_intent(get_intent(pid)["closure_followup_id"])
    assert fu["action_type"] == "prompt"          # healing never cards


def test_cron_moment_does_not_spawn_followup(intent_db):
    """Recurring intents must NOT proliferate follow-ups per fire (nag-mountain)."""
    from core.intentions import create_intent, get_intent, mark_triggered, mark_executed
    pid = create_intent(name="每日康复", trigger_type="cron",
                        trigger_config={"expression": "0 21 * * *"},
                        category="healing", closure_question="找到感觉了吗？")
    for _ in range(3):
        mark_triggered(pid)
        mark_executed(pid)
    p = get_intent(pid)
    assert p["closure_status"] == "none"
    assert p["closure_followup_id"] is None


def test_followup_not_respawned_on_reexec(intent_db):
    """Guard: a parent already 'awaiting' does not spawn a second follow-up."""
    from core.intentions import create_intent, get_intent, list_intents
    pid = create_intent(name="m", trigger_type="date",
                        trigger_config={"datetime": "2026-06-11T10:00:00"},
                        category="external", closure_question="?")
    _fire(pid)
    _fire(pid)  # second execute — guard (closure_status='awaiting') must skip
    followups = [i for i in list_intents(status="pending", limit=500)
                 if i.get("parent_intent_id") == pid]
    assert len(followups) == 1


def test_record_closure_writes_and_cancels_followup(intent_db):
    from core.intentions import create_intent, get_intent, record_closure
    pid = create_intent(name="约学妹", trigger_type="date",
                        trigger_config={"datetime": "2026-06-11T10:00:00"},
                        category="external", closure_question="约上了吗？")
    _fire(pid)
    fu_id = get_intent(pid)["closure_followup_id"]
    assert record_closure(pid, outcome="done", result="约了周四下午") is True
    parent = get_intent(pid)
    assert parent["closure_status"] == "done"
    assert parent["closure_result"] == "约了周四下午"
    assert get_intent(fu_id)["status"] == "cancelled"   # no double-ask


def test_record_closure_idempotent_and_noop(intent_db):
    from core.intentions import create_intent, record_closure
    # no follow-up (closure_status='none') — still records, no crash on NULL fu
    pid = create_intent(name="plain", trigger_type="date", trigger_config={})
    assert record_closure(pid, outcome="done", result="x") is True
    # second call → already terminal → no-op False
    assert record_closure(pid, outcome="recorded", result="y") is False
    # unknown id → no-op False
    assert record_closure("int_nope") is False


def test_record_closure_whitelists_outcome(intent_db):
    from core.intentions import create_intent, get_intent, record_closure
    pid = create_intent(name="p", trigger_type="date", trigger_config={})
    record_closure(pid, outcome="DROP TABLE", result="z")  # polluted → coerced to 'done'
    assert get_intent(pid)["closure_status"] == "done"


def test_get_closure_due_excludes_healing_includes_external(intent_db):
    """Healing never re-surfaced; external re-surfaced once its follow-up drained."""
    from core.intentions import create_intent, get_intent, get_closure_due, mark_triggered, mark_executed
    ext = create_intent(name="ext", trigger_type="date",
                        trigger_config={"datetime": "2026-06-11T10:00:00"},
                        category="external", closure_question="?")
    heal = create_intent(name="heal", trigger_type="date",
                         trigger_config={"datetime": "2026-06-11T10:00:00"},
                         category="healing", closure_question="?")
    _fire(ext); _fire(heal)
    # follow-ups still pending → not yet due
    assert get_closure_due() == []
    # drain the external follow-up (it fired, no answer) → now re-askable
    fu = get_intent(ext)["closure_followup_id"]
    mark_triggered(fu); mark_executed(fu)
    due_ids = {d["id"] for d in get_closure_due()}
    assert ext in due_ids
    assert heal not in due_ids                      # healing structurally excluded


def test_snapshot_awaiting_excludes_healing(intent_db, tmp_path):
    """The 待闭环 wall shows external; healing/autonomous never appear (no visible
    'you didn't do it' ledger). Old '不追做没做' wording is gone."""
    from core.intentions import create_intent, snapshot_active_intents
    ext = create_intent(name="约学妹", trigger_type="date",
                       trigger_config={"datetime": "2026-06-11T10:00:00"},
                       category="external", closure_question="约上了吗？")
    heal = create_intent(name="读 x402", trigger_type="date",
                        trigger_config={"datetime": "2026-06-10T20:00:00"},
                        category="healing", closure_question="选中率多少？")
    _fire(ext); _fire(heal)
    snapshot_active_intents(tmp_path)
    text = (tmp_path / "hot" / "active_intents.md").read_text()
    assert "待闭环" in text
    assert "约上了吗？" in text          # external surfaced
    assert "选中率多少？" not in text     # healing never on the wall
    assert "不追「做没做」" not in text   # old wording replaced


def test_cancel_awaiting_parent_sets_na_and_cancels_followup(intent_db):
    from core.intentions import create_intent, get_intent, cancel_intent
    pid = create_intent(name="p", trigger_type="date",
                        trigger_config={"datetime": "2026-06-11T10:00:00"},
                        category="external", closure_question="?")
    _fire(pid)
    fu_id = get_intent(pid)["closure_followup_id"]
    cancel_intent(pid, "no longer relevant")
    assert get_intent(pid)["closure_status"] == "na"
    assert get_intent(fu_id)["status"] == "cancelled"
