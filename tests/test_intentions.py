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
    # Keep the skip-log dedup sidecar out of the real repo data/ dir (and
    # give every test a fresh seen-set).
    monkeypatch.setattr(intentions_mod, "SKIP_LOG_SEEN_FILE",
                        tmp_path / ".cal_skip_log_seen.json")
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

    create_intent(name="a", trigger_type="date",
                  trigger_config={"datetime": "2999-01-01T09:00:00"})
    create_intent(name="b", trigger_type="cron",
                  trigger_config={"expression": "0 9 * * *"})

    all_intents = list_intents()
    assert len(all_intents) == 2

    pending = list_intents(status="pending")
    assert len(pending) == 2


def test_create_rejects_unfireable_rows(intent_db):
    """REQ-53: a date intent with empty/garbage datetime or a cron intent with
    a malformed expression would be created 'pending' but could never fire —
    bloating the snapshot wall forever. Rejected at the boundary."""
    from core.intentions import create_intent
    import pytest
    with pytest.raises(ValueError):
        create_intent(name="x", trigger_type="date", trigger_config={})
    with pytest.raises(ValueError):
        create_intent(name="x", trigger_type="date",
                      trigger_config={"datetime": "not-a-date"})
    with pytest.raises(ValueError):
        create_intent(name="x", trigger_type="cron", trigger_config={})
    with pytest.raises(ValueError):
        create_intent(name="x", trigger_type="cron",
                      trigger_config={"expression": "9am daily"})


def test_cancel_intent(intent_db):
    from core.intentions import cancel_intent, create_intent, get_intent

    iid = create_intent(name="cancel me", trigger_type="date",
                        trigger_config={"datetime": "2999-01-01T09:00:00"})
    assert cancel_intent(iid, "changed my mind")
    intent = get_intent(iid)
    assert intent["status"] == "cancelled"


def test_cancel_already_executed(intent_db):
    """New contract: cancel_intent retires from ANY state, so executed residue
    (e.g. the 2026-06-08 junk-intent storm) can be cleared without a raw DELETE.
    Previously executed intents were uncancellable."""
    from core.intentions import cancel_intent, create_intent, get_intent, mark_executed, mark_triggered

    iid = create_intent(name="done", trigger_type="date",
                        trigger_config={"datetime": "2999-01-01T09:00:00"})
    mark_triggered(iid)
    mark_executed(iid, result="ok")
    assert cancel_intent(iid)  # executed intents are now cancellable
    assert get_intent(iid)["status"] == "cancelled"
    # Idempotent on already-cancelled; False only when the intent doesn't exist.
    assert cancel_intent(iid)
    assert not cancel_intent("int_does_not_exist")


def test_mark_triggered_and_executed(intent_db):
    from core.intentions import create_intent, get_intent, mark_executed, mark_triggered

    iid = create_intent(name="lifecycle", trigger_type="date",
                        trigger_config={"datetime": "2999-01-01T09:00:00"})
    mark_triggered(iid)
    row = get_intent(iid)
    assert row["status"] == "triggered"
    assert row["attempt"] == 1          # attempt counted (REQ-31)
    mark_executed(iid, result="done")
    row = get_intent(iid)
    assert row["status"] == "executed"
    assert row["attempt"] == 0          # reset on success


def test_lifecycle_sweep_retries_future_oneshot(intent_db):
    from core.intentions import create_intent, mark_triggered, lifecycle_sweep, get_intent

    iid = create_intent(name="stuck", trigger_type="date",
                        trigger_config={"datetime": "2999-01-01T09:00:00"})
    mark_triggered(iid)

    # Won't reset — not stale yet
    count = lifecycle_sweep(stale_minutes=10)
    assert count == 0

    # Manually set triggered_at to old time
    import core.intentions as mod
    conn = mod._get_db()
    conn.execute("UPDATE intentions SET triggered_at = datetime('now', '-20 minutes') WHERE id = ?", (iid,))
    conn.commit()
    conn.close()

    count = lifecycle_sweep(stale_minutes=10)
    assert count == 1
    assert get_intent(iid)["status"] == "pending"


def test_lifecycle_sweep_bounded_retry_then_breach(intent_db, tmp_path, monkeypatch):
    """REQ-31 core: a one-shot that just fired and got stuck retries (≤3 within
    2h grace) instead of dying; exhausted retries expire LOUDLY via the breach
    queue. The old reset_stale_triggered expired EVERY just-fired one-shot on
    its first failure — 18/19 expired rows in the 6/13 audit."""
    import json
    import core.intentions as mod
    from datetime import datetime, timedelta
    from core.timeutil import now_local

    monkeypatch.setattr(mod, "BREACH_QUEUE", tmp_path / "breach.jsonl")

    # Fired 20 minutes ago (well within the 2h grace), stuck in triggered.
    recent = (now_local() - timedelta(minutes=20)).strftime("%Y-%m-%dT%H:%M:%S")
    iid = mod.create_intent(name="提醒喝水", trigger_type="date",
                            trigger_config={"datetime": recent},
                            prompt="提醒 Pascal 喝水")
    mod.mark_triggered(iid)
    conn = mod._get_db()
    conn.execute("UPDATE intentions SET triggered_at = datetime('now', '-15 minutes') WHERE id = ?", (iid,))
    conn.commit(); conn.close()

    # Attempt 1 stuck → retry, NOT expired (the old behavior killed it here).
    assert mod.lifecycle_sweep(stale_minutes=10) == 1
    assert mod.get_intent(iid)["status"] == "pending"

    # Exhaust the budget: simulate attempts ≥ MAX and stuck again.
    conn = mod._get_db()
    conn.execute("UPDATE intentions SET status='triggered', attempt = ?, "
                 "triggered_at = datetime('now', '-15 minutes') WHERE id = ?",
                 (mod.MAX_ATTEMPTS, iid))
    conn.commit(); conn.close()

    mod.lifecycle_sweep(stale_minutes=10)
    row = mod.get_intent(iid)
    assert row["status"] == "expired"
    # Breach notification queued — the drop is never silent.
    lines = (tmp_path / "breach.jsonl").read_text().splitlines()
    entry = json.loads(lines[-1])
    assert entry["id"] == iid
    assert entry["prompt"] == "提醒 Pascal 喝水"


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

    create_intent(name="pending1", trigger_type="date",
                  trigger_config={"datetime": "2999-01-01T09:00:00"})
    iid2 = create_intent(name="executed1", trigger_type="date",
                         trigger_config={"datetime": "2999-01-01T09:00:00"})
    mark_triggered(iid2)
    mark_executed(iid2)

    stats = intent_stats()
    assert stats["pending"] == 1
    assert stats["executed"] == 1


def test_validate_envelope_contract(intent_db):
    """Single-source envelope contract (REQ-53/leak-10): covered/missing split
    drives the manifest reconciliation in intentions_post."""
    from core.intentions import validate_envelope

    data = {"intents": {"int_a": {"response": "ok", "action": "notify"},
                         "int_b": "not-a-dict"}}
    covered, missing, errors = validate_envelope(data, ["int_a", "int_b", "int_c"])
    assert covered == ["int_a"]
    assert set(missing) == {"int_b", "int_c"}
    assert errors  # int_b's malformed slot reported

    # Degenerate shapes never raise
    assert validate_envelope("garbage", ["x"])[1] == ["x"]
    assert validate_envelope({}, ["x"])[1] == ["x"]


