from __future__ import annotations

import io
import json
import signal
import subprocess
from pathlib import Path

import pytest

from core.config import Config
from core.model_control import model_routes
from core.model_runtime import recent
from core.model_runtime import RuntimeRequest
from core.owner_chat_adapters import OwnerChatAdapters, process_runner
from core.owner_chat_model import main, run_owner_chat_model


def _config(tmp_path, *, codex=True, openai=True):
    path = tmp_path / "jarvis.yaml"
    path.write_text(
        f"""
claude:
  main_model: opus
  backup_enabled: true
  backup_auth_token: relay-secret
  backup_base_url: https://backup.example
  backup_model: relay-opus
  backup2_enabled: false
codex:
  fallback_enabled: {str(codex).lower()}
  fallback_model: gpt-codex
  binary: /bin/true
openai:
  fallback_enabled: {str(openai).lower()}
  fallback_model: gpt-api
  api_key: api-secret
  base_url: https://api.example/v1
""",
        encoding="utf-8",
    )
    return Config(path)


def _run(tmp_path, **kwargs):
    defaults = {
        "task_id": "lark:om_message",
        "conv_key": "oc_owner",
        "context_key": "matter:mat_test",
        "matter_id": "mat_test",
        "session_id": "11111111-1111-4111-8111-111111111111",
        "session_dir": tmp_path / "sessions",
        "root": tmp_path,
        "work_dir": tmp_path,
        "system_prompt": "primary private context",
        "backup_system_prompt": "bounded backup context",
        "timeout": 30,
        "preference": "auto",
        "gate_state": "primary",
        "logger": lambda *_args, **_kwargs: None,
        "config": _config(tmp_path),
        "health_rows": [],
    }
    defaults.update(kwargs)
    return run_owner_chat_model("private owner request", **defaults)


def test_owner_chat_failover_uses_runtime_receipt_and_route_specific_prompt(
    tmp_path,
):
    calls = []

    def runner(command, **kwargs):
        route = kwargs["env"].get("ANTHROPIC_AUTH_TOKEN") or "primary"
        prompt_path = Path(command[command.index(
            "--append-system-prompt-file"
        ) + 1])
        calls.append((
            route, command, kwargs, prompt_path.read_text(), prompt_path,
            prompt_path.stat().st_mode & 0o777,
        ))
        if route == "primary":
            return subprocess.CompletedProcess(
                command, 1, "", "You've hit your weekly limit",
            )
        return subprocess.CompletedProcess(
            command,
            0,
            json.dumps({"subtype": "success", "result": "backup answer"}),
            "",
        )

    result = _run(tmp_path, runner=runner)

    assert result.status == "succeeded"
    assert result.route_id == "backup1"
    assert result.provider == "Claude backup"
    assert result.text == "backup answer"
    assert result.attempted_routes == ("primary", "backup1")
    assert calls[0][2]["input"] == "private owner request"
    assert "private owner request" not in calls[0][1]
    assert "primary private context" not in calls[0][1]
    assert "bounded backup context" not in calls[1][1]
    assert calls[0][3] == "primary private context"
    assert calls[1][3] == "bounded backup context"
    assert all(not item[4].exists() for item in calls)
    assert all(item[5] == 0o600 for item in calls)
    assert calls[1][2]["env"]["ANTHROPIC_AUTH_TOKEN"] == "relay-secret"
    row = recent(1)[0]
    assert row["task_id"] == "lark:om_message"
    assert row["matter_id"] == "mat_test"
    assert row["selected_route"] == "backup1"
    assert "private owner request" not in str(row)


