import os
import signal
import subprocess
import sys
import time
from io import StringIO
from pathlib import Path

from core import aux_model
from core import model_fallback


def _result(command, code, stdout="", stderr=""):
    return subprocess.CompletedProcess(command, code, stdout, stderr)


def test_tripped_gate_starts_on_backup2_when_backup1_is_missing(
    tmp_path, monkeypatch,
):
    model_fallback.trip("spend_limit", tmp_path)
    monkeypatch.delenv("CLAUDE_BACKUP_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("CLAUDE_BACKUP_BASE_URL", raising=False)
    monkeypatch.setenv("CLAUDE_BACKUP2_ENABLED", "true")
    monkeypatch.setenv("CLAUDE_BACKUP2_AUTH_TOKEN", "backup2-token")
    monkeypatch.setenv("CLAUDE_BACKUP2_BASE_URL", "https://backup2.example")
    monkeypatch.setenv("CLAUDE_BACKUP2_MODEL", "backup2-model")
    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return _result(command, 0, "answer")

    result = aux_model.run_auxiliary_model(
        "prompt", root=tmp_path, runner=runner
    )

    assert result.provider == "Claude backup2"
    assert result.model == "backup2-model"
    assert len(calls) == 1
    assert calls[0][1]["env"]["ANTHROPIC_AUTH_TOKEN"] == "backup2-token"


def test_backup1_transport_failure_advances_to_backup2(tmp_path, monkeypatch):
    model_fallback.trip("spend_limit", tmp_path)
    monkeypatch.setenv("CLAUDE_BACKUP_ENABLED", "true")
    monkeypatch.setenv("CLAUDE_BACKUP_AUTH_TOKEN", "backup1-token")
    monkeypatch.setenv("CLAUDE_BACKUP_BASE_URL", "https://backup1.example")
    monkeypatch.setenv("CLAUDE_BACKUP2_ENABLED", "true")
    monkeypatch.setenv("CLAUDE_BACKUP2_AUTH_TOKEN", "backup2-token")
    monkeypatch.setenv("CLAUDE_BACKUP2_BASE_URL", "https://backup2.example")
    calls = []

    def runner(command, **kwargs):
        token = kwargs["env"]["ANTHROPIC_AUTH_TOKEN"]
        calls.append(token)
        if token == "backup1-token":
            return _result(command, 1, stderr="connection reset")
        return _result(command, 0, stdout="recovered")

    result = aux_model.run_auxiliary_model(
        "prompt", root=tmp_path, runner=runner
    )

    assert result.text == "recovered"
    assert result.provider == "Claude backup2"
    assert calls == ["backup1-token", "backup2-token"]


def test_backup_preserves_requested_auxiliary_model_tier(tmp_path, monkeypatch):
    model_fallback.trip("spend_limit", tmp_path)
    monkeypatch.setenv("CLAUDE_BACKUP_ENABLED", "true")
    monkeypatch.setenv("CLAUDE_BACKUP_AUTH_TOKEN", "backup-token")
    monkeypatch.setenv("CLAUDE_BACKUP_BASE_URL", "https://backup.example")
    monkeypatch.setenv("CLAUDE_BACKUP_MODEL", "claude-opus-5")
    calls = []

    def runner(command, **kwargs):
        calls.append(command)
        return _result(command, 0, stdout="cheap answer")

    result = aux_model.run_auxiliary_model(
        "small classification", root=tmp_path, model="haiku", runner=runner)

    assert result.model == "haiku"
    assert calls[0][calls[0].index("--model") + 1] == "haiku"


def test_fresh_unhealthy_backup_is_skipped_by_auxiliary_route_plan(
    tmp_path, monkeypatch,
):
    model_fallback.trip("spend_limit", tmp_path)
    monkeypatch.setenv("CLAUDE_BACKUP_ENABLED", "true")
    monkeypatch.setenv("CLAUDE_BACKUP_AUTH_TOKEN", "backup1-token")
    monkeypatch.setenv("CLAUDE_BACKUP_BASE_URL", "https://backup1.example")
    monkeypatch.setenv("CLAUDE_BACKUP2_ENABLED", "true")
    monkeypatch.setenv("CLAUDE_BACKUP2_AUTH_TOKEN", "backup2-token")
    monkeypatch.setenv("CLAUDE_BACKUP2_BASE_URL", "https://backup2.example")
    monkeypatch.setenv("CLAUDE_BACKUP2_MODEL", "backup2-model")
    monkeypatch.setattr(
        aux_model,
        "_provider_health_rows",
        lambda _root: [{
            "id": "backup1",
            "status": "unhealthy",
            "checked_epoch": time.time(),
            "observation_source": "real_request",
            "detail": "real request: network_error",
        }],
    )
    calls = []

    def runner(command, **kwargs):
        calls.append(kwargs["env"]["ANTHROPIC_AUTH_TOKEN"])
        return _result(command, 0, stdout="recovered")

    result = aux_model.run_auxiliary_model(
        "prompt", root=tmp_path, runner=runner
    )

    assert result.provider == "Claude backup2"
    assert result.model == "backup2-model"
    assert calls == ["backup2-token"]


def test_text_only_call_disables_claude_and_openai_tools(
    tmp_path, monkeypatch,
):
    calls = []

    def runner(command, **kwargs):
        calls.append(command)
        return _result(command, 1, stderr="unavailable")

    monkeypatch.setenv("OPENAI_FALLBACK_ENABLED", "true")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    seen = {}

    def openai_call(payload, *_args):
        seen.update(payload)
        return {"output_text": "text fallback"}

    monkeypatch.setattr("core.openai_fallback.call_openai", openai_call)

    result = aux_model.run_auxiliary_model(
        "untrusted external text",
        root=tmp_path,
        allow_tools=False,
        runner=runner,
    )

    assert result.provider == "GPT fallback"
    assert "--tools" in calls[0]
    assert calls[0][calls[0].index("--tools") + 1] == ""
    assert "--dangerously-skip-permissions" not in calls[0]
    assert "--strict-mcp-config" in calls[0]
    assert "tools" not in seen
    assert "No local tools are available" in seen["instructions"]


def test_owner_background_call_keeps_tool_capability(tmp_path, monkeypatch):
    calls = []

    def runner(command, **kwargs):
        calls.append(command)
        return _result(command, 0, stdout="done")

    result = aux_model.run_auxiliary_model(
        "owner task",
        root=tmp_path,
        allow_tools=True,
        session_args=("--session-id", "first-session"),
        runner=runner,
    )

    assert result.text == "done"
    assert "--dangerously-skip-permissions" in calls[0]
    assert "--tools" not in calls[0]
    assert calls[0][calls[0].index("--session-id") + 1] == "first-session"


def test_background_retries_register_distinct_provider_sessions(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("CLAUDE_BACKUP_ENABLED", "true")
    monkeypatch.setenv("CLAUDE_BACKUP_AUTH_TOKEN", "backup-token")
    monkeypatch.setenv("CLAUDE_BACKUP_BASE_URL", "https://backup.example")
    calls = []
    registered = []

    def runner(command, **kwargs):
        calls.append(command)
        if len(calls) == 1:
            return _result(command, 1, stderr="connection reset")
        return _result(command, 0, stdout="recovered")

    result = aux_model.run_auxiliary_model(
        "owner task",
        root=tmp_path,
        session_args=(
            "--resume", "main-session", "--fork-session",
            "--session-id", "11111111-1111-4111-8111-111111111111",
        ),
        session_registrar=lambda session_id: registered.append(session_id) or True,
        runner=runner,
    )

    first_id = calls[0][calls[0].index("--session-id") + 1]
    retry_id = calls[1][calls[1].index("--session-id") + 1]
    assert result.text == "recovered"
    assert first_id == "11111111-1111-4111-8111-111111111111"
    assert retry_id != first_id
    assert registered == [retry_id]


def test_background_retry_refuses_unregistered_provider_session(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("CLAUDE_BACKUP_ENABLED", "true")
    monkeypatch.setenv("CLAUDE_BACKUP_AUTH_TOKEN", "backup-token")
    monkeypatch.setenv("CLAUDE_BACKUP_BASE_URL", "https://backup.example")
    calls = []

    def runner(command, **kwargs):
        calls.append(command)
        return _result(command, 1, stderr="connection reset")

    try:
        aux_model.run_auxiliary_model(
            "owner task",
            root=tmp_path,
            session_args=("--session-id", "first-session"),
            session_registrar=lambda _session_id: False,
            runner=runner,
        )
    except RuntimeError as exc:
        assert "unregistered" in str(exc)
    else:
        raise AssertionError("unregistered retry was launched")
    assert len(calls) == 1


def test_cli_preserves_explicit_uuid_when_forking_resumed_session(
    tmp_path, monkeypatch,
):
    seen = {}

    def run(prompt, **kwargs):
        seen["prompt"] = prompt
        seen.update(kwargs)
        return aux_model.AuxiliaryModelResult(text="done")

    monkeypatch.setattr(aux_model, "run_auxiliary_model", run)
    monkeypatch.setattr(sys, "stdin", StringIO("background work"))

    result = aux_model.main([
        "--root", str(tmp_path),
        "--resume", "main-session",
        "--fork-session",
        "--session-id", "11111111-1111-4111-8111-111111111111",
    ])

    assert result == 0
    assert seen["session_args"] == (
        "--resume", "main-session", "--fork-session",
        "--session-id", "11111111-1111-4111-8111-111111111111",
    )


def test_cli_wires_retry_sessions_into_managed_job_registry(
    tmp_path, monkeypatch,
):
    from core.jobs import JobManager

    manager = JobManager(tmp_path / "jobs")
    job_id = manager.create_job("owner", "background work")
    retry_id = "22222222-2222-4222-8222-222222222222"

    def run(_prompt, **kwargs):
        assert kwargs["session_registrar"](retry_id) is True
        return aux_model.AuxiliaryModelResult(text="done")

    monkeypatch.setattr(aux_model, "run_auxiliary_model", run)
    monkeypatch.setattr(sys, "stdin", StringIO("background work"))

    assert aux_model.main([
        "--root", str(tmp_path),
        "--managed-job-id", job_id,
        "--jobs-dir", str(tmp_path / "jobs"),
    ]) == 0
    assert retry_id in manager.get_job(job_id)["session_ids"]


def test_primary_spend_limit_trips_gate_and_reaches_backup(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("CLAUDE_BACKUP_ENABLED", "true")
    monkeypatch.setenv("CLAUDE_BACKUP_AUTH_TOKEN", "backup-token")
    monkeypatch.setenv("CLAUDE_BACKUP_BASE_URL", "https://backup.example")
    calls = []

    def runner(command, **kwargs):
        token = (kwargs.get("env") or {}).get("ANTHROPIC_AUTH_TOKEN", "")
        calls.append(token)
        if not token:
            return _result(
                command, 1, stdout="You've hit your monthly spend limit"
            )
        return _result(command, 0, stdout="backup answer")

    result = aux_model.run_auxiliary_model(
        "prompt", root=tmp_path, runner=runner
    )

    assert result.provider == "Claude backup"
    assert calls == ["", "backup-token"]
    assert model_fallback.gate(tmp_path, probe=False) == "backup"


def test_error_stdout_is_never_returned_as_content(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    result = aux_model.run_auxiliary_model(
        "prompt",
        root=tmp_path,
        runner=lambda command, **_kwargs: _result(
            command,
            0,
            stdout="You've hit your monthly spend limit",
        ),
    )

    assert result.text == ""


def test_real_subprocess_adapter_reads_prompt_and_disables_tools(
    tmp_path, monkeypatch,
):
    executable = tmp_path / "claude-stub"
    executable.write_text(
        "#!/bin/sh\n"
        "input=$(cat)\n"
        "[ \"$input\" = \"hello over stdin\" ] || exit 9\n"
        "printf 'stub answer'\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    result = aux_model.run_auxiliary_model(
        "hello over stdin",
        root=tmp_path,
        allow_tools=False,
        claude_bin=str(executable),
    )

    assert result.text == "stub answer"
    assert result.provider == "Claude primary"


def test_real_subprocess_timeout_kills_descendants_holding_pipes(
    tmp_path, monkeypatch,
):
    executable = tmp_path / "claude-with-child"
    executable.write_text(
        "#!/bin/sh\n"
        "python3 -c 'import subprocess,time;"
        "subprocess.Popen([\"sleep\",\"3\"]);time.sleep(3)'\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    monkeypatch.delenv("CLAUDE_BACKUP_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("CLAUDE_BACKUP_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    started = time.monotonic()
    result = aux_model.run_auxiliary_model(
        "hang",
        root=tmp_path,
        timeout=0.2,
        claude_bin=str(executable),
    )
    elapsed = time.monotonic() - started

    assert result.text == ""
    assert elapsed < 1.5


def test_cli_signal_terminates_active_model_process_group(
    tmp_path, monkeypatch,
):
    process = object()
    terminated = []

    def fake_run(_prompt, **kwargs):
        kwargs["process_holder"]["model"] = process
        handler = signal.getsignal(signal.SIGTERM)
        handler(signal.SIGTERM, None)
        raise AssertionError("signal handler must interrupt outside spawn")

    monkeypatch.setattr(aux_model, "run_auxiliary_model", fake_run)
    monkeypatch.setattr(
        aux_model,
        "_terminate_process_group",
        lambda current: terminated.append(current),
    )
    monkeypatch.setattr("sys.stdin", StringIO("owner task"))

    try:
        aux_model.main(["--root", str(tmp_path)])
    except SystemExit as exc:
        assert exc.code == 128 + signal.SIGTERM
    else:
        raise AssertionError("SIGTERM did not interrupt auxiliary router")

    assert terminated
    assert terminated[0] is process


def test_cli_signal_during_model_spawn_reaps_new_process_group(
    tmp_path, monkeypatch,
):
    executable = tmp_path / "claude"
    executable.write_text("#!/bin/sh\nsleep 30\n", encoding="utf-8")
    executable.chmod(0o755)
    monkeypatch.setenv(
        "PATH",
        f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}",
    )
    monkeypatch.setenv("CLAUDE_BACKUP_ENABLED", "false")
    monkeypatch.setenv("CLAUDE_BACKUP2_ENABLED", "false")
    monkeypatch.setenv("OPENAI_FALLBACK_ENABLED", "false")
    monkeypatch.setattr("sys.stdin", StringIO("owner task"))
    real_popen = subprocess.Popen
    spawned = []

    def signal_during_spawn(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        spawned.append(process)
        signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
        return process

    monkeypatch.setattr(aux_model.subprocess, "Popen", signal_during_spawn)

    assert (
        aux_model.main(
            [
                "--root",
                str(tmp_path),
                "--timeout",
                "30",
                "--allow-tools",
            ]
        )
        == 128 + signal.SIGTERM
    )
    assert len(spawned) == 1
    assert spawned[0].poll() is not None


def test_cli_sigterm_reaps_real_model_process_group(tmp_path):
    executable = tmp_path / "claude"
    pid_file = tmp_path / "model.pid"
    executable.write_text(
        "#!/bin/sh\n"
        "echo $$ > \"$AUX_CHILD_PID_FILE\"\n"
        "trap '' TERM INT\n"
        "sleep 30\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ.get('PATH', '')}",
        "PYTHONPATH": str(Path(__file__).resolve().parent.parent),
        "AUX_CHILD_PID_FILE": str(pid_file),
        "CLAUDE_BACKUP_ENABLED": "false",
        "CLAUDE_BACKUP2_ENABLED": "false",
        "OPENAI_FALLBACK_ENABLED": "false",
    }
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "core.aux_model",
            "--root",
            str(tmp_path),
            "--timeout",
            "30",
            "--allow-tools",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=Path(__file__).resolve().parent.parent,
        env=env,
    )
    model_pid = 0
    try:
        assert process.stdin is not None
        process.stdin.write("owner task")
        process.stdin.close()
        deadline = time.monotonic() + 3
        while not pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert pid_file.exists()
        model_pid = int(pid_file.read_text(encoding="utf-8").strip())

        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=3) == 128 + signal.SIGTERM
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            try:
                os.kill(model_pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.02)
        else:
            raise AssertionError(
                "model process survived auxiliary-router SIGTERM"
            )
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=1)
        if model_pid:
            try:
                os.killpg(model_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_hung_primary_keeps_budget_for_backup(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_BACKUP_ENABLED", "true")
    monkeypatch.setenv("CLAUDE_BACKUP_AUTH_TOKEN", "backup-token")
    monkeypatch.setenv("CLAUDE_BACKUP_BASE_URL", "https://backup.example")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    timeouts = []

    def runner(command, **kwargs):
        timeouts.append(kwargs["timeout"])
        if not (kwargs.get("env") or {}).get("ANTHROPIC_AUTH_TOKEN"):
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        return _result(command, 0, stdout="backup recovered")

    result = aux_model.run_auxiliary_model(
        "prompt", root=tmp_path, timeout=20, runner=runner
    )

    assert result.text == "backup recovered"
    assert result.provider == "Claude backup"
    assert timeouts[0] <= 10.1
    assert len(timeouts) == 2


def test_cli_consumes_background_system_prompt_file(
    tmp_path, monkeypatch, capsys,
):
    prompt_file = tmp_path / "system.txt"
    prompt_file.write_text("private memory", encoding="utf-8")
    seen = {}

    def fake_run(prompt, **kwargs):
        seen["prompt"] = prompt
        seen.update(kwargs)
        return aux_model.AuxiliaryModelResult(
            text="done", provider="Claude primary", model="opus"
        )

    monkeypatch.setattr(aux_model, "run_auxiliary_model", fake_run)
    monkeypatch.setattr("sys.stdin", StringIO("owner task"))

    assert aux_model.main([
        "--root",
        str(tmp_path),
        "--system-prompt-file",
        str(prompt_file),
        "--consume-system-prompt-file",
    ]) == 0

    assert capsys.readouterr().out == "done"
    assert seen["system_prompt"] == "private memory"
    assert not prompt_file.exists()
