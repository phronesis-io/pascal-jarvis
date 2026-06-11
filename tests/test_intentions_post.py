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
                        lambda parent, outcome="done", result="": calls.append((parent, outcome, result)))
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


def test_malformed_intents_envelope_not_emitted(monkeypatch, capsys):
    """The 09:00 leak: {"intents": {"id": , ...}} is invalid JSON and must
    not be carded as raw text."""
    monkeypatch.setattr("sys.stdin", _Stdin(
        '{"intents": {"int_c460a594e7": , "int_c83a783ac8": }}'))
    ip.main()
    out = capsys.readouterr().out
    assert '"intents"' not in out
    assert "int_c460a594e7" not in out


class _Stdin:
    def __init__(self, text):
        self._text = text

    def read(self):
        return self._text
