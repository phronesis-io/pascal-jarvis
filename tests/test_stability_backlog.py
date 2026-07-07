"""Stability backlog #4 + #7 (6/15 brain-death postmortem).

#4 — night/batch queue delivery accounting + bounded growth: tri-state flush
return, per-entry JSONL audit (capped), age/retry expiry so a forever-failing
flush can't grow the queue forever.

#7 — delivery-alert self-survival, producer side: the in-loop tracker writes
each overdue delivery to data/.delivery_deadletter.jsonl (append under
fcntl.flock) so the daemon can raise the alarm even when the loop is dead.
"""

import fcntl
import json
import time

import core.delivery_deadletter as ddl
import core.heartbeat_loop as hbl
from core.delivery_deadletter import DEADLETTER_FILE, record_overdue
from core.heartbeat_loop import (
    DELIVERY_ALERT_THRESHOLD,
    FLUSH_DELIVERED,
    FLUSH_PERMANENT,
    FLUSH_RETRYABLE,
    NIGHT_FLUSH_MAX_RETRIES,
    NIGHT_QUEUE_FILE,
    QUIET_FLUSH_AUDIT_FILE,
    QUIET_FLUSH_AUDIT_KEEP,
    QUIET_FLUSH_AUDIT_REWRITE_AT,
    _flush_night_queue,
    _note_delivery,
    _queue_for_morning,
)


def _audit_rows(tmp_path):
    p = tmp_path / QUIET_FLUSH_AUDIT_FILE
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text().splitlines()]


def _deadletter_rows(tmp_path):
    p = tmp_path / DEADLETTER_FILE
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text().splitlines()]


def _queue_entry(tmp_path, text, source="content-recommend"):
    (tmp_path / ".heartbeat_last_source").write_text(source)
    _queue_for_morning(text, tmp_path)


# ── #4a: tri-state flush return + per-entry accounting ───────────────


def test_flush_delivered_returns_tristate_and_audits(tmp_path, monkeypatch):
    _queue_entry(tmp_path, "早间内容")
    monkeypatch.setattr(hbl, "_lark_send_text", lambda t, u: True)
    assert _flush_night_queue(tmp_path, "ou_test") == FLUSH_DELIVERED
    rows = _audit_rows(tmp_path)
    assert len(rows) == 1
    assert rows[0]["status"] == FLUSH_DELIVERED
    assert rows[0]["source"] == "content-recommend"
    assert "早间内容" in rows[0]["text_preview"]


def test_flush_retryable_keeps_entries_and_bumps_retry(tmp_path, monkeypatch):
    _queue_entry(tmp_path, "夜间内容")
    monkeypatch.setattr(hbl, "_lark_send_text", lambda t, u: False)
    assert _flush_night_queue(tmp_path, "ou_test") == FLUSH_RETRYABLE
    kept = [json.loads(l) for l in
            (tmp_path / NIGHT_QUEUE_FILE).read_text().splitlines()]
    assert kept[0]["retries"] == 1
    assert kept[0]["text"] == "夜间内容"  # content untouched
    rows = _audit_rows(tmp_path)
    assert rows and rows[-1]["status"] == FLUSH_RETRYABLE


def test_flush_permanent_when_no_user_id(tmp_path, monkeypatch):
    _queue_entry(tmp_path, "无处可送")
    sent = []
    monkeypatch.setattr(hbl, "_lark_send_text", lambda t, u: sent.append(t) or True)
    assert _flush_night_queue(tmp_path, "") == FLUSH_PERMANENT
    assert sent == []
    assert not (tmp_path / NIGHT_QUEUE_FILE).exists()  # never queued forever
    rows = _audit_rows(tmp_path)
    assert rows and rows[0]["status"] == FLUSH_PERMANENT
    dl = _deadletter_rows(tmp_path)
    assert dl and dl[0]["kind"] == "night_queue_undeliverable"