def test_envelope_schema_doc_in_heartbeat_md():
    """Prompt and parser must not drift (the drift class that shipped 'reply
    HEARTBEAT_OK' against a state machine requiring an envelope)."""
    from pathlib import Path
    hb = (Path(__file__).parent.parent / "HEARTBEAT.md").read_text()
    assert "NEVER reply HEARTBEAT_OK to this task" in hb
    assert '"closure": {"parent": "<parent_id>", "outcome": "done|recorded|na"' in hb


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
    pid = create_intent(name="plain", trigger_type="date",
                        trigger_config={"datetime": "2999-01-01T09:00:00"})
    assert record_closure(pid, outcome="done", result="x") is True
    # second call → already terminal → no-op False
    assert record_closure(pid, outcome="recorded", result="y") is False
    # unknown id → no-op False
    assert record_closure("int_nope") is False


def test_record_closure_stamps_closed_at(intent_db):
    """closed_at makes time-to-close measurable (REQ-33 learning loop)."""
    from core.intentions import create_intent, get_intent, record_closure
    pid = create_intent(name="p", trigger_type="date",
                        trigger_config={"datetime": "2999-01-01T09:00:00"})
    record_closure(pid, outcome="done", result="x", via="button")
    assert get_intent(pid)["closed_at"]


def test_record_closure_whitelists_outcome(intent_db):
    from core.intentions import create_intent, get_intent, record_closure
    pid = create_intent(name="p", trigger_type="date",
                        trigger_config={"datetime": "2999-01-01T09:00:00"})
    record_closure(pid, outcome="DROP TABLE", result="z")  # polluted → coerced to 'done'
    assert get_intent(pid)["closure_status"] == "done"


