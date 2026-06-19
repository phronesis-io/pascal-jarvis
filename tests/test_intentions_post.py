"""Tests for tasks/intentions_post.py — output quality gate.

Regression coverage for the "🎯 Intent / sent" leak: internal "prompt"-type
intents reporting a bare status word, and malformed JSON envelopes, must never
reach the user as a card.
"""

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
_spec = importlib.util.spec_from_file_location(
    "intentions_post", ROOT / "tasks" / "intentions_post.py"
)
ip = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ip)


@pytest.mark.parametrize("token", [
    "sent", "Sent", "SENT", "done.", "ok", "OK!", "hello", "noted",
    "completed", "已发送", "完成", "收到", "  done  ", "",
])
def test_contentless_status_tokens_filtered(token):
    assert ip._is_contentless(token) is True


@pytest.mark.parametrize("real", [
    "14:00 鱼刺 11，议题：反馈采集",
    "21:00 康复训练时间，今日四件套",
    "明天 10:00 例行咨询，可以带的素材：身体警讯",
    "sent the message to 凌安",   # multi-word: real content, not a bare token
])
def test_real_messages_kept(real):
    assert ip._is_contentless(real) is False


def test_apply_action_skips_status_word(monkeypatch):
    """A notify intent whose response is just 'sent' produces no user message."""
    monkeypatch.setattr(ip, "mark_executed", lambda *a, **k: None)
    monkeypatch.setattr(ip, "mark_failed", lambda *a, **k: None)
    msgs = []
    ip._apply_action("int_x", response="sent", action="notify", user_messages=msgs)
    assert msgs == []


def test_apply_action_keeps_real_message(monkeypatch):
    monkeypatch.setattr(ip, "mark_executed", lambda *a, **k: None)
    msgs = []
    ip._apply_action("int_x", response="14:00 开会，记得带电脑",
                     action="notify", user_messages=msgs)
    assert msgs == ["14:00 开会，记得带电脑"]


def test_apply_action_records_closure_and_does_not_card(monkeypatch):
    """A follow-up with a closure sub-object records onto the parent and never
    surfaces a card — even with action=notify (recording is internal)."""
    monkeypatch.setattr(ip, "mark_executed", lambda *a, **k: None)
    calls = []
    monkeypatch.setattr(ip, "record_closure",
                        lambda parent, outcome="done", result="", via="cli":
                        calls.append((parent, outcome, result)))
    msgs = []
    ip._apply_action("int_fu", response="约了周四下午", action="notify",
                     user_messages=msgs,
                     closure={"parent": "int_parent", "outcome": "done", "result": "约了周四下午"})
    assert calls == [("int_parent", "done", "约了周四下午")]
    assert msgs == []   # closure row never cards


def test_apply_action_no_closure_still_cards(monkeypatch):
    """A follow-up still ASKING (no closure field) cards its question normally."""
    monkeypatch.setattr(ip, "mark_executed", lambda *a, **k: None)
    monkeypatch.setattr(ip, "record_closure", lambda *a, **k: None)
    msgs = []
    ip._apply_action("int_fu", response="你之前说今天约学妹，约上了吗？",
                     action="notify", user_messages=msgs)
    assert msgs == ["你之前说今天约学妹，约上了吗？"]


def _isolate_state(monkeypatch):
    """main() touches the inflight manifest / breach queue — never the real
    ones from a test."""
    monkeypatch.setattr(ip, "read_inflight", lambda: [])
    monkeypatch.setattr(ip, "reconcile_inflight",
                        lambda covered: {"retried": [], "expired": [], "breached": []})
    monkeypatch.setattr(ip, "read_inflight_breaches", lambda: [])
    monkeypatch.setattr(ip, "mark_breaches_shown", lambda ids: None)
    monkeypatch.setattr(ip, "_ledger_append", lambda ids, card_roots=None: None)
    monkeypatch.setattr(ip, "note_closure_touch", lambda *a, **k: None)