def test_flush_empty_queue_returns_falsy(tmp_path):
    assert not _flush_night_queue(tmp_path, "ou_test")


# ── #4b: bounded growth — age / retry expiry ─────────────────────────


def test_old_entry_expires_to_audit_not_digest(tmp_path, monkeypatch):
    _queue_entry(tmp_path, "新内容")
    # hand-plant a 49h-old entry (epoch is the expiry clock)
    old = {"ts": "2026-07-05 08:00", "epoch": int(time.time()) - 49 * 3600,
           "text": "旧内容", "source": "checkin"}
    with open(tmp_path / NIGHT_QUEUE_FILE, "a") as f:
        f.write(json.dumps(old, ensure_ascii=False) + "\n")
    sent = []
    monkeypatch.setattr(hbl, "_lark_send_text", lambda t, u: sent.append(t) or True)
    assert _flush_night_queue(tmp_path, "ou_test") == FLUSH_DELIVERED
    assert "旧内容" not in sent[0] and "新内容" in sent[0]
    by_status = {r["status"]: r for r in _audit_rows(tmp_path)}
    assert "旧内容" in by_status["expired"]["text_preview"]
    assert "新内容" in by_status[FLUSH_DELIVERED]["text_preview"]
    # #7: each expired promised delivery leaves a dead-letter line
    dl = _deadletter_rows(tmp_path)
    assert len(dl) == 1
    assert dl[0]["kind"] == "night_queue_expired"
    assert dl[0]["due_since"] == "2026-07-05 08:00"
    assert set(dl[0]) >= {"ts", "kind", "detail", "due_since"}


def test_retry_exhausted_entry_expires(tmp_path, monkeypatch):
    entry = {"ts": hbl.now_local_str("%Y-%m-%d %H:%M"), "epoch": int(time.time()),
             "text": "屡试屡败", "source": "checkin",
             "retries": NIGHT_FLUSH_MAX_RETRIES}
    (tmp_path / NIGHT_QUEUE_FILE).write_text(
        json.dumps(entry, ensure_ascii=False) + "\n")
    sent = []
    monkeypatch.setattr(hbl, "_lark_send_text", lambda t, u: sent.append(t) or True)
    assert not _flush_night_queue(tmp_path, "ou_test")  # nothing left to send
    assert sent == []
    assert not (tmp_path / NIGHT_QUEUE_FILE).exists()
    rows = _audit_rows(tmp_path)
    assert rows and rows[0]["status"] == "expired"
    assert _deadletter_rows(tmp_path)[0]["kind"] == "night_queue_expired"


def test_forever_failing_flush_expires_after_max_retries(tmp_path, monkeypatch):
    _queue_entry(tmp_path, "永远发不出去")
    monkeypatch.setattr(hbl, "_lark_send_text", lambda t, u: False)
    for _ in range(NIGHT_FLUSH_MAX_RETRIES):
        assert _flush_night_queue(tmp_path, "ou_test") == FLUSH_RETRYABLE
    # retry budget exhausted → next flush expires it instead of retrying forever
    assert not _flush_night_queue(tmp_path, "ou_test")
    assert not (tmp_path / NIGHT_QUEUE_FILE).exists()
    assert _audit_rows(tmp_path)[-1]["status"] == "expired"


def test_unparseable_legacy_ts_expires(tmp_path, monkeypatch):
    (tmp_path / NIGHT_QUEUE_FILE).write_text(
        json.dumps({"text": "无时间戳", "source": "checkin"}) + "\n")
    monkeypatch.setattr(hbl, "_lark_send_text", lambda t, u: True)
    assert not _flush_night_queue(tmp_path, "ou_test")
    assert _audit_rows(tmp_path)[0]["status"] == "expired"


# ── #4a: audit cap — keep last N, rewrite past 2N ────────────────────


