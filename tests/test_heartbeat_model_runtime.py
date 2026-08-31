from __future__ import annotations

import subprocess
from pathlib import Path

from core.config import Config
from core.heartbeat_model import run_heartbeat_model
from core.model_runtime import recent


def _config(tmp_path):
    path = tmp_path / "jarvis.yaml"
    path.write_text(
        """
claude:
  main_model: opus
  backup_enabled: true
  backup_auth_token: relay-secret
  backup_base_url: https://backup.example
  backup_model: relay-opus
  backup2_enabled: false
codex:
  fallback_enabled: false
openai:
  fallback_enabled: true
  fallback_model: gpt-test
  api_key: api-secret
  base_url: https://api.example/v1
""",
        encoding="utf-8",
    )
    return Config(path)


def _prompt_builder(seen):
    def build(route):
        seen.append(route.id)
        return f"system-{route.id}", f"prompt-{route.id}"

    return build


def test_text_only_heartbeat_fails_over_with_route_specific_prompt_and_receipt(
    tmp_path,
):
    prompts = []
    calls = []

    def runner(command, **kwargs):
        route = (
            "backup1"
            if kwargs["env"].get("ANTHROPIC_AUTH_TOKEN")
            else "primary"
        )
        calls.append((route, command, kwargs))
        if route == "primary":
            return subprocess.CompletedProcess(
                command,
                1,
                "",
                "You've hit your weekly limit",
            )
        return subprocess.CompletedProcess(command, 0, "backup answer", "")

    result = run_heartbeat_model(
        "logical private task",
        task_id="heartbeat:test-task",
        root=tmp_path,
        work_dir=tmp_path,
        claude_bin="claude",
        default_model="opus",
        timeout=30,
        allow_tools=False,
        prompt_builder=_prompt_builder(prompts),
        runner=runner,
        logger=lambda *_args, **_kwargs: None,
        config=_config(tmp_path),
        health_rows=[],
    )

    assert result.status == "succeeded"
    assert result.route_id == "backup1"
    assert result.text == "backup answer"
    assert prompts == ["primary", "backup1"]
    assert calls[0][1][calls[0][1].index("--system-prompt") + 1] == "system-primary"
    assert calls[1][1][calls[1][1].index("--system-prompt") + 1] == "system-backup1"
    assert [item["route_id"] for item in result_attempts(result.call_id)] == [
        "primary", "backup1",
    ]
    row = recent(1)[0]
    assert row["task_id"] == "heartbeat:test-task"
    assert "logical private task" not in str(row)


def test_default_heartbeat_model_is_preserved_without_explicit_override(
    tmp_path,
):
    models = []

    def runner(command, **_kwargs):
        models.append(command[command.index("--model") + 1])
        return subprocess.CompletedProcess(command, 0, "answer", "")

    result = run_heartbeat_model(
        "logical task",
        task_id="heartbeat:default-model",
        root=tmp_path,
        work_dir=tmp_path,
        claude_bin="claude",
        default_model="sonnet",
        timeout=30,
        allow_tools=False,
        prompt_builder=lambda _route: ("system", "prompt"),
        runner=runner,
        logger=lambda *_args, **_kwargs: None,
        config=_config(tmp_path),
        health_rows=[],
    )

    assert result.status == "succeeded"
    assert result.model == "sonnet"
    assert models == ["sonnet"]


def result_attempts(call_id):
    from core.db import get_db

    return get_db().execute(
        "SELECT route_id FROM model_runtime_attempts "
        "WHERE call_id=? ORDER BY attempt",
        (call_id,),
    ).fetchall()


def test_allow_tools_request_is_still_forced_read_only(tmp_path):
    commands = []

    def runner(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "answer", "")

    result = run_heartbeat_model(
        "tool task",
        task_id="heartbeat:tool-task",
        root=tmp_path,
        work_dir=tmp_path,
        claude_bin="claude",
        default_model="opus",
        timeout=30,
        allow_tools=True,
        prompt_builder=lambda route: (f"system-{route.id}", "prompt"),
        runner=runner,
        logger=lambda *_args, **_kwargs: None,
        config=_config(tmp_path),
        health_rows=[],
    )

    assert result.status == "succeeded"
    assert result.route_id == "primary"
    assert len(commands) == 1
    assert commands[0][commands[0].index("--tools") + 1] == ""


