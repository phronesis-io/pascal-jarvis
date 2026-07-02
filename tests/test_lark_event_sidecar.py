"""Tests for Lark card callback sidecar helpers."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).parent.parent
_spec = importlib.util.spec_from_file_location(
    "lark_event_sidecar", ROOT / "scripts" / "lark_event_sidecar.py"
)
sidecar = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sidecar)


def test_intent_close_payload_records_via_button_and_returns_card(monkeypatch):
    calls = []

    def fake_record_closure(parent_id, outcome="done", result="", via="cli"):
        calls.append((parent_id, outcome, result, via))
        return True

    import core.intentions as intentions
    monkeypatch.setattr(intentions, "record_closure", fake_record_closure)

    payload = sidecar._intent_close_payload({
        "id": " int_parent ",
        "outcome": "recorded",
        "result": "没做（按钮记录）",
    })

    assert calls == [("int_parent", "recorded", "没做（按钮记录）", "button")]
    assert payload["toast"]["type"] == "success"
    assert payload["card"]["type"] == "raw"
    card = payload["card"]["data"]
    assert card["header"]["title"]["content"] == "闭环已记录"
    assert "已记录为没做/改天" in card["elements"][0]["text"]["content"]
    assert "没做（按钮记录）" in card["elements"][0]["text"]["content"]


def test_app_id_reads_lark_cli_config_when_env_missing(monkeypatch):
    monkeypatch.delenv("LARK_APP_ID", raising=False)

    def fake_run(args, capture_output=True, text=True, timeout=10):
        assert args == ["lark-cli", "config", "show"]
        return SimpleNamespace(stdout=json.dumps({"appId": "cli_app_id"}))

    monkeypatch.setattr(sidecar.subprocess, "run", fake_run)

    assert sidecar._app_id() == "cli_app_id"


def test_intent_close_payload_returns_persistent_noop_card(monkeypatch):
    import core.intentions as intentions
    monkeypatch.setattr(intentions, "record_closure", lambda *a, **k: False)

    payload = sidecar._intent_close_payload({"id": "int_closed", "outcome": "done"})

    assert payload["toast"]["type"] == "info"
    assert payload["card"]["type"] == "raw"
    card_text = payload["card"]["data"]["elements"][0]["text"]["content"]
    assert "已经闭环过了" in payload["toast"]["content"]
    assert "已经闭环过了" in card_text


# ──────────────────────────────────────────────────────────────────────────
# REQ-81.3 — success paths must leave a stderr trace (card taps previously
# logged nothing on success, so "点选无反应" reports couldn't be disproven).
# ──────────────────────────────────────────────────────────────────────────

def test_handle_card_feedback_logs_success(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sidecar, "JARVIS_DIR", tmp_path)

    payload = sidecar._handle_card_action(
        {"action": "feedback", "source": "daily-reflect", "rating": "up"})

    assert payload["toast"]["type"] == "success"
    assert "card feedback recorded" in capsys.readouterr().err
    # the actual write happened too — log must not replace the side effect
    logged = (tmp_path / "engagement_log.jsonl").read_text(encoding="utf-8")
    assert '"rating": "up"' in logged


def test_handle_card_watchlater_logs_success(monkeypatch, capsys):
    monkeypatch.setattr(
        sidecar.subprocess, "run",
        lambda *a, **k: SimpleNamespace(stdout="已收藏", stderr=""))

    payload = sidecar._handle_card_action(
        {"action": "watchlater", "title": "T", "url": "https://example.com/v"})

    assert payload["toast"]["type"] == "success"
    assert "card watchlater saved" in capsys.readouterr().err


def test_handle_card_intent_close_logs_success(monkeypatch, capsys):
    import core.intentions as intentions
    monkeypatch.setattr(intentions, "record_closure", lambda *a, **k: True)

    payload = sidecar._handle_card_action(
        {"action": "intent_close", "id": "int_x", "outcome": "done"})

    assert payload["toast"]["type"] == "success"
    assert "card intent_close handled" in capsys.readouterr().err


def test_handle_card_unknown_action_returns_empty(capsys):
    assert sidecar._handle_card_action({"action": "nonsense"}) == {}
