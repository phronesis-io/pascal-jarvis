"""Tests for Lark card callback sidecar helpers."""

import importlib.util
import json
import logging
import sys
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
        {"action": "feedback", "source": "daily-reflect", "rating": "up"},
        owner_authenticated=True)

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
        {"action": "watchlater", "title": "T", "url": "https://example.com/v"},
        owner_authenticated=True)

    assert payload["toast"]["type"] == "success"
    assert "card watchlater saved" in capsys.readouterr().err


def test_handle_card_intent_close_logs_success(monkeypatch, capsys):
    import core.intentions as intentions
    monkeypatch.setattr(intentions, "record_closure", lambda *a, **k: True)

    payload = sidecar._handle_card_action(
        {"action": "intent_close", "id": "int_x", "outcome": "done"},
        owner_authenticated=True)

    assert payload["toast"]["type"] == "success"
    assert "card intent_close handled" in capsys.readouterr().err


def test_handle_card_unknown_action_returns_empty(capsys):
    assert sidecar._handle_card_action({"action": "nonsense"}) == {}


# ──────────────────────────────────────────────────────────────────────────
# Memorial (奏折) batch route — one generic branch dispatches every memorial
# card button to core.memorial.decide/chat. Legacy actions must not regress
# (covered by the tests above running against the same _handle_card_action).
# ──────────────────────────────────────────────────────────────────────────

def test_handle_card_memorial_decide_routes_and_logs(monkeypatch, capsys):
    import core.memorial as memorial
    sentinel = {"toast": {"type": "success", "content": "已批：已阅 ✓"}}
    calls = []

    def fake_decide(mid, opt, *, owner_authenticated=False):
        assert owner_authenticated is True
        calls.append((mid, opt))
        return sentinel

    monkeypatch.setattr(memorial, "decide", fake_decide)
    monkeypatch.setattr(memorial, "chat",
                        lambda mid: (_ for _ in ()).throw(AssertionError("chat called")))

    payload = sidecar._handle_card_action(
        {"action": "memorial", "id": "mem_1", "opt": "read"},
        owner_authenticated=True)

    assert payload is sentinel
    assert calls == [("mem_1", "read")]
    assert "card memorial handled: id=mem_1 opt=read" in capsys.readouterr().err


def test_handle_card_memorial_chat_routes_to_chat(monkeypatch, capsys):
    import core.memorial as memorial
    sentinel = {"toast": {"type": "success", "content": "开聊"}}
    calls = []

    def fake_chat(mid):
        calls.append(mid)
        return sentinel

    monkeypatch.setattr(memorial, "chat", fake_chat)
    monkeypatch.setattr(
        memorial, "decide",
        lambda *a: (_ for _ in ()).throw(AssertionError("decide called")))

    payload = sidecar._handle_card_action(
        {"action": "memorial", "id": "mem_2", "opt": "chat"},
        owner_authenticated=True)

    assert payload is sentinel
    assert calls == ["mem_2"]
    assert "card memorial handled: id=mem_2 opt=chat" in capsys.readouterr().err


def test_handle_card_memorial_failure_returns_info_toast(monkeypatch, capsys):
    import core.memorial as memorial
    monkeypatch.setattr(
        memorial, "decide",
        lambda *a, **k: (
            _ for _ in ()
        ).throw(RuntimeError("ledger on fire")))

    payload = sidecar._handle_card_action(
        {"action": "memorial", "id": "mem_3", "opt": "read"},
        owner_authenticated=True)

    assert payload == {"toast": {"type": "info",
                                 "content": "出错了，直接在对话里告诉我"}}
    err = capsys.readouterr().err
    assert "card memorial failed" in err and "ledger on fire" in err


def test_handle_card_action_rejects_unauthenticated_operator(capsys):
    payload = sidecar._handle_card_action(
        {"action": "memorial", "id": "mem_4", "opt": "read"}
    )

    assert payload["toast"]["type"] == "info"
    assert "只能由主人" in payload["toast"]["content"]
    assert "unauthenticated operator" in capsys.readouterr().err


def test_owner_card_callback_matches_configured_operator(monkeypatch):
    monkeypatch.setattr(sidecar, "_owner_id", lambda: "ou_owner")
    owner = SimpleNamespace(
        event=SimpleNamespace(
            operator=SimpleNamespace(
                open_id="ou_owner", user_id="", union_id=""
            )
        )
    )
    stranger = SimpleNamespace(
        event=SimpleNamespace(
            operator=SimpleNamespace(
                open_id="ou_other", user_id="", union_id=""
            )
        )
    )

    assert sidecar._owner_card_callback(owner) is True
    assert sidecar._owner_card_callback(stranger) is False


# ──────────────────────────────────────────────────────────────────────────
# Audit 2026-07-10 — disconnect watchdog: a once-connected sidecar whose link
# stays down must exit (bot.sh's 5s loop respawns it); a cold start that never
# connected must NOT exit (offline flight → SDK infinite retry, no restart
# storm).
# ──────────────────────────────────────────────────────────────────────────

class _Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_watchdog_never_fires_before_first_connect():
    clock = _Clock()
    wd = sidecar.DisconnectWatchdog(lambda: False, exit_after_s=600,
                                    clock=clock)
    for _ in range(100):
        clock.t += 300  # hours offline, never connected once
        assert wd.should_exit() is False


def test_watchdog_fires_after_sustained_disconnect():
    clock = _Clock()
    state = {"up": True}
    wd = sidecar.DisconnectWatchdog(lambda: state["up"], exit_after_s=600,
                                    clock=clock)
    assert wd.should_exit() is False  # connected → watchdog armed
    state["up"] = False
    assert wd.should_exit() is False  # first down tick starts the timer
    clock.t += 599
    assert wd.should_exit() is False  # still within SDK self-heal budget
    clock.t += 2
    assert wd.should_exit() is True   # sustained outage → die, get respawned


def test_watchdog_reconnect_resets_timer():
    clock = _Clock()
    state = {"up": True}
    wd = sidecar.DisconnectWatchdog(lambda: state["up"], exit_after_s=600,
                                    clock=clock)
    wd.should_exit()
    state["up"] = False
    wd.should_exit()  # timer starts
    clock.t += 590
    assert wd.should_exit() is False
    state["up"] = True
    assert wd.should_exit() is False  # SDK self-healed (the primary path)
    state["up"] = False
    wd.should_exit()  # timer restarts from zero
    clock.t += 599
    assert wd.should_exit() is False


def test_sdk_logs_move_to_stderr():
    # lark_oapi logs to a StreamHandler(sys.stdout) — on the sidecar that
    # stream IS the NDJSON event pipe. The redirect must leave no handler
    # writing to stdout (forensics go to stderr → LOG_FILE instead).
    sdk_logger = logging.getLogger("Lark")
    h = logging.StreamHandler(sys.stdout)
    sdk_logger.addHandler(h)
    try:
        sidecar._redirect_sdk_logs_to_stderr()
        assert all(getattr(x, "stream", None) is not sys.stdout
                   for x in sdk_logger.handlers)
        assert h.stream is sys.stderr
    finally:
        sdk_logger.removeHandler(h)