def test_tool_capable_admission_rejection_can_fail_over(tmp_path):
    calls = []
    commands = []

    def runner(command, **kwargs):
        commands.append(command)
        route = kwargs["env"].get("ANTHROPIC_AUTH_TOKEN") or "primary"
        calls.append(route)
        if route == "primary":
            return subprocess.CompletedProcess(
                command, 1, "", "API Error: 529 Overloaded",
            )
        return subprocess.CompletedProcess(command, 0, "recovered", "")

    result = run_heartbeat_model(
        "tool task",
        task_id="heartbeat:admission-failover",
        root=tmp_path,
        work_dir=tmp_path,
        claude_bin="claude",
        default_model="opus",
        timeout=30,
        allow_tools=True,
        prompt_builder=lambda route: (f"system-{route.id}", "prompt"),
        runner=runner,
        logger=lambda *_args, **_kwargs: None,
        config=_config(tmp_path),
        health_rows=[],
    )

    assert result.status == "succeeded"
    assert result.route_id == "backup1"
    assert calls == ["primary", "relay-secret"]
    assert all(command[command.index("--tools") + 1] == "" for command in commands)


def test_gpt_task_narrows_to_openai_without_touching_claude(
    tmp_path, monkeypatch,
):
    prompts = []
    claude_calls = []
    seen_payload = {}

    def openai_call(payload, *_args):
        seen_payload.update(payload)
        return {"output_text": "gpt answer"}

    monkeypatch.setattr("core.openai_fallback.call_openai", openai_call)
    result = run_heartbeat_model(
        "score network data",
        task_id="heartbeat:gpt-task",
        root=tmp_path,
        work_dir=tmp_path,
        claude_bin="claude",
        default_model="opus",
        requested_model="gpt",
        timeout=30,
        allow_tools=False,
        restrict_tools=True,
        prompt_builder=_prompt_builder(prompts),
        runner=lambda *args, **kwargs: claude_calls.append((args, kwargs)),
        logger=lambda *_args, **_kwargs: None,
        config=_config(tmp_path),
        health_rows=[],
    )

    assert result.status == "succeeded"
    assert result.route_id == "openai"
    assert result.model == "gpt-test"
    assert prompts == ["openai"]
    assert claude_calls == []
    assert seen_payload["instructions"].endswith("system-openai")


def test_private_heartbeat_waits_when_account_gate_disables_primary(tmp_path):
    prompts = []
    calls = []

    def runner(command, **kwargs):
        calls.append(kwargs["env"].get("ANTHROPIC_AUTH_TOKEN") or "primary")
        return subprocess.CompletedProcess(
            command, 1, "", "You've hit your weekly limit"
        )

    result = run_heartbeat_model(
        "private memory batch",
        task_id="heartbeat:private-memory",
        root=tmp_path,
        work_dir=tmp_path,
        claude_bin="claude",
        default_model="opus",
        requested_model="sonnet",
        timeout=30,
        allow_tools=False,
        primary_only=True,
        gate_state="backup",
        prompt_builder=_prompt_builder(prompts),
        runner=runner,
        logger=lambda *_args, **_kwargs: None,
        config=_config(tmp_path),
        health_rows=[],
    )

    assert result.status != "succeeded"
    assert result.terminal_reason == "no_eligible_route"
    assert calls == []
    assert prompts == []
    assert result_attempts(result.call_id) == []