def test_owner_chat_ambiguous_tool_failure_never_replays(tmp_path):
    calls = []

    def runner(command, **_kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(
            command, 1, "", "tool stream ended after execution",
        )

    def should_not_run(**_kwargs):
        raise AssertionError("ambiguous owner turn must not be replayed")

    result = _run(
        tmp_path,
        runner=runner,
        codex_runner=should_not_run,
        openai_runner=should_not_run,
    )

    assert result.status == "ambiguous"
    assert result.route_id == "primary"
    assert result.attempted_routes == ("primary",)
    assert len(calls) == 1


def test_owner_chat_cancellation_never_replays_on_another_provider(tmp_path):
    def runner(command, **_kwargs):
        return subprocess.CompletedProcess(command, 143, "", "terminated")

    def should_not_run(**_kwargs):
        raise AssertionError("cancelled owner turn must not be replayed")

    result = _run(
        tmp_path,
        runner=runner,
        codex_runner=should_not_run,
        openai_runner=should_not_run,
    )

    # A tool-capable process may have completed an effect before SIGTERM.
    # Runtime therefore requires reconciliation, but still never replays it.
    assert result.status == "ambiguous"
    assert result.terminal_reason == "process_interrupted"
    assert result.route_id == "primary"
    assert result.attempted_routes == ("primary",)


def test_owner_chat_codex_preference_preserves_logical_context(tmp_path):
    seen = {}

    def codex_runner(**kwargs):
        seen.update(kwargs)
        return "codex answer"

    def should_not_run(*_args, **_kwargs):
        raise AssertionError("preferred healthy Codex should answer first")

    result = _run(
        tmp_path,
        preference="codex",
        runner=should_not_run,
        codex_runner=codex_runner,
    )

    assert result.status == "succeeded"
    assert result.route_id == "codex"
    assert result.provider == "Codex"
    assert seen["conv_key"] == "oc_owner"
    assert seen["context_key"] == "matter:mat_test"
    assert seen["allow_tools"] is True
    assert seen["system_prompt"] == "primary private context"


def test_proven_codex_unavailability_can_fall_back_to_claude(tmp_path):
    from core.codex_fallback import CodexUnavailableError

    routes = []

    def codex_runner(**_kwargs):
        raise CodexUnavailableError("not logged in")

    def runner(command, **kwargs):
        routes.append(kwargs["env"].get("ANTHROPIC_AUTH_TOKEN") or "primary")
        return subprocess.CompletedProcess(
            command,
            0,
            json.dumps({"subtype": "success", "result": "claude answer"}),
            "",
        )

    result = _run(
        tmp_path,
        preference="codex",
        runner=runner,
        codex_runner=codex_runner,
    )

    assert result.status == "succeeded"
    assert result.attempted_routes == ("codex", "primary")
    assert routes == ["primary"]


def test_claude_owner_session_resumes_only_when_the_session_exists(tmp_path):
    commands = []

    def runner(command, **_kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(
            command,
            0,
            json.dumps({"subtype": "success", "result": "answer"}),
            "",
        )

    session_dir = tmp_path / "sessions"
    _run(tmp_path, runner=runner, session_dir=session_dir)
    assert "--session-id" in commands[-1]
    assert "--resume" not in commands[-1]

    session_dir.mkdir(parents=True)
    (session_dir / "11111111-1111-4111-8111-111111111111.jsonl").write_text(
        "{}\n", encoding="utf-8",
    )
    _run(tmp_path, runner=runner, session_dir=session_dir)
    assert "--resume" in commands[-1]
    assert "--session-id" not in commands[-1]


def test_owner_runtime_public_envelope_contains_only_bounded_receipt_data(
    tmp_path,
):
    def runner(command, **_kwargs):
        return subprocess.CompletedProcess(
            command,
            0,
            json.dumps({"subtype": "success", "result": "answer"}),
            "",
        )

    envelope = _run(tmp_path, runner=runner).envelope()

    assert envelope["subtype"] == "success"
    assert envelope["result"] == "answer"
    assert envelope["runtime"]["schema"] == "jarvis.owner-chat-model.v1"
    assert envelope["runtime"]["call_id"].startswith("mrc_")
    assert "private owner request" not in json.dumps(envelope)
    assert "primary private context" not in json.dumps(envelope)


def test_explicit_route_facts_are_not_reacquired_inside_the_runtime(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(
        "core.owner_chat_model.get_preference",
        lambda _key: (_ for _ in ()).throw(AssertionError("double preference read")),
    )
    monkeypatch.setattr(
        "core.owner_chat_model.primary_gate",
        lambda _root: (_ for _ in ()).throw(AssertionError("double gate lease")),
    )

    def runner(command, **_kwargs):
        return subprocess.CompletedProcess(
            command,
            0,
            json.dumps({"subtype": "success", "result": "answer"}),
            "",
        )

    result = _run(
        tmp_path, runner=runner, preference="auto", gate_state="primary",
    )
    assert result.status == "succeeded"


def test_resident_owner_path_passes_one_gate_and_keeps_groups_outside_runtime():
    source = (
        Path(__file__).resolve().parents[1] / "bot.sh"
    ).read_text(encoding="utf-8")

    assert '[ "$is_owner_p2p" -eq 1 ] && _use_model_runtime=1' in source
    assert "python3 -m core.owner_chat_model" in source
    assert '--preference "$_provider_preference"' in source
    assert '--gate-state "$_provider_gate"' in source
    assert '[ "$_use_model_runtime" -eq 1 ] && _attempt_sequence="1"' in source
    assert "no shell-level replay" in source


def test_cli_signal_terminates_the_active_provider_before_exit(
    tmp_path, monkeypatch,
):
    prompt = tmp_path / "system.txt"
    prompt.write_text("system", encoding="utf-8")
    sentinel = object()
    terminated = []

    def interrupted(_content, **kwargs):
        kwargs["process_holder"]["process"] = sentinel
        signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
        raise AssertionError("signal handler must interrupt the model call")

    monkeypatch.setattr("core.owner_chat_model.run_owner_chat_model", interrupted)
    monkeypatch.setattr(
        "core.owner_chat_model.terminate_process_group",
        lambda process: terminated.append(process),
    )
    monkeypatch.setattr("sys.stdin", io.StringIO("owner request"))

    with pytest.raises(SystemExit) as exc:
        main([
            "--task-id", "lark:om_cancel",
            "--conv-key", "owner",
            "--session-id", "11111111-1111-4111-8111-111111111111",
            "--session-dir", str(tmp_path / "sessions"),
            "--system-prompt-file", str(prompt),
            "--work-dir", str(tmp_path),
            "--root", str(tmp_path),
            "--preference", "auto",
            "--gate-state", "primary",
        ])

    assert exc.value.code == 128 + signal.SIGTERM
    assert terminated == [sentinel]


def test_agentic_openai_tools_share_the_owner_cancellation_holder(
    tmp_path, monkeypatch,
):
    from core import openai_fallback

    holder = {"cancelled": 0}
    captured = {}

    def agentic(*_args, **kwargs):
        captured.update(kwargs)
        return "gpt answer"

    monkeypatch.setattr(openai_fallback, "run_agentic", agentic)
    config = _config(tmp_path)
    route = next(item for item in model_routes(config) if item.id == "openai")
    adapters = OwnerChatAdapters(
        base=tmp_path,
        work=tmp_path,
        sessions=tmp_path / "sessions",
        session_id="11111111-1111-4111-8111-111111111111",
        conv_key="owner",
        context_key="matter:test",
        system_prompt="system",
        backup_system_prompt="bounded",
        claude_bin="claude",
        runner=process_runner(holder),
        process_holder=holder,
        codex_runner=None,
        openai_runner=None,
        logger=lambda *_args, **_kwargs: None,
    )

    outcome = adapters.openai(
        route,
        RuntimeRequest(
            task_id="lark:om_openai",
            prompt="owner request",
            context="owner_chat",
            effect_authority="external",
            allow_tools=True,
        ),
        "gpt-api",
        10,
    )

    assert outcome.status == "succeeded"
    assert captured["process_holder"] is holder
    assert captured["process_key"] == "process"
    holder["cancelled"] = signal.SIGTERM
    assert captured["cancelled"]() is True