def test_malformed_intents_envelope_not_emitted(monkeypatch, capsys):
    """The 09:00 leak: {"intents": {"id": , ...}} is invalid JSON and must
    not be carded as raw text."""
    _isolate_state(monkeypatch)
    monkeypatch.setattr("sys.stdin", _Stdin(
        '{"intents": {"int_c460a594e7": , "int_c83a783ac8": }}'))
    ip.main()
    out = capsys.readouterr().out
    assert '"intents"' not in out
    assert "int_c460a594e7" not in out


def test_no_envelope_sentinel_reconciles_everything(monkeypatch, capsys):
    """REQ-30: '__NO_ENVELOPE__' (runner saw HEARTBEAT_OK / empty / killed /
    parse failure) reconciles the manifest with zero covered ids and emits
    nothing user-facing."""
    calls = []
    monkeypatch.setattr(ip, "reconcile_inflight",
                        lambda covered: (calls.append(covered),
                                         {"retried": ["int_b"], "expired": [], "breached": []})[1])
    monkeypatch.setattr(ip, "read_inflight", lambda: ["int_b"])
    monkeypatch.setattr("sys.stdin", _Stdin("__NO_ENVELOPE__"))
    ip.main()
    assert calls == [[]]                       # nothing covered
    assert capsys.readouterr().out == ""       # nothing user-facing


def test_envelope_reconciles_covered_ids(monkeypatch, capsys):
    """A parsed envelope reconciles with exactly the covered ids, so the
    uncovered remainder gets the retry policy."""
    recon_calls = []
    monkeypatch.setattr(ip, "read_inflight", lambda: ["int_a", "int_b"])
    monkeypatch.setattr(ip, "reconcile_inflight",
                        lambda covered: (recon_calls.append(sorted(covered)),
                                         {"retried": [], "expired": [], "breached": []})[1])
    monkeypatch.setattr(ip, "read_inflight_breaches", lambda: [])
    monkeypatch.setattr(ip, "mark_breaches_shown", lambda ids: None)
    monkeypatch.setattr(ip, "_ledger_append", lambda ids, card_roots=None: None)
    monkeypatch.setattr(ip, "mark_executed", lambda *a, **k: None)
    monkeypatch.setattr(ip, "get_intent", lambda iid: {"parent_intent_id": None})
    monkeypatch.setattr("sys.stdin", _Stdin(
        '{"intents": {"int_a": {"response": "记得 14:00 开会", "action": "notify"}}}'))
    ip.main()
    assert recon_calls == [["int_a"]]


def test_asking_followup_card_carries_closure_buttons(monkeypatch, capsys):
    """REQ-34: a follow-up that is ASKING gets one-tap ✅/❌/🚫 buttons whose
    value routes intent_close to the sidecar."""
    import json
    monkeypatch.setattr(ip, "read_inflight", lambda: ["int_fu"])
    monkeypatch.setattr(ip, "reconcile_inflight",
                        lambda covered: {"retried": [], "expired": [], "breached": []})
    monkeypatch.setattr(ip, "read_inflight_breaches", lambda: [])
    monkeypatch.setattr(ip, "mark_breaches_shown", lambda ids: None)
    monkeypatch.setattr(ip, "_ledger_append", lambda ids, card_roots=None: None)
    monkeypatch.setattr(ip, "note_closure_touch", lambda *a, **k: None)
    monkeypatch.setattr(ip, "mark_executed", lambda *a, **k: None)
    monkeypatch.setattr(ip, "get_intent",
                        lambda iid: {"parent_intent_id": "int_parent", "name": "闭环: 约学妹"})
    monkeypatch.setattr("sys.stdin", _Stdin(
        '{"intents": {"int_fu": {"response": "昨天的饭局，有值得跟进的吗？", "action": "notify"}}}'))
    ip.main()
    out = capsys.readouterr().out.strip()
    card = json.loads(out)
    blob = json.dumps(card, ensure_ascii=False)
    assert "intent_close" in blob
    assert "int_parent" in blob