def test_audit_cap_rewrites_past_double(tmp_path):
    p = tmp_path / QUIET_FLUSH_AUDIT_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("".join(json.dumps({"status": "delivered", "i": i}) + "\n"
                         for i in range(QUIET_FLUSH_AUDIT_REWRITE_AT)))
    hbl._audit_flush(tmp_path, [{"text": "触发重写", "source": "s"}], "delivered")
    rows = _audit_rows(tmp_path)
    assert len(rows) == QUIET_FLUSH_AUDIT_KEEP
    assert "触发重写" in rows[-1]["text_preview"]  # newest row survives the trim


def test_audit_no_rewrite_below_threshold(tmp_path):
    p = tmp_path / QUIET_FLUSH_AUDIT_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("".join(json.dumps({"status": "delivered", "i": i}) + "\n"
                         for i in range(QUIET_FLUSH_AUDIT_KEEP)))
    hbl._audit_flush(tmp_path, [{"text": "x", "source": "s"}], "delivered")
    assert len(_audit_rows(tmp_path)) == QUIET_FLUSH_AUDIT_KEEP + 1


# ── #7: dead-letter producer + contract ──────────────────────────────


def test_record_overdue_appends_json_line(tmp_path):
    assert record_overdue(tmp_path, kind="delivery_failures",
                          detail="3 consecutive send failures",
                          due_since="2026-07-07 08:00")
    rows = _deadletter_rows(tmp_path)
    assert len(rows) == 1
    assert set(rows[0]) == {"ts", "kind", "detail", "due_since"}
    assert rows[0]["kind"] == "delivery_failures"
    assert rows[0]["due_since"] == "2026-07-07 08:00"


def test_record_overdue_holds_flock_around_write(tmp_path, monkeypatch):
    calls = []
    real_flock = fcntl.flock
    monkeypatch.setattr(ddl.fcntl, "flock",
                        lambda fd, op: calls.append(op) or real_flock(fd, op))
    record_overdue(tmp_path, kind="k", detail="d", due_since="s")
    assert calls[0] == fcntl.LOCK_EX  # taken before the write
    assert calls[-1] == fcntl.LOCK_UN  # released after


def test_deadletter_contract_docstring():
    # The daemon-side consumer is wired separately against this contract —
    # it must survive in the module docstring, not tribal knowledge.
    doc = ddl.__doc__
    assert "flock" in doc
    assert "PRODUCER" in doc and "CONSUMER" in doc
    assert "truncate" in doc and "unlink" in doc


def test_deadletter_producer_side_cap(tmp_path):
    p = tmp_path / DEADLETTER_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("".join(json.dumps({"kind": "old", "i": i}) + "\n"
                         for i in range(ddl.DEADLETTER_MAX_LINES)))
    record_overdue(tmp_path, kind="new", detail="d", due_since="s")
    rows = _deadletter_rows(tmp_path)
    assert len(rows) == ddl.DEADLETTER_MAX_LINES // 2 + 1
    assert rows[-1]["kind"] == "new"


def test_note_delivery_writes_deadletter_at_threshold(tmp_path, monkeypatch):
    monkeypatch.setattr(hbl, "_lark_send_text", lambda t, u: True)
    t0 = time.time()
    for i in range(DELIVERY_ALERT_THRESHOLD):
        _note_delivery(tmp_path, ok=False, user_id="ou_test", now=t0 + i)
    dl = _deadletter_rows(tmp_path)
    assert len(dl) == 1  # once per alert window, not per failure
    assert dl[0]["kind"] == "delivery_failures"
    assert str(DELIVERY_ALERT_THRESHOLD) in dl[0]["detail"]


def test_note_delivery_success_clears_streak_start(tmp_path, monkeypatch):
    monkeypatch.setattr(hbl, "_lark_send_text", lambda t, u: True)
    _note_delivery(tmp_path, ok=False, user_id="u")
    _note_delivery(tmp_path, ok=True, user_id="u")
    st = json.loads((tmp_path / hbl.DELIVERY_STATE_FILE).read_text())
    assert st["consec_fails"] == 0
    assert "first_fail" not in st
