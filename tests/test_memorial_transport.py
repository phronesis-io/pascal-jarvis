"""Behavioral contracts for the isolated memorial Lark transport."""

from __future__ import annotations

import json
import subprocess

from core import memorial_transport
from core.lark_bot_transport import BotSendResult


def test_success_extracts_nested_message_id_without_retry():
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(
            argv, 0,
            stdout=json.dumps({"data": {"message": {"message_id": "om_ok"}}}),
            stderr="",
        )

    result = memorial_transport.send(
        ["--user-id", "ou_test", "--markdown", "hello"],
        runner=run,
        sleeper=lambda _delay: None,
    )

    assert result == "om_ok"
    assert len(calls) == 1
    assert calls[0][0][:3] == ["lark-cli", "im", "+messages-send"]


def test_failures_emit_structured_events_without_provider_stderr(capsys):
    attempts = []

    def run(argv, **_kwargs):
        attempts.append(argv)
        return subprocess.CompletedProcess(
            argv, 17, stdout="", stderr="secret card body",
        )

    result = memorial_transport.send(
        ["--user-id", "ou_test", "--markdown", "private"],
        runner=run,
        sleeper=lambda _delay: None,
    )

    assert result == ""
    assert len(attempts) == 3
    events = [json.loads(line) for line in capsys.readouterr().err.splitlines()]
    assert [event["msg"] for event in events] == ["lark_send_rejected"] * 3
    assert events[-1]["attempt"] == 3
    assert events[-1]["returncode"] == 17
    assert "secret card body" not in str(events)


def test_broken_log_sink_never_aborts_retries(monkeypatch):
    attempts = []
    monkeypatch.setattr(
        memorial_transport, "log",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("log down")),
    )

    result = memorial_transport.send(
        ["--user-id", "ou_test", "--markdown", "private"],
        runner=lambda argv, **_kwargs: (
            attempts.append(argv)
            or subprocess.CompletedProcess(argv, 17, stdout="", stderr="private")
        ),
        sleeper=lambda _delay: None,
    )

    assert result == ""
    assert len(attempts) == 3


def test_bot_api_path_skips_cli_and_returns_verified_receipt(monkeypatch):
    calls = []
    monkeypatch.setattr(
        memorial_transport,
        "send_from_cli_args",
        lambda args: (
            calls.append(list(args)) or BotSendResult(True, True, "om_direct")
        ),
    )

    result = memorial_transport.send(
        ["--user-id", "ou_owner", "--markdown", "hello"],
        runner=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("CLI should not run")
        ),
    )

    assert result == "om_direct"
    assert calls == [["--user-id", "ou_owner", "--markdown", "hello"]]
