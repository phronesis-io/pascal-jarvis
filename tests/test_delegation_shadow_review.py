"""Human review path for Phase-0 shadow labels (2026-07-27).

Phase-0 promotion is gated on reviewed production labels. The code to record a
label existed, but nothing could show a reviewer WHAT to label: shadow rows
deliberately store only a stable reference, never the message body. The result
was 20 captured candidates and 0 reviewed labels — the gate blocked on the one
step that had no interface.

These tests pin the review sheet: oldest-first (so the observation window can
actually widen), text resolved read-only at review time and never persisted,
graceful degradation to the bare reference, and a gate readout that names what
is still missing instead of printing a bare boolean.
"""

import sqlite3

from core.delegation_shadow import (render_gate, render_queue,
                                    resolve_source_text)
from core.delegations import DelegationStore


def _store(path, clock=None):
    clock = clock or [1_000.0]
    return DelegationStore(root=path, db_path=path / "jarvis.db",
                           now=lambda: clock[0])


def _audit_db(root, rows):
    (root / "data").mkdir(exist_ok=True)
    conn = sqlite3.connect(root / "data" / "conversation_audit.db")
    conn.execute(
        "CREATE TABLE conversation_events "
        "(id INTEGER PRIMARY KEY, message_id TEXT, content TEXT)")
    conn.executemany(
        "INSERT INTO conversation_events (message_id, content) VALUES (?,?)",
        rows)
    conn.commit()
    conn.close()


def _capture(store, *, ref, operation, is_delegation=False, created_at=None):
    row, _ = store.record_shadow_prediction(
        principal_id="owner",
        source="lark",
        source_ref=ref,
        title="shadow",
        operation=operation,
        predicted_is_delegation=is_delegation,
        predicted_target_risk=2 if is_delegation else 0,
        predicted_verifier="lark_message" if is_delegation else "",
    )
    return row["id"]


# ── The queue ────────────────────────────────────────────────────────


def test_unlabeled_queue_is_oldest_first(tmp_path):
    """Reviewing only new arrivals would keep observation_days near zero
    forever, no matter how many labels accumulate."""
    clock = [1_000.0]
    store = _store(tmp_path, clock)
    first = _capture(store, ref="om_old", operation="discussion")
    clock[0] += 7 * 86400
    second = _capture(store, ref="om_new", operation="message_send",
                      is_delegation=True)

    ids = [row["id"] for row in store.unlabeled_shadow(limit=10)]
    assert ids == [first, second]

    store.label_shadow(first, actual_is_delegation=False,
                       actual_target_risk=0, actual_verifier="")
    assert [r["id"] for r in store.unlabeled_shadow(limit=10)] == [second]


def test_queue_shows_the_real_message_and_the_prediction(tmp_path):
    store = _store(tmp_path)
    _capture(store, ref="om_1", operation="message_send", is_delegation=True)
    _audit_db(tmp_path, [("om_1", "帮我把这份文档发给张三")])

    sheet = render_queue(store.unlabeled_shadow(limit=10), root=tmp_path)

    assert "帮我把这份文档发给张三" in sheet
    assert "替你发消息" in sheet          # prediction in plain language
    assert "lark_message" in sheet        # and the verifier it would use
    assert "--is-delegation" in sheet     # and how to disagree


def test_unresolvable_reference_still_reviewable(tmp_path):
    """A missing audit row must degrade to the reference, not hide the row."""
    store = _store(tmp_path)
    _capture(store, ref="om_missing", operation="discussion")

    sheet = render_queue(store.unlabeled_shadow(limit=10), root=tmp_path)

    assert "om_missing" in sheet
    assert "无法解析" in sheet


def test_empty_queue_says_so(tmp_path):
    assert "空的" in render_queue([], root=tmp_path)


# ── Privacy: resolution is read-only and never persisted ─────────────


def test_resolution_never_writes_to_the_audit_store(tmp_path):
    _audit_db(tmp_path, [("om_1", "私人内容")])
    db = tmp_path / "data" / "conversation_audit.db"
    before = db.read_bytes()

    assert resolve_source_text("om_1", root=tmp_path) == "私人内容"

    assert db.read_bytes() == before


def test_resolution_is_silent_when_there_is_no_audit_store(tmp_path):
    assert resolve_source_text("om_1", root=tmp_path) == ""


def test_message_body_stays_out_of_the_control_plane(tmp_path):
    """The whole point of the reference design: resolving for review must not
    leak the body into jarvis.db."""
    store = _store(tmp_path)
    _capture(store, ref="om_1", operation="message_send", is_delegation=True)
    _audit_db(tmp_path, [("om_1", "私人内容不得入库")])

    render_queue(store.unlabeled_shadow(limit=10), root=tmp_path)

    blob = (tmp_path / "jarvis.db").read_bytes()
    assert b"\xe7\xa7\x81\xe4\xba\xba\xe5\x86\x85\xe5\xae\xb9" not in blob


# ── The gate readout ─────────────────────────────────────────────────


def test_gate_names_every_missing_dimension(tmp_path):
    store = _store(tmp_path)
    for index in range(3):
        _capture(store, ref=f"om_{index}", operation="discussion")

    text = render_gate(store.shadow_metrics())

    assert "待复核 3" in text
    assert "0/50" in text
    assert "14 天" in text
    assert "连接器类 0/5" in text


def test_gate_reports_ready_without_deciding(tmp_path):
    """Meeting the thresholds is evidence, not permission — the readout must
    hand the decision to Pascal rather than announce promotion."""
    text = render_gate({
        "predictions": 60, "labeled": 60, "observation_days": 20,
        "connector_classes": ["a", "b", "c", "d", "e"],
        "connector_class_count": 5, "precision": 1.0,
        "high_risk_recall": 1.0, "verifier_accuracy": 1.0,
        "phase1_ready": True,
    })
    assert "由用户决定" in text