def test_rendered_closure_card_records_touch(monkeypatch, capsys):
    """A closure ask consumes touch budget only once the card is rendered."""
    calls = []
    monkeypatch.setattr(ip, "read_inflight", lambda: ["int_fu"])
    monkeypatch.setattr(ip, "read_inflight_breaches", lambda: [])
    monkeypatch.setattr(ip, "reconcile_inflight",
                        lambda covered: {"retried": [], "expired": [], "breached": []})
    monkeypatch.setattr(ip, "mark_breaches_shown", lambda ids: None)
    monkeypatch.setattr(ip, "_ledger_append", lambda ids, card_roots=None: None)
    monkeypatch.setattr(ip, "mark_executed", lambda *a, **k: None)
    monkeypatch.setattr(ip, "get_intent",
                        lambda iid: {"parent_intent_id": "int_parent", "name": "闭环再问"})
    monkeypatch.setattr(ip, "note_closure_touch",
                        lambda parent, via="card": calls.append((parent, via)))
    monkeypatch.setattr("sys.stdin", _Stdin(
        '{"intents": {"int_fu": {"response": "现在真的跑起来了吗？", "action": "notify"}}}'))
    ip.main()
    assert "现在真的跑起来了吗" in capsys.readouterr().out
    assert calls == [("int_parent", "card")]


def test_duplicate_closure_card_does_not_record_touch(tmp_path, monkeypatch, capsys):
    """Duplicate-suppressed closure cards do not burn proactive touch budget."""
    import json, datetime
    calls = []
    ledger = tmp_path / ".intent_card_ledger.jsonl"
    fresh = datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    ledger.write_text(json.dumps(
        {"ts": fresh, "intent_ids": ["int_parent__fu"], "card_roots": ["int_parent"], "message_ids": []}) + "\n")
    monkeypatch.setattr(ip, "CARD_LEDGER", ledger)
    monkeypatch.setattr(ip, "read_inflight", lambda: ["int_parent__reask1"])
    monkeypatch.setattr(ip, "read_inflight_breaches", lambda: [])
    monkeypatch.setattr(ip, "reconcile_inflight",
                        lambda covered: {"retried": [], "expired": [], "breached": []})
    monkeypatch.setattr(ip, "mark_executed", lambda *a, **k: None)
    monkeypatch.setattr(ip, "get_intent",
                        lambda iid: {"parent_intent_id": "int_parent", "name": "闭环再问"})
    monkeypatch.setattr(ip, "note_closure_touch",
                        lambda parent, via="card": calls.append((parent, via)))
    monkeypatch.setattr("sys.stdin", _Stdin(
        '{"intents": {"int_parent__reask1": {"response": "现在真的跑起来了吗？", "action": "notify"}}}'))
    ip.main()
    assert capsys.readouterr().out.strip() == ""
    assert calls == []


class _Stdin:
    def __init__(self, text):
        self._text = text

    def read(self):
        return self._text


# ── REQ-59: outbox semantic dedup by root intent ─────────────────────────

def test_root_id_strips_followup_suffix():
    assert ip._root_id("int_023339f780__fu") == "int_023339f780"
    assert ip._root_id("int_abc") == "int_abc"
    assert ip._root_id("") == ""


