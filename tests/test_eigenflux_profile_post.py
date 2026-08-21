"""Behavioral contract for the eigenflux-profile heartbeat post-hook.

Until 2026-08-21 the only executable reference to eigenflux-profile lived in
the retired dashboard's network-overview page test — a health *rendering* of
the task, not the task. This suite covers the hook itself: parse the model
verdict, mutate the EigenFlux profile only on an explicit should_update with
real fields, and never crash the heartbeat.
"""

import io
import subprocess

import pytest

from tasks import eigenflux_profile_post as hook


@pytest.fixture
def run_recorder(monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(hook.subprocess, "run", fake_run)
    return calls


def _feed(monkeypatch, raw: str) -> None:
    monkeypatch.setattr(hook.sys, "stdin", io.StringIO(raw))


def test_heartbeat_ok_and_empty_input_are_silent_noops(
        monkeypatch, run_recorder, capsys):
    for raw in ("", "HEARTBEAT_OK", "prefix HEARTBEAT_OK suffix"):
        _feed(monkeypatch, raw)
        assert hook.main() == 0
    assert run_recorder == []
    assert capsys.readouterr().out == ""


def test_unparseable_json_never_fails_the_heartbeat(
        monkeypatch, run_recorder):
    _feed(monkeypatch, "not json at all {")
    assert hook.main() == 0
    assert run_recorder == []


def test_should_update_with_fields_calls_the_cli_and_reports(
        monkeypatch, run_recorder, capsys):
    _feed(monkeypatch, '{"should_update": true, "agent_name": "Jarvis", '
                       '"bio": "personal agent", "reason": "stale bio"}')
    assert hook.main() == 0
    assert run_recorder == [[
        "eigenflux", "profile", "update", "-f", "json",
        "--name", "Jarvis", "--bio", "personal agent",
    ]]
    assert "EigenFlux profile updated. stale bio" in capsys.readouterr().out


def test_should_update_false_or_blank_fields_never_touch_the_cli(
        monkeypatch, run_recorder):
    _feed(monkeypatch, '{"should_update": false, "agent_name": "X"}')
    assert hook.main() == 0
    _feed(monkeypatch, '{"should_update": true, "agent_name": "  ", "bio": ""}')
    assert hook.main() == 0
    assert run_recorder == []


def test_non_string_model_fields_are_coerced_for_subprocess(
        monkeypatch, run_recorder):
    _feed(monkeypatch, '{"should_update": true, "agent_name": 123}')
    assert hook.main() == 0
    assert run_recorder == [[
        "eigenflux", "profile", "update", "-f", "json", "--name", "123",
    ]]
    for arg in run_recorder[0]:
        assert isinstance(arg, str)
