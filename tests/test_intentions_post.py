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
                        lambda covered: {"retried": [], "expired": []})
    monkeypatch.setattr(ip, "clear_breaches", lambda ids=None: None)
    monkeypatch.setattr(ip, "_ledger_append", lambda ids: None)


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
                                         {"retried": ["int_b"], "expired": []})[1])
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
                                         {"retried": [], "expired": []})[1])
    monkeypatch.setattr(ip, "clear_breaches", lambda ids=None: None)
    monkeypatch.setattr(ip, "_ledger_append", lambda ids: None)
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
                        lambda covered: {"retried": [], "expired": []})
    monkeypatch.setattr(ip, "clear_breaches", lambda ids=None: None)
    monkeypatch.setattr(ip, "_ledger_append", lambda ids: None)
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


class _Stdin:
    def __init__(self, text):
        self._text = text

    def read(self):
        return self._text