def test_closure_spawn_on_expired_external_moment(intent_db, tmp_path, monkeypatch):
    """REQ-33: an external moment that EXPIRED (reminder failed) still spawns
    its closure follow-up — the real-world event happened. Healing does not
    (never-nag guarantee holds even on failure)."""
    import core.intentions as mod
    from datetime import timedelta
    from core.timeutil import now_local

    monkeypatch.setattr(mod, "BREACH_QUEUE", tmp_path / "breach.jsonl")

    recent = (now_local() - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%S")
    ext = mod.create_intent(name="饭局", trigger_type="date",
                            trigger_config={"datetime": recent},
                            category="external", closure_question="有值得跟进的吗？")
    heal = mod.create_intent(name="读书", trigger_type="date",
                             trigger_config={"datetime": recent},
                             category="healing", closure_question="读了吗？")
    for iid in (ext, heal):
        mod.mark_triggered(iid)
    conn = mod._get_db()
    conn.execute("UPDATE intentions SET attempt = ?, "
                 "triggered_at = datetime('now', '-15 minutes')", (mod.MAX_ATTEMPTS,))
    conn.commit(); conn.close()

    mod.lifecycle_sweep(stale_minutes=10)
    assert mod.get_intent(ext)["status"] == "expired"
    assert mod.get_intent(ext)["closure_status"] == "awaiting"    # spawned
    assert mod.get_intent(f"{ext}__fu") is not None
    assert mod.get_intent(heal)["closure_status"] == "none"       # never-nag
    assert mod.get_intent(f"{heal}__fu") is None


def test_awaiting_ttl_terminates_zombies(intent_db):
    """REQ-33: 'awaiting' with a dead follow-up past the category TTL → 'na'
    with closed_at — the int_7a545cc10d zombie class cannot recur."""
    import core.intentions as mod
    pid = mod.create_intent(name="m", trigger_type="date",
                            trigger_config={"datetime": "2026-06-01T10:00:00"},
                            category="healing", closure_question="?")
    mod.mark_triggered(pid)
    mod.mark_executed(pid)
    fu_id = mod.get_intent(pid)["closure_followup_id"]
    conn = mod._get_db()
    # follow-up died; parent executed 10 days ago (healing TTL = 3d)
    conn.execute("UPDATE intentions SET status='expired' WHERE id = ?", (fu_id,))
    conn.execute("UPDATE intentions SET executed_at = datetime('now', '-10 days') WHERE id = ?", (pid,))
    conn.commit(); conn.close()

    mod.lifecycle_sweep(stale_minutes=10)
    row = mod.get_intent(pid)
    assert row["closure_status"] == "na"
    assert row["closed_at"]
    assert "ttl" in row["closure_result"]


def test_cron_next_fire_at_catchup(intent_db):
    """REQ-32: a cron intent fires when now >= next_fire_at (missed minutes
    caught up), and mark_executed advances next_fire_at."""
    import core.intentions as mod
    from datetime import timedelta
    from core.timeutil import now_local

    pid = mod.create_intent(name="daily", trigger_type="cron",
                            trigger_config={"expression": "0 21 * * *"})
    row = mod.get_intent(pid)
    assert row["next_fire_at"]                       # stamped at create

    # Simulate a missed occurrence 30 minutes ago — exact-minute matching
    # would have lost the whole day; catch-up fires it now.
    missed = (now_local() - timedelta(minutes=30)).replace(tzinfo=None)
    conn = mod._get_db()
    conn.execute("UPDATE intentions SET next_fire_at = ? WHERE id = ?",
                 (missed.isoformat(), pid))
    conn.commit(); conn.close()
    due_ids = {d["id"] for d in mod.get_due_intents()}
    assert pid in due_ids

    mod.mark_triggered(pid)
    mod.mark_executed(pid)
    row = mod.get_intent(pid)
    assert row["status"] == "pending"                # recurring resets
    from datetime import datetime
    advanced = datetime.fromisoformat(row["next_fire_at"]).replace(tzinfo=None)
    assert advanced > missed  # advanced past the missed occurrence


def test_cron_stale_occurrence_skipped(intent_db):
    """REQ-32 ceiling: an occurrence >6h stale (host slept) is skipped with a
    recomputed next_fire_at — never fire 21:00 content at 03:00."""
    import core.intentions as mod
    from datetime import datetime, timedelta
    from core.timeutil import now_local

    pid = mod.create_intent(name="evening", trigger_type="cron",
                            trigger_config={"expression": "0 21 * * *"})
    stale = (now_local() - timedelta(hours=8)).replace(tzinfo=None)
    conn = mod._get_db()
    conn.execute("UPDATE intentions SET next_fire_at = ? WHERE id = ?",
                 (stale.isoformat(), pid))
    conn.commit(); conn.close()

    due_ids = {d["id"] for d in mod.get_due_intents()}
    assert pid not in due_ids                        # skipped, not fired
    row = mod.get_intent(pid)
    assert datetime.fromisoformat(row["next_fire_at"]) > now_local().replace(tzinfo=None)
    assert "skipped" in (row["last_error"] or "")


def test_inflight_manifest_reconcile(intent_db, tmp_path, monkeypatch):
    """REQ-30: ids the envelope did not cover get the retry policy applied
    immediately via the manifest — absence of an envelope is a deterministic
    signal, not silent death."""
    import core.intentions as mod
    from datetime import timedelta
    from core.timeutil import now_local

    monkeypatch.setattr(mod, "INFLIGHT_FILE", tmp_path / "inflight.json")
    monkeypatch.setattr(mod, "BREACH_QUEUE", tmp_path / "breach.jsonl")

    recent = (now_local() - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%S")
    a = mod.create_intent(name="a", trigger_type="date",
                          trigger_config={"datetime": recent})
    b = mod.create_intent(name="b", trigger_type="date",
                          trigger_config={"datetime": recent})
    mod.mark_triggered(a)
    mod.mark_triggered(b)
    mod.write_inflight([a, b])

    # Envelope covered only `a` — `b` must go back to pending NOW.
    result = mod.reconcile_inflight([a])
    assert result["retried"] == [b]
    assert mod.get_intent(b)["status"] == "pending"
    assert mod.read_inflight() == []                 # manifest cleared


def test_breach_queue_peek_and_mark_shown(intent_db, tmp_path, monkeypatch):
    """Red-team fix: peek is NON-mutating (counter only advances on a rendered
    card via mark_breaches_shown), so a no-envelope cycle can't silently burn
    the notify budget."""
    import core.intentions as mod
    from core.timeutil import now_local

    monkeypatch.setattr(mod, "BREACH_QUEUE", tmp_path / "breach.jsonl")
    intent = {"id": "int_x", "name": "n", "prompt": "p", "purpose": "",
              "attempt": 3, "trigger_type": "date",
              "trigger_config": '{"datetime": "2026-06-12T18:00:00"}'}
    mod._queue_breach(intent, now_local())

    # peek does NOT bump — repeatable, budget untouched on no-render cycles
    assert [b["id"] for b in mod.peek_breaches()] == ["int_x"]
    assert mod.peek_breaches()[0].get("notify_attempts", 0) == 0
    assert mod.peek_breaches()[0].get("notify_attempts", 0) == 0  # still 0

    # a rendered card marks it shown ONCE → retired (BREACH_MAX_SHOWS=1, the
    # 2026-06-15 anti-nag fix: the dinner-closure apology was shown 3x).
    assert mod.BREACH_MAX_SHOWS == 1
    mod.mark_breaches_shown(["int_x"])
    assert mod.peek_breaches() == []                 # shown once → gone, no nag

    # back-compat alias drain_breaches == peek_breaches (non-mutating)
    assert mod.drain_breaches is mod.peek_breaches


def test_reconcile_breach_survives_post_clear(intent_db, tmp_path, monkeypatch):
    """Red-team fix: a breach freshly queued by reconcile_inflight must NOT be
    wiped by the same post-cycle's mark-shown (it never rode a card)."""
    import core.intentions as mod
    from datetime import timedelta
    from core.timeutil import now_local

    monkeypatch.setattr(mod, "BREACH_QUEUE", tmp_path / "breach.jsonl")
    monkeypatch.setattr(mod, "INFLIGHT_FILE", tmp_path / "inflight.json")

    # An old breach already in the queue rode this cycle's PRE prompt (id A);
    # reconcile then exhausts B and queues it fresh.
    old = (now_local() - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%S")
    a = mod.create_intent(name="a", trigger_type="date", trigger_config={"datetime": old})
    b = mod.create_intent(name="b", trigger_type="date", trigger_config={"datetime": old})
    mod._queue_breach({"id": a, "name": "a", "prompt": "pa", "attempt": 3,
                       "trigger_type": "date", "trigger_config": '{"datetime": "%s"}' % old},
                      now_local())
    mod.mark_triggered(b)
    # exhaust b's attempts so reconcile expires + breaches it
    conn = mod._get_db()
    conn.execute("UPDATE intentions SET attempt = ? WHERE id = ?", (mod.MAX_ATTEMPTS, b))
    conn.commit(); conn.close()
    mod.write_inflight([b], breach_ids=[a])

    result = mod.reconcile_inflight([])            # b uncovered → expired + breached
    assert b in result["breached"]
    # mark only the breach that rode the card (a), NOT reconcile's fresh b
    fresh = set(result["breached"])
    mod.mark_breaches_shown([x for x in [a] if x not in fresh])
    entries = {x["id"]: x for x in mod.peek_breaches()}
    assert b in entries                            # reconcile's fresh breach survived
    assert entries[b]["notify_attempts"] == 0      # b never rode a card → budget intact
    assert a not in entries                        # a shown once → retired (max=1, no nag)


def test_closure_stats_funnel(intent_db):
    from core.intentions import create_intent, closure_stats, mark_triggered, mark_executed, record_closure
    pid = create_intent(name="m", trigger_type="date",
                        trigger_config={"datetime": "2026-06-12T10:00:00"},
                        category="external", closure_question="?")
    mark_triggered(pid)
    mark_executed(pid)
    record_closure(pid, outcome="done", result="x")
    stats = closure_stats()
    ext = stats["external"]
    assert ext["created"] == 1
    assert ext["executed"] == 1
    assert ext["closed_done"] == 1
    assert ext["median_hours_to_close"] is not None


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


def test_generate_closure_reask_intents_after_followup_drained(intent_db):
    """A hard/external closure whose first follow-up asked and got no result
    becomes a bounded re-ask intent instead of sinking forever."""
    import core.intentions as mod

    ext = mod.create_intent(name="上线 Jarvis 修复", trigger_type="date",
                            trigger_config={"datetime": "2026-06-11T10:00:00"},
                            category="external", closure_question="现在真的跑起来了吗？")
    _fire(ext)
    fu = mod.get_intent(ext)["closure_followup_id"]
    mod.mark_triggered(fu)
    mod.mark_executed(fu, result="asked, no answer")

    conn = mod._get_db()
    conn.execute("UPDATE intentions SET executed_at = datetime('now', '-25 hours') WHERE id = ?", (fu,))
    conn.commit(); conn.close()

    created = mod.generate_closure_reask_intents()
    assert len(created) == 1
    reask = mod.get_intent(created[0])
    assert reask["parent_intent_id"] == ext
    assert reask["source"] == "closure"
    assert reask["action_type"] == "notify"
    assert "闭环再问" in reask["prompt"]
    assert mod.get_intent(ext)["closure_followup_id"] == created[0]
    assert mod.generate_closure_reask_intents() == []  # pending reask blocks dupes


def test_generate_closure_reask_respects_gap_and_healing(intent_db):
    """Re-asks are daily-throttled and healing closures remain never-card."""
    import core.intentions as mod

    ext = mod.create_intent(name="ext", trigger_type="date",
                            trigger_config={"datetime": "2026-06-11T10:00:00"},
                            category="external", closure_question="?")
    heal = mod.create_intent(name="heal", trigger_type="date",
                             trigger_config={"datetime": "2026-06-11T10:00:00"},
                             category="healing", closure_question="?")
    _fire(ext); _fire(heal)
    ext_fu = mod.get_intent(ext)["closure_followup_id"]
    heal_fu = mod.get_intent(heal)["closure_followup_id"]
    for fu in (ext_fu, heal_fu):
        mod.mark_triggered(fu)
        mod.mark_executed(fu, result="asked")

    # The external follow-up was just executed, so the 24h re-ask gap blocks it.
    created = mod.generate_closure_reask_intents()
    assert created == []

    # Even after the gap, healing is structurally excluded.
    conn = mod._get_db()
    conn.execute("UPDATE intentions SET executed_at = datetime('now', '-25 hours') WHERE id IN (?, ?)",
                 (ext_fu, heal_fu))
    conn.commit(); conn.close()
    created = mod.generate_closure_reask_intents()
    assert len(created) == 1
    assert mod.get_intent(created[0])["parent_intent_id"] == ext


def test_note_closure_touch_counts_rendered_card_budget(intent_db):
    """Only rendered closure cards should consume proactive touch budget."""
    import core.intentions as mod

    ext = mod.create_intent(name="ext", trigger_type="date",
                            trigger_config={"datetime": "2026-06-11T10:00:00"},
                            category="external", closure_question="?")
    _fire(ext)
    assert mod.note_closure_touch(ext, via="card") is True
    assert mod.note_closure_touch(ext, via="card") is True
    assert mod.get_intent(ext)["closure_touches"] == 2

    conn = mod._get_db()
    conn.execute("UPDATE intentions SET closure_touches = ? WHERE id = ?",
                 (mod.CLOSURE_POLICY["external"]["decay_budget"], ext))
    conn.execute("UPDATE intentions SET status = 'executed' WHERE id = ?",
                 (mod.get_intent(ext)["closure_followup_id"],))
    conn.commit(); conn.close()
    assert mod.get_closure_due() == []


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


def test_cron_next_fire_anchors_to_fired_occurrence(intent_db, monkeypatch):
    """Red-team fix: a slow/late mark_executed must advance next_fire_at from
    the FIRED occurrence, not wall-clock now — else a late post skips every
    intervening occurrence (hourly intent loses the 14:00 beat if post runs
    at 14:03)."""
    import core.intentions as mod
    from datetime import datetime, timedelta
    from core.timeutil import now_local

    pid = mod.create_intent(name="hourly", trigger_type="cron",
                            trigger_config={"expression": "0 * * * *"})
    # Simulate: occurrence was 13:00, but we're marking executed late.
    fired = now_local().replace(minute=0, second=0, microsecond=0) - timedelta(hours=2)
    conn = mod._get_db()
    conn.execute("UPDATE intentions SET next_fire_at = ? WHERE id = ?",
                 (fired.replace(tzinfo=None).isoformat(), pid))
    conn.commit(); conn.close()

    mod.mark_executed(pid)
    row = mod.get_intent(pid)
    nxt = datetime.fromisoformat(row["next_fire_at"]).replace(tzinfo=None)
    # Next fire must be the occurrence right AFTER the fired one (fired+1h),
    # not jumped to now+1h — catch-up preserved.
    assert nxt == fired.replace(tzinfo=None) + timedelta(hours=1)


# ── REQ-60: closure-of-closure guard + stale closure expiry ──────────────

def test_closure_followup_does_not_spawn_second_level(intent_db):
    """A followup row (source='closure') reaching terminal must NOT breed a
    second-level __fu (the 小明 triple-nag root cause)."""
    import core.intentions as mod
    # Simulate a closure followup row
    fu = mod.create_intent(name="闭环: 饭后", trigger_type="date",
                           trigger_config={"datetime": "2026-06-14T11:00:00"},
                           source="closure", category="external",
                           closure_question="有什么跟进的吗？")
    row = mod.get_intent(fu)
    # _on_moment_terminal must early-return (source='closure')
    mod._on_moment_terminal(dict(row), how="executed")
    assert mod.get_intent(f"{fu}__fu") is None  # no second-level followup


def test_stale_closure_followup_expires_not_fires(intent_db, monkeypatch):
    """REQ-60: a closure follow-up overdue by > CLOSURE_STALE_DAYS is expired,
    not surfaced — the dinner ask must not nag 2 days late."""
    import core.intentions as mod
    from datetime import timedelta
    from core.timeutil import now_local
    stale = (now_local() - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%S")
    fu = mod.create_intent(name="闭环: 小明饭", trigger_type="date",
                           trigger_config={"datetime": stale},
                           source="closure", category="external",
                           closure_question="有什么跟进的吗？")
    due_ids = {d["id"] for d in mod.get_due_intents()}
    assert fu not in due_ids                       # NOT surfaced
    assert mod.get_intent(fu)["status"] == "expired"
    assert "stale" in (mod.get_intent(fu)["last_error"] or "")


def test_fresh_closure_followup_still_fires(intent_db):
    """A closure follow-up within the staleness window fires normally."""
    import core.intentions as mod
    from datetime import timedelta
    from core.timeutil import now_local
    recent = (now_local() - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%S")
    fu = mod.create_intent(name="闭环: 今天的饭", trigger_type="date",
                           trigger_config={"datetime": recent},
                           source="closure", category="external",
                           closure_question="有什么跟进的吗？")
    due_ids = {d["id"] for d in mod.get_due_intents()}
    assert fu in due_ids


# ── REQ-61: cron success clears last_error (not a status field) ───────────

def test_cron_success_clears_last_error(intent_db):
    import core.intentions as mod
    pid = mod.create_intent(name="小时报", trigger_type="cron",
                            trigger_config={"expression": "0 9-23 * * *"})
    mod.mark_triggered(pid)
    mod.mark_executed(pid, result="11:30-12:21：Pascal 在深度投入，无需打扰")
    row = mod.get_intent(pid)
    assert row["status"] == "pending"          # cron resets
    assert not row["last_error"]               # narration NOT stored as error


# ===========================================================================
# REQ-68 — calendar→intent idempotent upsert (kill prep/closure churn)
# REQ-70 — carry/prep reminders anchored to first-leave-time
# ===========================================================================

def _cal_md(date_str: str, lines: list[str], label: str = "Day 1") -> str:
    """Build calendar markdown for one date. `lines` are ('HH:MM-HH:MM', title)
    tuples or raw event strings."""
    body = [f"{label} ({date_str} 周一)"]
    for ln in lines:
        if isinstance(ln, tuple):
            span, title = ln
            body.append(f"  {span}  {title}")
        else:
            body.append(f"  {ln}")
    return "\n".join(body)


def _tags_of(intent: dict) -> set:
    return set(json.loads(intent.get("tags", "[]") or "[]"))


def _cal_rows():
    """All calendar-source intents in the test DB (any status)."""
    from core.intentions import list_intents
    return [i for i in list_intents(limit=1000) if i.get("source") == "calendar"]


def test_calendar_upsert_idempotent_no_duplicates(intent_db):
    """REQ-68: calling generate_calendar_intents TWICE on the same markdown
    creates NO duplicate prep/closure rows — the 小明 11-row churn (two preps
    at the same 17:00:16) cannot recur. At most one row per (date,title,role)."""
    from datetime import timedelta
    from core.timeutil import now_local
    import core.intentions as mod

    # Event tomorrow afternoon → a social dinner gets prep + closure.
    day = (now_local() + timedelta(days=1)).strftime("%Y-%m-%d")
    md = _cal_md(day, [("18:30-20:30", "和小明哥哥吃饭")])

    first = mod.generate_calendar_intents(md)
    second = mod.generate_calendar_intents(md)

    assert second == []                       # second sync creates nothing new
    rows = _cal_rows()
    # Exactly one prep + one closure for this single event.
    prep = [r for r in rows if f"cal:{day}:" in str(_tags_of(r))]
    close = [r for r in rows if f"cal-close:{day}:" in str(_tags_of(r))]
    assert len(prep) == 1, f"expected 1 prep, got {len(prep)}"
    assert len(close) == 1, f"expected 1 closure, got {len(close)}"
    assert len(first) == 2                     # first sync made exactly the pair


def test_calendar_upsert_supersedes_on_reschedule(intent_db):
    """REQ-68: a rescheduled event UPDATEs the existing prep/closure trigger in
    place — never a stale wrong-time row PLUS a fresh duplicate."""
    from datetime import timedelta
    from core.timeutil import now_local
    import core.intentions as mod

    day = (now_local() + timedelta(days=1)).strftime("%Y-%m-%d")
    mod.generate_calendar_intents(_cal_md(day, [("18:30-20:30", "和小明吃饭")]))
    # Event moved later; same date+title → supersede, not duplicate.
    mod.generate_calendar_intents(_cal_md(day, [("19:30-21:30", "和小明吃饭")]))

    rows = _cal_rows()
    prep = [r for r in rows if f"cal:{day}:" in str(_tags_of(r))]
    close = [r for r in rows if f"cal-close:{day}:" in str(_tags_of(r))]
    assert len(prep) == 1                       # still one prep
    assert len(close) == 1                      # still one closure
    # Prep trigger moved with the event (new start 19:30 → prep 19:00).
    cfg = json.loads(prep[0]["trigger_config"])
    assert cfg["datetime"][11:16] == "19:00"


def test_calendar_no_dup_after_prep_fired(intent_db):
    """REQ-68 core idempotency: once a prep has FIRED (left 'pending'), a
    re-sync must NOT create a second one. The old dedup only looked at
    pending/triggered, so a fired prep was invisible and got re-INSERTed —
    a direct source of the duplicate-prep churn."""
    from datetime import timedelta
    from core.timeutil import now_local
    import core.intentions as mod

    day = (now_local() + timedelta(days=1)).strftime("%Y-%m-%d")
    md = _cal_md(day, [("18:30-20:30", "晚餐 X")])
    created = mod.generate_calendar_intents(md)
    prep_id = [i for i in created
               if "calendar-prep" in _tags_of(mod.get_intent(i))][0]
    # Simulate the prep firing.
    mod.mark_triggered(prep_id); mod.mark_executed(prep_id)
    assert mod.get_intent(prep_id)["status"] == "executed"

    # Re-sync the SAME event → no second prep.
    mod.generate_calendar_intents(md)
    prep = [r for r in _cal_rows() if "calendar-prep" in _tags_of(r)]
    assert len(prep) == 1


def test_calendar_prep_after_event_start_skipped(intent_db, monkeypatch):
    """REQ-68.2: a prep whose computed fire time would be AT/AFTER the event
    start is skipped (the 18:00 prep that fired AFTER the 17:30 dinner). With a
    pathological lead the prep lands after start → must be dropped, not fired
    late."""
    from datetime import timedelta
    from core.timeutil import now_local
    import core.intentions as mod

    # Event ~3h out, but a 5h prep lead would put prep AFTER the event start.
    monkeypatch.setattr(mod, "PREP_LEAD", timedelta(hours=5))
    start = (now_local() + timedelta(hours=3)).replace(second=0, microsecond=0)
    day = start.strftime("%Y-%m-%d")
    span = f"{start.strftime('%H:%M')}-{(start + timedelta(hours=1)).strftime('%H:%M')}"
    md = _cal_md(day, [(span, "运动康复 康复课")])

    created = mod.generate_calendar_intents(md)
    prep = [i for i in created
            if "calendar-prep" in _tags_of(mod.get_intent(i))]
    assert prep == []                           # prep dropped (would fire late)


def test_calendar_carry_reminder_fires_in_morning_not_at_event(intent_db):
    """REQ-70: a morning carry reminder for an AFTERNOON event fires in the
    morning, not at the event's own prep time. The 12:30 康复课 伞 must
    be anchored to a sane morning hour, not lunchtime."""
    from datetime import timedelta
    from core.timeutil import now_local
    import core.intentions as mod

    # Tomorrow so the morning anchor is safely in the future regardless of
    # the current wall-clock hour.
    day = (now_local() + timedelta(days=1)).strftime("%Y-%m-%d")
    md = _cal_md(day, [("12:30-13:30", "运动康复 康复课 记得带伞")])

    created = mod.generate_calendar_intents(md)
    carry = [mod.get_intent(i) for i in created
             if "calendar-carry" in _tags_of(mod.get_intent(i))]
    assert len(carry) == 1, "expected one morning carry checklist"
    cfg = json.loads(carry[0]["trigger_config"])
    fire_hour = int(cfg["datetime"][11:13])
    # Anchored to the morning ceiling (event is much later), NOT 12:30/11:30.
    assert fire_hour <= mod.CARRY_MORNING_CEILING
    assert fire_hour < 12, f"carry fired at {fire_hour}:xx — should be morning"
    # expires at first-leave (travel-pause hygiene): never lingers past the day.
    assert carry[0]["expires_at"].startswith(day)


def test_calendar_carry_anchors_to_first_leave(intent_db):
    """REQ-70: with multiple events, the carry checklist anchors to the FIRST
    out-of-home event (~CARRY_LEAD before), and MERGES all carry items into ONE
    intent (one card, not N)."""
    from datetime import timedelta
    from core.timeutil import now_local
    import core.intentions as mod

    day = (now_local() + timedelta(days=1)).strftime("%Y-%m-%d")
    md = _cal_md(day, [
        ("10:00-11:00", "羽毛球 带球拍"),       # first leave + carry
        ("15:00-16:00", "要还的书 拿给图书馆"),   # later carry (要还 cue)
    ])
    created = mod.generate_calendar_intents(md)
    carry = [mod.get_intent(i) for i in created
             if "calendar-carry" in _tags_of(mod.get_intent(i))]
    assert len(carry) == 1                       # merged into ONE checklist
    cfg = json.loads(carry[0]["trigger_config"])
    fire_h, fire_m = int(cfg["datetime"][11:13]), int(cfg["datetime"][14:16])
    # 10:00 first leave, CARRY_LEAD=75min → ~08:45 (>= floor 07:00).
    assert (fire_h, fire_m) == (8, 45), f"expected 08:45, got {fire_h:02d}:{fire_m:02d}"
    # Both carry items merged into the one prompt.
    assert "羽毛球" in carry[0]["prompt"]
    assert "要还的书" in carry[0]["prompt"]


def test_calendar_carry_idempotent(intent_db):
    """REQ-68+70: re-syncing does not duplicate the carry checklist."""
    from datetime import timedelta
    from core.timeutil import now_local
    import core.intentions as mod

    day = (now_local() + timedelta(days=1)).strftime("%Y-%m-%d")
    md = _cal_md(day, [("10:00-11:00", "羽毛球 带球拍")])
    mod.generate_calendar_intents(md)
    second = mod.generate_calendar_intents(md)
    assert second == []
    carry = [r for r in _cal_rows() if "calendar-carry" in _tags_of(r)]
    assert len(carry) == 1


def test_calendar_no_carry_when_nothing_to_bring(intent_db):
    """A plain meeting with no carry cue gets NO carry checklist (no noise)."""
    from datetime import timedelta
    from core.timeutil import now_local
    import core.intentions as mod

    day = (now_local() + timedelta(days=1)).strftime("%Y-%m-%d")
    md = _cal_md(day, [("15:00-16:00", "线上同步会")])
    created = mod.generate_calendar_intents(md)
    carry = [r for r in _cal_rows() if "calendar-carry" in _tags_of(r)]
    assert carry == []
    # but a regular prep is still created
    assert any("calendar-prep" in _tags_of(mod.get_intent(i)) for i in created)


# ===========================================================================
# REQ-85 — all-day status blocks filtered + true-start-day keying
# (the 15-row Prep:请假 create/cancel churn class, hard deadline 7/11)
# ===========================================================================

def _multi_day_md(day_events: list[tuple[str, list]]) -> str:
    """Join several _cal_md day sections. day_events = [(date_str, lines)]."""
    return "\n".join(_cal_md(d, lines, label=f"Day {i + 1}")
                     for i, (d, lines) in enumerate(day_events))


def _map_entry_for(date_str, time_str, summary, start_local, end_local,
                   eid="evt_1"):
    """A calendar_event_mapping.json-style entry. start/end are aware local
    datetimes, serialized to the Z-suffixed UTC form Lark emits — so the test
    exercises the .replace('Z', '+00:00') + to-local path for real."""
    from datetime import timezone

    def z(dt):
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    return {"event_id": eid, "calendar_id": "cal_x", "summary": summary,
            "time": time_str, "date": date_str,
            "start_iso": z(start_local), "end_iso": z(end_local)}


def test_calendar_allday_status_block_generates_nothing(intent_db):
    """REQ-85(a): a 00:00-00:00 status block (请假/请假/OOO) is calendar STATE —
    zero prep, zero closure, zero carry, even when the title carries carry
    (带电脑) and social (饭) cues that would otherwise trigger those roles."""
    from datetime import timedelta
    from core.timeutil import now_local
    import core.intentions as mod

    day = (now_local() + timedelta(days=1)).strftime("%Y-%m-%d")
    md = _cal_md(day, [("00:00-00:00", "年假 OOO 带电脑回家吃饭")])
    created = mod.generate_calendar_intents(md, event_map=[])
    assert created == []
    assert _cal_rows() == []


def test_calendar_status_filter_does_not_overmatch(intent_db):
    """REQ-85(a) 防误杀: a 00:00-08:00 红眼航班 (all-night, no keyword) and a
    14:00-15:00 请假面谈 (keyword, not all-day) both still get their prep."""
    from datetime import timedelta
    from core.timeutil import now_local
    import core.intentions as mod

    # 红眼 two days out so its 00:00 start is always >2h ahead of `now`.
    day2 = (now_local() + timedelta(days=2)).strftime("%Y-%m-%d")
    day1 = (now_local() + timedelta(days=1)).strftime("%Y-%m-%d")
    md = _multi_day_md([(day1, [("14:00-15:00", "请假面谈")]),
                        (day2, [("00:00-08:00", "红眼航班")])])
    created = mod.generate_calendar_intents(md, event_map=[])
    preps = [i for i in created
             if "calendar-prep" in _tags_of(mod.get_intent(i))]
    assert len(preps) == 2


def test_calendar_multiday_event_single_prep_keyed_on_start_day(intent_db):
    """REQ-85(b) 主修: a multi-day event generates exactly ONE prep, keyed on
    its true start day; re-sync is idempotent; continuation days create
    nothing (the per-day cal: key churn cannot recur)."""
    from datetime import timedelta
    from core.timeutil import now_local
    import core.intentions as mod

    start_local = (now_local() + timedelta(days=1)).replace(
        hour=12, minute=0, second=0, microsecond=0)
    end_local = (start_local + timedelta(days=3)).replace(minute=30)
    days = [(start_local + timedelta(days=i)).strftime("%Y-%m-%d")
            for i in range(4)]
    md = _multi_day_md([(d, [("12:00-12:30", "冲绳旅行")]) for d in days])
    emap = [_map_entry_for(d, "12:00-12:30", "冲绳旅行", start_local, end_local)
            for d in days]

    first = mod.generate_calendar_intents(md, event_map=emap)
    preps = [r for r in _cal_rows() if "calendar-prep" in _tags_of(r)]
    assert len(first) == 1 and len(preps) == 1
    assert f"cal:{days[0]}:冲绳旅行" in _tags_of(preps[0])   # start-day key
    # Re-sync: byte-identical key already present → nothing new.
    assert mod.generate_calendar_intents(md, event_map=emap) == []
    assert len([r for r in _cal_rows() if "calendar-prep" in _tags_of(r)]) == 1


def test_calendar_multiday_event_in_progress_generates_nothing(intent_db):
    """REQ-85(b): once a multi-day event has STARTED, every remaining rendered
    day is a continuation day → zero generation for the rest of the event
    (请假 from 7/2 must produce nothing through 7/11)."""
    from datetime import timedelta
    from core.timeutil import now_local
    import core.intentions as mod

    start_local = (now_local() - timedelta(days=1)).replace(
        hour=12, minute=0, second=0, microsecond=0)
    end_local = (start_local + timedelta(days=3)).replace(minute=30)
    days = [(start_local + timedelta(days=i)).strftime("%Y-%m-%d")
            for i in range(1, 4)]   # today .. start+3 (start day already past)
    md = _multi_day_md([(d, [("12:00-12:30", "冲绳旅行")]) for d in days])
    emap = [_map_entry_for(d, "12:00-12:30", "冲绳旅行", start_local, end_local)
            for d in days]
    assert mod.generate_calendar_intents(md, event_map=emap) == []
    assert _cal_rows() == []


def test_calendar_continuation_skip_logged_once_per_day(intent_db, capsys):
    """Log-hygiene fix (2026-07-07): the continuation-day skip line fired for
    every rendered future day on EVERY cycle (~1,475 请假 lines/day in
    jarvis.log). It must log on the first sync of the day and stay silent on
    re-syncs — while the skip itself (zero generation) keeps working."""
    from datetime import timedelta
    from core.timeutil import now_local
    import core.intentions as mod

    start_local = (now_local() - timedelta(days=1)).replace(
        hour=12, minute=0, second=0, microsecond=0)
    end_local = (start_local + timedelta(days=3)).replace(minute=30)
    days = [(start_local + timedelta(days=i)).strftime("%Y-%m-%d")
            for i in range(1, 4)]
    md = _multi_day_md([(d, [("12:00-12:30", "冲绳旅行")]) for d in days])
    emap = [_map_entry_for(d, "12:00-12:30", "冲绳旅行", start_local, end_local)
            for d in days]

    assert mod.generate_calendar_intents(md, event_map=emap) == []
    first = capsys.readouterr().err
    assert first.count("skip continuation day") == len(days)
    # Same cycle content again (the every-10-min re-sync): still skipped,
    # but no repeat log lines.
    assert mod.generate_calendar_intents(md, event_map=emap) == []
    assert "skip continuation day" not in capsys.readouterr().err


def test_calendar_multiday_no_resurrection_of_cancelled_legacy_rows(intent_db):
    """REQ-85(b) 旧 key 兼容: a hand-cancelled legacy per-day row anywhere in
    the event's true span blocks a new start-day prep — the 15 cancelled 请假
    rows must never be recreated (range fallback dedup, zero migration)."""
    from datetime import timedelta
    from core.timeutil import now_local
    import core.intentions as mod

    start_local = (now_local() + timedelta(days=1)).replace(
        hour=12, minute=0, second=0, microsecond=0)
    end_local = (start_local + timedelta(days=2)).replace(minute=30)
    days = [(start_local + timedelta(days=i)).strftime("%Y-%m-%d")
            for i in range(3)]
    # Legacy row keyed on a MIDDLE day of the span, already cancelled by hand.
    legacy = mod.create_intent(
        name="Prep: 冲绳旅行", trigger_type="date",
        trigger_config={"datetime": "2999-01-01T09:00:00"},
        tags=[f"cal:{days[1]}:冲绳旅行", "calendar-prep"], source="calendar")
    mod.cancel_intent(legacy, "manual cleanup")

    md = _multi_day_md([(d, [("12:00-12:30", "冲绳旅行")]) for d in days])
    emap = [_map_entry_for(d, "12:00-12:30", "冲绳旅行", start_local, end_local)
            for d in days]
    assert mod.generate_calendar_intents(md, event_map=emap) == []
    rows = [r for r in _cal_rows() if "calendar-prep" in _tags_of(r)]
    assert len(rows) == 1 and rows[0]["id"] == legacy   # nothing revived


def test_calendar_multiday_social_closure_uses_true_end(intent_db):
    """REQ-85(b): a multi-day SOCIAL event's closure fires after the event's
    REAL end (end_iso), not after the first day's rendered 12:30 slice."""
    from datetime import timedelta
    from core.timeutil import now_local
    import core.intentions as mod

    start_local = (now_local() + timedelta(days=1)).replace(
        hour=12, minute=0, second=0, microsecond=0)
    end_local = (start_local + timedelta(days=3)).replace(minute=30)
    days = [(start_local + timedelta(days=i)).strftime("%Y-%m-%d")
            for i in range(4)]
    md = _multi_day_md([(d, [("12:00-12:30", "团建聚会之旅")]) for d in days])
    emap = [_map_entry_for(d, "12:00-12:30", "团建聚会之旅", start_local, end_local)
            for d in days]
    created = mod.generate_calendar_intents(md, event_map=emap)
    close = [mod.get_intent(i) for i in created
             if "calendar-close" in _tags_of(mod.get_intent(i))]
    assert len(close) == 1
    cfg = json.loads(close[0]["trigger_config"])
    # snap_to_golden(true end 12:30 + 90min) = last day 14:00 — NOT day 1.
    assert cfg["datetime"][:10] == days[3]
    assert close[0]["expires_at"] > end_local.isoformat()[:19]


def test_calendar_recurring_daily_meeting_not_merged(intent_db):
    """REQ-85(b) 例会不误伤: two days of the same-named recurring instance each
    carry their OWN per-day start_iso → each day is its own start day → two
    preps (the decisive reason for mapping over a title-merge heuristic)."""
    from datetime import timedelta
    from core.timeutil import now_local
    import core.intentions as mod

    d1 = (now_local() + timedelta(days=1)).replace(hour=10, minute=0,
                                                   second=0, microsecond=0)
    d2 = d1 + timedelta(days=1)
    md = _multi_day_md([
        (d1.strftime("%Y-%m-%d"), [("10:00-10:15", "每日例会")]),
        (d2.strftime("%Y-%m-%d"), [("10:00-10:15", "每日例会")]),
    ])
    emap = [
        _map_entry_for(d1.strftime("%Y-%m-%d"), "10:00-10:15", "每日例会",
                       d1, d1 + timedelta(minutes=15), eid="evt_recur"),
        _map_entry_for(d2.strftime("%Y-%m-%d"), "10:00-10:15", "每日例会",
                       d2, d2 + timedelta(minutes=15), eid="evt_recur"),
    ]
    created = mod.generate_calendar_intents(md, event_map=emap)
    preps = [r for r in _cal_rows() if "calendar-prep" in _tags_of(r)]
    assert len(preps) == 2
    tags = set().union(*[_tags_of(r) for r in preps])
    assert f"cal:{d1.strftime('%Y-%m-%d')}:每日例会" in tags
    assert f"cal:{d2.strftime('%Y-%m-%d')}:每日例会" in tags


def test_calendar_event_map_garbage_falls_back_per_day(intent_db):
    """REQ-85(b) fail-open: a matching entry with garbage start/end_iso must
    fall back to plain per-day handling — never crash, never drop the prep."""
    from datetime import timedelta
    from core.timeutil import now_local
    import core.intentions as mod

    day = (now_local() + timedelta(days=1)).strftime("%Y-%m-%d")
    md = _cal_md(day, [("15:00-16:00", "线上同步会")])
    emap = [{"event_id": "evt_bad", "summary": "线上同步会", "date": day,
             "time": "15:00-16:00", "start_iso": "not-a-date", "end_iso": ""}]
    created = mod.generate_calendar_intents(md, event_map=emap)
    preps = [i for i in created
             if "calendar-prep" in _tags_of(mod.get_intent(i))]
    assert len(preps) == 1


# ===========================================================================
# REQ-85(c) — expires_at lapse leaves a trace; REQ-90 — closure edge cases
# ===========================================================================

def test_cleanup_expired_emits_trace_event(intent_db, monkeypatch):
    """REQ-85(c): a date prep dying via expires_at must leave a visible trace
    (intent_expired reason=expires_at_lapsed) — the lone expired 'Prep: 发散'
    was undiagnosable under the old bare UPDATE. Idempotent: a second sweep
    emits nothing new."""
    from datetime import timedelta
    from core.timeutil import now_local
    import core.intentions as mod

    events = []
    monkeypatch.setattr(mod, "_emit_intent",
                        lambda ev, iid, **f: events.append((ev, iid, f)))
    past = (now_local() - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S")
    expired_at = (now_local() - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S")
    iid = mod.create_intent(name="Prep: 发散", trigger_type="date",
                            trigger_config={"datetime": past},
                            expires_at=expired_at)
    mod.cleanup_expired()
    assert mod.get_intent(iid)["status"] == "expired"
    traces = [(ev, i, f) for ev, i, f in events if ev == "intent_expired"]
    assert traces == [("intent_expired", iid,
                       {"reason": "expires_at_lapsed", "name": "Prep: 发散"})]
    events.clear()
    mod.cleanup_expired()                       # already expired → no re-emit
    assert events == []


def test_context_moment_closes_na_on_execute(intent_db):
    """REQ-90①: a context moment (followup=None policy) reaching terminal must
    CLOSE its closure axis (na + closed_at), not fall through writing nothing —
    the int_879cb1472b black-hole class. No follow-up row is spawned."""
    from core.intentions import create_intent, get_intent

    pid = create_intent(name="Prep: 某会", trigger_type="date",
                        trigger_config={"datetime": "2026-06-30T10:00:00"},
                        category="context", closure_question="有要跟进的吗？")
    _fire(pid)
    row = get_intent(pid)
    assert row["closure_status"] == "na"
    assert row["closed_at"]
    assert "no-followup policy" in row["closure_result"]
    assert get_intent(f"{pid}__fu") is None      # nothing spawned
    _fire(pid)                                    # re-execute → guard: no change
    assert get_intent(pid)["closure_status"] == "na"


def test_cron_intent_coerces_closure_question(intent_db):
    """REQ-90②: cron intents never spawn follow-ups, so closure_question is a
    dead field on them — coerced to empty at create, never raised."""
    from core.intentions import create_intent, get_intent

    pid = create_intent(name="每日跟进", trigger_type="cron",
                        trigger_config={"expression": "0 9 * * *"},
                        category="external", closure_question="进展如何？")
    assert get_intent(pid)["closure_question"] == ""


def test_record_closure_done_empty_result_coerced_to_na(intent_db):
    """REQ-90③: done with an empty result is a claim without a record —
    coerced to na (never raise: a raise would fail the closure write itself
    and mint a new zombie class)."""
    from core.intentions import create_intent, get_intent, record_closure

    pid = create_intent(name="p", trigger_type="date",
                        trigger_config={"datetime": "2999-01-01T09:00:00"})
    assert record_closure(pid, outcome="done", result="   ") is True
    row = get_intent(pid)
    assert row["closure_status"] == "na"
    assert row["closed_at"]


def test_closure_done_button_carries_result(intent_db):
    """Companion to REQ-90③: the ✅ button must send a non-empty result, or
    every tap would be silently downgraded to na by the coerce."""
    from tasks.intentions_post import _closure_buttons

    buttons = _closure_buttons([{"parent": "int_x", "name": "饭局"}])
    done = [b for b in buttons if b["value"].get("outcome") == "done"]
    assert done and str(done[0]["value"].get("result", "")).strip()


def test_backfill_req90_script_dry_run_and_apply(intent_db, tmp_path):
    """The backfill script identifies the black-hole rows in dry-run mode and
    closes exactly them (and only non-terminal ones) with --apply."""
    import sqlite3 as sq
    from datetime import datetime
    from scripts.backfill_req90_context_closures import main as backfill_main

    db_path = tmp_path / "jarvis_copy.db"
    conn = sq.connect(str(db_path))
    conn.execute("""CREATE TABLE intentions (
        id TEXT PRIMARY KEY, name TEXT, status TEXT, category TEXT,
        closure_status TEXT DEFAULT 'none', closure_result TEXT DEFAULT '',
        closed_at TEXT)""")
    conn.executemany(
        "INSERT INTO intentions (id, name, status, category, closure_status) "
        "VALUES (?, ?, 'executed', 'context', ?)",
        [("int_879cb1472b", "blackhole-1", "none"),
         ("int_d9aa5c5668", "blackhole-2", "none")])
    conn.commit(); conn.close()

    # Dry run: identifies both, writes nothing.
    assert backfill_main(["--db", str(db_path)]) == 0
    conn = sq.connect(str(db_path))
    assert conn.execute("SELECT COUNT(*) FROM intentions "
                        "WHERE closure_status = 'none'").fetchone()[0] == 2
    conn.close()

    # Apply: both closed na with closed_at; second apply is a no-op.
    assert backfill_main(["--db", str(db_path), "--apply"]) == 0
    conn = sq.connect(str(db_path))
    rows = conn.execute("SELECT closure_status, closed_at, closure_result "
                        "FROM intentions").fetchall()
    conn.close()
    for status, closed_at, result in rows:
        assert status == "na" and closed_at
        assert "backfill REQ-90" in result
    assert backfill_main(["--db", str(db_path), "--apply"]) == 0


# ---------------------------------------------------------------------------
# update_intent — field whitelist, JSON serialization, edge cases
# ---------------------------------------------------------------------------

def test_update_intent_basic_field(intent_db):
    """A simple scalar field update should persist and be readable back."""
    from core.intentions import create_intent, get_intent, update_intent

    iid = create_intent(
        name="original name",
        trigger_type="date",
        trigger_config={"datetime": "2099-01-01T09:00:00"},
        prompt="test",
    )
    result = update_intent(iid, name="updated name")
    assert result is True
    assert get_intent(iid)["name"] == "updated name"


def test_update_intent_json_field_serialization(intent_db):
    """JSON-typed fields (tags, trigger_config, etc.) passed as native Python
    objects must be serialized to JSON strings for storage and readable back."""
    from core.intentions import create_intent, get_intent, update_intent

    iid = create_intent(
        name="json test",
        trigger_type="date",
        trigger_config={"datetime": "2099-01-01T09:00:00"},
        prompt="test",
    )
    new_tags = ["urgent", "closure-followup"]
    new_trigger = {"datetime": "2099-06-15T12:00:00"}
    new_context = {"key": "value", "nested": {"a": 1}}
    update_intent(iid, tags=new_tags, trigger_config=new_trigger, context=new_context)

    intent = get_intent(iid)
    assert json.loads(intent["tags"]) == new_tags
    assert json.loads(intent["trigger_config"]) == new_trigger
    assert json.loads(intent["context"]) == new_context


def test_update_intent_disallowed_fields_rejected(intent_db):
    """Passing only disallowed field names should return False (no-op)."""
    from core.intentions import create_intent, get_intent, update_intent

    iid = create_intent(
        name="guard test",
        trigger_type="date",
        trigger_config={"datetime": "2099-01-01T09:00:00"},
        prompt="test",
    )
    result = update_intent(iid, id="hacked", nonexistent_field="nope")
    assert result is False
    assert get_intent(iid)["name"] == "guard test"


def test_update_intent_mixed_allowed_and_disallowed(intent_db):
    """When both allowed and disallowed fields are passed, only allowed ones
    should be applied; the call should return True."""
    from core.intentions import create_intent, get_intent, update_intent

    iid = create_intent(
        name="mixed test",
        trigger_type="date",
        trigger_config={"datetime": "2099-01-01T09:00:00"},
        prompt="original prompt",
        priority=5,
    )
    result = update_intent(iid, prompt="new prompt", id="hacked", bogus="x")
    assert result is True
    intent = get_intent(iid)
    assert intent["prompt"] == "new prompt"
    assert intent["id"] == iid  # unchanged


def test_update_intent_empty_kwargs(intent_db):
    """Calling with no kwargs should return False."""
    from core.intentions import create_intent, update_intent

    iid = create_intent(
        name="empty test",
        trigger_type="date",
        trigger_config={"datetime": "2099-01-01T09:00:00"},
        prompt="test",
    )
    assert update_intent(iid) is False


# ── CLI integration tests ──────────────────────────────────────────────


def _run_cli(intent_db, argv):
    """Run _cli with patched DB, capturing stdout/stderr."""
    import io
    import contextlib
    from core.intentions import _cli
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = _cli(argv)
    return rc, out.getvalue(), err.getvalue()


def _seed_intent(name="test task", dt="2099-12-31T09:00:00", prompt="remind me"):
    from core.intentions import create_intent
    return create_intent(
        name=name,
        trigger_type="date",
        trigger_config={"datetime": dt},
        prompt=prompt,
    )


def test_cli_list_default(intent_db):
    iid = _seed_intent()
    rc, out, _ = _run_cli(intent_db, ["list"])
    assert rc == 0
    assert "1 intent(s)" in out
    assert iid in out


def test_cli_list_by_status(intent_db):
    _seed_intent()
    rc, out, _ = _run_cli(intent_db, ["list", "pending"])
    assert rc == 0
    assert "1 intent(s) status=pending" in out

    rc2, out2, _ = _run_cli(intent_db, ["list", "executed"])
    assert rc2 == 0
    assert "0 intent(s)" in out2


def test_cli_get(intent_db):
    iid = _seed_intent(name="get-me")
    rc, out, _ = _run_cli(intent_db, ["get", iid])
    assert rc == 0
    parsed = json.loads(out)
    assert parsed["name"] == "get-me"
    assert parsed["id"] == iid


def test_cli_get_missing(intent_db):
    rc, out, _ = _run_cli(intent_db, ["get", "nonexistent"])
    assert rc == 1
    assert "not found" in out


def test_cli_due(intent_db):
    rc, out, _ = _run_cli(intent_db, ["due"])
    assert rc == 0
    assert "0 due now" in out


def test_cli_cancel(intent_db):
    iid = _seed_intent()
    rc, out, _ = _run_cli(intent_db, ["cancel", iid, "no longer needed"])
    assert rc == 0
    assert f"cancelled {iid}" in out
    from core.intentions import get_intent
    assert get_intent(iid)["status"] == "cancelled"


def test_cli_cancel_missing(intent_db):
    rc, out, _ = _run_cli(intent_db, ["cancel", "ghost"])
    assert rc == 1
    assert "not found" in out


def test_cli_delete(intent_db):
    iid = _seed_intent()
    rc, out, _ = _run_cli(intent_db, ["delete", iid])
    assert rc == 0
    assert "gone=True" in out
    from core.intentions import get_intent
    assert get_intent(iid) is None


def test_cli_stats(intent_db):
    _seed_intent()
    rc, out, _ = _run_cli(intent_db, ["stats"])
    assert rc == 0
    parsed = json.loads(out)
    assert parsed.get("pending", 0) >= 1


def test_cli_stats_closure(intent_db):
    _seed_intent()
    rc, out, _ = _run_cli(intent_db, ["stats", "--closure"])
    assert rc == 0
    parsed = json.loads(out)
    assert "total" in parsed or "by_status" in parsed or isinstance(parsed, dict)


def test_cli_purge_terminal(intent_db):
    from core.intentions import create_intent, update_intent
    iid = create_intent(
        name="done task",
        trigger_type="date",
        trigger_config={"datetime": "2026-01-01T09:00:00"},
        prompt="x",
    )
    update_intent(iid, status="executed")
    rc, out, _ = _run_cli(intent_db, ["purge", "executed"])
    assert rc == 0
    assert "purged 1" in out


def test_cli_purge_refuses_pending(intent_db):
    _seed_intent()
    rc, _, err = _run_cli(intent_db, ["purge", "pending"])
    assert rc == 2
    assert "refuses" in err


def test_cli_unknown_command(intent_db):
    rc, _, err = _run_cli(intent_db, ["bogus"])
    assert rc == 2
    assert "unknown command" in err


def test_cli_awaiting(intent_db):
    rc, out, _ = _run_cli(intent_db, ["awaiting"])
    assert rc == 0
    assert "0 awaiting closure" in out


def test_cli_create(intent_db):
    rc, out, _ = _run_cli(intent_db, [
        "create", "VC follow-up", "date", '{"datetime":"2099-12-31T09:00:00"}',
        "--prompt", "follow up with VC", "--purpose", "test",
        "--priority", "3", "--tags", "vc,inbound",
    ])
    assert rc == 0
    assert "created" in out
    assert "VC follow-up" in out
    from core.intentions import get_intent
    iid = out.split()[1]
    it = get_intent(iid)
    assert it["name"] == "VC follow-up"
    assert it["priority"] == 3
    assert it["prompt"] == "follow up with VC"


def test_cli_create_bad_json(intent_db):
    rc, _, err = _run_cli(intent_db, ["create", "x", "date", "NOT-JSON"])
    assert rc == 2
    assert "bad trigger_config JSON" in err


def test_cli_create_missing_args(intent_db):
    rc, _, err = _run_cli(intent_db, ["create", "x"])
    assert rc == 2
    assert "usage" in err