def test_private_heartbeat_uses_ephemeral_read_only_codex_not_relays(
        tmp_path, monkeypatch):
    from core import codex_fallback

    config = _config(tmp_path)
    config._raw["codex"].update({
        "fallback_enabled": True,
        "fallback_model": "gpt-private",
        "binary": "/opt/codex",
    })
    prompts = []
    claude_calls = []
    codex_calls = []

    monkeypatch.setattr(
        codex_fallback, "ensure_codex_authenticated", lambda *_args: None,
    )

    def invoke(**kwargs):
        codex_calls.append(kwargs)
        return codex_fallback.CliResult(text="compiled", thread_id="unused")

    monkeypatch.setattr(codex_fallback, "invoke_codex", invoke)

    def claude_runner(command, **kwargs):
        claude_calls.append((command, kwargs))
        return subprocess.CompletedProcess(
            command, 1, "", "You've hit your weekly limit",
        )

    result = run_heartbeat_model(
        "private memory batch",
        task_id="heartbeat:private-memory-codex",
        root=tmp_path,
        work_dir=tmp_path,
        claude_bin="claude",
        default_model="opus",
        requested_model="sonnet",
        timeout=30,
        allow_tools=False,
        private_fallback="codex",
        gate_state="primary",
        prompt_builder=_prompt_builder(prompts),
        runner=claude_runner,
        logger=lambda *_args, **_kwargs: None,
        config=config,
        health_rows=[],
    )

    assert result.status == "succeeded"
    assert result.route_id == "codex"
    assert result.model == "gpt-private"
    assert result.text == "compiled"
    assert prompts == ["primary", "codex"]
    assert len(claude_calls) == 1
    assert "ANTHROPIC_AUTH_TOKEN" not in claude_calls[0][1]["env"]
    assert len(codex_calls) == 1
    assert codex_calls[0]["thread_id"] == ""
    assert codex_calls[0]["allow_tools"] is False
    assert codex_calls[0]["ephemeral"] is True
    assert codex_calls[0]["work_dir"] != tmp_path
    assert "jarvis-private-codex-" in str(codex_calls[0]["work_dir"])
    assert not Path(codex_calls[0]["work_dir"]).exists()
    assert [item["route_id"] for item in result_attempts(result.call_id)] == [
        "primary", "codex",
    ]


def test_private_codex_failure_never_falls_through_to_a_relay(
        tmp_path, monkeypatch):
    from core import codex_fallback

    config = _config(tmp_path)
    config._raw["codex"].update({
        "fallback_enabled": True,
        "fallback_model": "gpt-private",
        "binary": "/opt/codex",
    })
    monkeypatch.setattr(
        codex_fallback, "ensure_codex_authenticated",
        lambda *_args: (_ for _ in ()).throw(
            codex_fallback.CodexUnavailableError("not logged in")
        ),
    )
    claude_calls = []

    result = run_heartbeat_model(
        "private memory batch",
        task_id="heartbeat:private-memory-no-relay",
        root=tmp_path,
        work_dir=tmp_path,
        claude_bin="claude",
        default_model="opus",
        requested_model="sonnet",
        timeout=30,
        allow_tools=False,
        private_fallback="codex",
        gate_state="backup",
        prompt_builder=lambda _route: ("system", "request"),
        runner=lambda *args, **kwargs: claude_calls.append((args, kwargs)),
        logger=lambda *_args, **_kwargs: None,
        config=config,
        health_rows=[],
    )

    assert result.status == "failed"
    assert result.terminal_reason == "cli_unavailable"
    assert claude_calls == []
    assert [item["route_id"] for item in result_attempts(result.call_id)] == [
        "codex",
    ]


def test_interrupted_tool_process_maps_to_killed_without_fallback(tmp_path):
    calls = []

    def runner(command, **_kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 143, "", "terminated")

    result = run_heartbeat_model(
        "task",
        task_id="heartbeat:killed",
        root=tmp_path,
        work_dir=tmp_path,
        claude_bin="claude",
        default_model="opus",
        timeout=30,
        allow_tools=True,
        primary_only=True,
        prompt_builder=lambda _route: ("system", "prompt"),
        runner=runner,
        logger=lambda *_args, **_kwargs: None,
        config=_config(tmp_path),
        health_rows=[],
    )

    assert result.killed is True
    assert result.terminal_reason == "process_interrupted"
    assert len(calls) == 1


def test_transient_error_detail_is_useful_but_redacts_credentials(tmp_path):
    def runner(command, **_kwargs):
        return subprocess.CompletedProcess(
            command,
            1,
            "",
            (
                "Prompt is too long\n"
                "Bearer private-secret-value\n"
                "x-api-key=another-private-value"
            ),
        )

    result = run_heartbeat_model(
        "task",
        task_id="heartbeat:redacted-error",
        root=tmp_path,
        work_dir=tmp_path,
        claude_bin="claude",
        default_model="opus",
        timeout=30,
        allow_tools=True,
        primary_only=True,
        prompt_builder=lambda _route: ("system", "prompt"),
        runner=runner,
        logger=lambda *_args, **_kwargs: None,
        config=_config(tmp_path),
        health_rows=[],
    )

    assert result.terminal_reason == "context_overflow"
    assert result.error_detail.startswith("Prompt is too long")
    assert "private-secret-value" not in result.error_detail
    assert "another-private-value" not in result.error_detail
    assert "[redacted]" in result.error_detail