def test_recent_card_roots_reads_ledger(tmp_path, monkeypatch):
    import json, datetime
    ledger = tmp_path / ".intent_card_ledger.jsonl"
    now = datetime.datetime.now()
    fresh = now.strftime("%Y-%m-%dT%H:%M:%S")
    old = (now - datetime.timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S")
    ledger.write_text(
        json.dumps({"ts": fresh, "intent_ids": ["int_aaa__fu"], "card_roots": ["int_aaa"], "message_ids": []}) + "\n" +
        json.dumps({"ts": old, "intent_ids": ["int_bbb"], "card_roots": ["int_bbb"], "message_ids": []}) + "\n")
    monkeypatch.setattr(ip, "CARD_LEDGER", ledger)
    roots = ip._recent_card_roots(within_min=30)
    assert "int_aaa" in roots          # fresh card_root → in window
    assert "int_bbb" not in roots      # 2h old → outside window


def test_duplicate_card_for_same_root_suppressed(tmp_path, monkeypatch, capsys):
    """The triple-nag fix: a second card whose only root was already carded
    within the window is suppressed (REQ-59)."""
    import json, datetime
    ledger = tmp_path / ".intent_card_ledger.jsonl"
    fresh = datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    ledger.write_text(json.dumps(
        {"ts": fresh, "intent_ids": ["int_023339f780__fu"], "card_roots": ["int_023339f780"], "message_ids": []}) + "\n")
    monkeypatch.setattr(ip, "CARD_LEDGER", ledger)
    monkeypatch.setattr(ip, "read_inflight", lambda: ["int_023339f780__fu"])
    monkeypatch.setattr(ip, "read_inflight_breaches", lambda: [])
    monkeypatch.setattr(ip, "reconcile_inflight",
                        lambda covered: {"retried": [], "expired": [], "breached": []})
    monkeypatch.setattr(ip, "mark_executed", lambda *a, **k: None)
    monkeypatch.setattr(ip, "get_intent",
                        lambda iid: {"parent_intent_id": "int_023339f780", "name": "闭环"})
    monkeypatch.setattr("sys.stdin", _Stdin(
        '{"intents": {"int_023339f780__fu": {"response": "饭后有什么跟进的吗？", "action": "notify"}}}'))
    ip.main()
    out = capsys.readouterr().out
    assert out.strip() == ""           # card suppressed (same root within window)


def test_dedup_only_keys_on_button_roots_not_silent_slots(tmp_path, monkeypatch, capsys):
    """Red-team P1-A: a card that covered an intent only as a silent/no-button
    slot must NOT register that root in the dedup ledger — otherwise that
    intent's own later genuine notify gets suppressed. Only closure-ask
    (button) roots enter card_roots."""
    import json, datetime
    ledger = tmp_path / ".intent_card_ledger.jsonl"
    monkeypatch.setattr(ip, "CARD_LEDGER", ledger)
    monkeypatch.setattr(ip, "read_inflight", lambda: ["int_B"])
    monkeypatch.setattr(ip, "read_inflight_breaches", lambda: [])
    monkeypatch.setattr(ip, "reconcile_inflight",
                        lambda covered: {"retried": [], "expired": [], "breached": []})
    monkeypatch.setattr(ip, "mark_executed", lambda *a, **k: None)
    # int_B is a plain notify (no parent_intent_id → no buttons)
    monkeypatch.setattr(ip, "get_intent", lambda iid: {"parent_intent_id": None})
    monkeypatch.setattr("sys.stdin", _Stdin(
        '{"intents": {"int_B": {"response": "你的快递到了", "action": "notify"}}}'))
    ip.main()
    out = capsys.readouterr().out
    assert "快递" in out                       # the genuine notify went out
    # ledger row for this card must carry EMPTY card_roots (no button/closure)
    row = json.loads(ledger.read_text().splitlines()[-1])
    assert row["card_roots"] == []            # int_B not registered as a nag root


# ── E2E: main() → reconcile_inflight through a real DB ────────────────────

def test_main_reconcile_inflight_e2e(tmp_path, monkeypatch, capsys):
    """End-to-end: envelope covers one intent, leaves another uncovered.

    Exercises the REAL reconcile_inflight + mark_executed through an actual
    SQLite DB — no mocking of the core.intentions data path.
    """
    import json
    import sqlite3
    import core.intentions as mod

    # ── Wire a real tmp SQLite ──
    db_path = tmp_path / "data" / "jarvis.db"
    db_path.parent.mkdir(parents=True)

    def _test_get_db():
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    monkeypatch.setattr(mod, "_get_db", _test_get_db)
    monkeypatch.setattr(mod, "_table_ready", False)
    mod._init()

    # ── Wire inflight / breach / sched files to tmp ──
    inflight = tmp_path / "data" / ".intention_inflight.json"
    breach_q = tmp_path / "data" / ".intent_breach_queue.jsonl"
    ledger = tmp_path / "data" / ".intent_card_ledger.jsonl"
    monkeypatch.setattr(mod, "INFLIGHT_FILE", inflight)
    monkeypatch.setattr(mod, "BREACH_QUEUE", breach_q)
    monkeypatch.setattr(ip, "CARD_LEDGER", ledger)
    # Silence sched_events (no real sched_events.jsonl needed)
    monkeypatch.setattr(mod, "_emit_intent", lambda *a, **k: None)

    # ── Create two intents, mark both triggered ──
    from core.intentions import create_intent, mark_triggered, get_intent

    id_a = create_intent(
        name="covered intent",
        trigger_type="date",
        trigger_config={"datetime": "2026-06-19T09:00:00"},
        prompt="remind me to drink water",
        intent_id="int_e2e_a",
    )
    id_b = create_intent(
        name="uncovered intent",
        trigger_type="cron",
        trigger_config={"expression": "0 * * * *"},
        prompt="hourly check",
        intent_id="int_e2e_b",
    )
    mark_triggered(id_a)
    mark_triggered(id_b)

    # Sanity: both should be 'triggered'
    assert get_intent(id_a)["status"] == "triggered"
    assert get_intent(id_b)["status"] == "triggered"

    # Write the inflight manifest (normally done by intentions_pre.sh)
    mod.write_inflight([id_a, id_b])

    # ── Feed envelope covering ONLY int_a ──
    envelope = json.dumps({
        "intents": {
            id_a: {"response": "记得喝水", "action": "notify"},
        }
    })
    monkeypatch.setattr("sys.stdin", _Stdin(envelope))
    ip.main()

    # ── Verify via real DB ──
    a = get_intent(id_a)
    b = get_intent(id_b)

    # Covered intent → executed
    assert a["status"] == "executed"

    # Uncovered cron intent → retried back to pending (cron always retries)
    assert b["status"] == "pending"
    assert "retry" in (b.get("last_error") or "").lower()

    # Inflight manifest cleared
    assert not inflight.exists()

    # Card was emitted for the covered intent
    out = capsys.readouterr().out
    assert "喝水" in out


def test_main_no_envelope_reconcile_e2e(tmp_path, monkeypatch, capsys):
    """E2E no-envelope path: all inflight intents get the retry policy."""
    import json
    import sqlite3
    import core.intentions as mod

    db_path = tmp_path / "data" / "jarvis.db"
    db_path.parent.mkdir(parents=True)

    def _test_get_db():
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    monkeypatch.setattr(mod, "_get_db", _test_get_db)
    monkeypatch.setattr(mod, "_table_ready", False)
    mod._init()

    inflight = tmp_path / "data" / ".intention_inflight.json"
    monkeypatch.setattr(mod, "INFLIGHT_FILE", inflight)
    monkeypatch.setattr(mod, "BREACH_QUEUE", tmp_path / "data" / ".breach.jsonl")
    monkeypatch.setattr(mod, "_emit_intent", lambda *a, **k: None)

    from core.intentions import create_intent, mark_triggered, get_intent

    iid = create_intent(
        name="lost intent",
        trigger_type="cron",
        trigger_config={"expression": "0 9 * * *"},
        prompt="morning check",
        intent_id="int_e2e_lost",
    )
    mark_triggered(iid)
    mod.write_inflight([iid])

    monkeypatch.setattr("sys.stdin", _Stdin("__NO_ENVELOPE__"))
    ip.main()

    it = get_intent(iid)
    assert it["status"] == "pending"
    assert "envelope missing" in (it.get("last_error") or "")
    assert not inflight.exists()
    assert capsys.readouterr().out == ""
