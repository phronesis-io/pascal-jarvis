import json
import subprocess
import threading

from core import provider_health as ph


def _write_config(tmp_path):
    (tmp_path / "jarvis.yaml").write_text(
        """
claude:
  main_model: opus
  backup_enabled: true
  backup_auth_token: backup-secret-token
  backup_base_url: https://backup.example
  backup_model: relay-opus
  backup2_enabled: false
codex:
  fallback_enabled: false
openai:
  fallback_enabled: true
  fallback_model: gpt-test
  api_key: sk-super-secret-value
  base_url: https://openai.example/v1
""",
        encoding="utf-8",
    )


def test_snapshot_distinguishes_configured_disabled_and_unverified(tmp_path):
    _write_config(tmp_path)

    state = ph.snapshot(tmp_path)
    rows = {row["id"]: row for row in state["providers"]}

    assert rows["primary"]["status"] == "not_run"
    assert rows["backup1"]["status"] == "not_run"
    assert rows["backup1"]["requested_model"] == "relay-opus"
    assert rows["backup2"]["status"] == "disabled"
    assert rows["codex"]["status"] == "disabled"
    assert rows["openai"]["status"] == "not_run"
    assert "secret" not in json.dumps(state)


def test_claude_probe_keeps_credentials_out_of_argv_and_state(tmp_path):
    _write_config(tmp_path)
    spec = ph.provider_specs(ph.Config(tmp_path / "jarvis.yaml"))[1]
    seen = {}

    def runner(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["env"] = kwargs["env"]
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout=json.dumps({"result": ph.CANARY_MARKER, "model": "relay-real"}),
            stderr="",
        )

    result = ph.probe_provider(spec, root=tmp_path, runner=runner)

    assert result["status"] == "healthy"
    assert result["actual_model"] == "relay-real"
    assert "backup-secret-token" not in " ".join(seen["cmd"])
    assert seen["env"]["ANTHROPIC_AUTH_TOKEN"] == "backup-secret-token"
    assert seen["cmd"][seen["cmd"].index("--permission-mode") + 1] == "dontAsk"
    assert seen["cmd"][seen["cmd"].index("--tools") + 1] == ""
    assert (
        seen["cmd"][seen["cmd"].index("--mcp-config") + 1]
        == '{"mcpServers":{}}'
    )
    assert "--strict-mcp-config" in seen["cmd"]
    assert "token" not in json.dumps(result).lower()


def test_openai_probe_uses_auth_argument_and_records_observed_model(tmp_path):
    _write_config(tmp_path)
    spec = ph.provider_specs(ph.Config(tmp_path / "jarvis.yaml"))[-1]
    seen = {}

    def caller(payload, api_key, base_url, timeout, user_agent=""):
        seen.update(
            payload=payload,
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
        )
        return {"output_text": ph.CANARY_MARKER, "model": "gpt-observed"}

    result = ph.probe_provider(
        spec, root=tmp_path, openai_caller=caller
    )

    assert result["status"] == "healthy"
    assert result["actual_model"] == "gpt-observed"
    assert seen["api_key"] == "sk-super-secret-value"
    assert seen["payload"]["model"] == "gpt-test"
    assert "sk-super-secret-value" not in json.dumps(result)


def test_claude_probe_requires_exact_canary_marker(tmp_path):
    _write_config(tmp_path)
    spec = ph.provider_specs(ph.Config(tmp_path / "jarvis.yaml"))[0]

    def runner(command, **_kwargs):
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps({
                "result": f"Unable to return {ph.CANARY_MARKER}",
            }),
            stderr="",
        )

    result = ph.probe_provider(spec, root=tmp_path, runner=runner)

    assert result["status"] == "unhealthy"


def test_openai_probe_requires_exact_canary_marker(tmp_path):
    _write_config(tmp_path)
    spec = ph.provider_specs(ph.Config(tmp_path / "jarvis.yaml"))[-1]

    result = ph.probe_provider(
        spec,
        root=tmp_path,
        openai_caller=lambda *_args, **_kwargs: {
            "output_text": f"Unable to return {ph.CANARY_MARKER}",
        },
    )

    assert result["status"] == "unhealthy"


def test_explanatory_canary_output_does_not_clear_sticky_fallback(tmp_path):
    from core.model_fallback import gate, trip

    _write_config(tmp_path)
    trip("spend_limit", tmp_path)

    def runner(command, **kwargs):
        # Backup1 gets base_url overridden to "https://backup.example";
        # primary inherits the ambient env (which may have its own value).
        if kwargs["env"].get("ANTHROPIC_BASE_URL") == "https://backup.example":
            output = ph.CANARY_MARKER
        else:
            output = f"Unable to return {ph.CANARY_MARKER}"
        return subprocess.CompletedProcess(
            command, 0, stdout=json.dumps({"result": output}), stderr=""
        )

    ph.probe_all(
        tmp_path,
        runner=runner,
        openai_caller=lambda *_args, **_kwargs: {
            "output_text": ph.CANARY_MARKER,
        },
    )

    assert gate(tmp_path, probe=False) == "backup"


def test_probe_all_persists_redacted_results(tmp_path):
    _write_config(tmp_path)

    def runner(cmd, **kwargs):
        return subprocess.CompletedProcess(
            cmd, 0, stdout=json.dumps({"result": ph.CANARY_MARKER}), stderr=""
        )

    def caller(*args, **kwargs):
        return {"output_text": ph.CANARY_MARKER}

    state = ph.probe_all(
        tmp_path, runner=runner, openai_caller=caller
    )
    saved = json.loads(
        (tmp_path / ph.STATE_FILE).read_text(encoding="utf-8")
    )

    assert saved == state
    assert [row["status"] for row in state["providers"]] == [
        "healthy",
        "healthy",
        "disabled",
        "disabled",
        "healthy",
    ]
    serialized = json.dumps(saved)
    assert "backup-secret-token" not in serialized
    assert "sk-super-secret-value" not in serialized


def test_probe_all_runs_independent_claude_routes_concurrently(tmp_path):
    _write_config(tmp_path)
    rendezvous = threading.Barrier(2)

    def runner(cmd, **kwargs):
        rendezvous.wait(timeout=1)
        return subprocess.CompletedProcess(
            cmd, 0, stdout=json.dumps({"result": ph.CANARY_MARKER}), stderr=""
        )

    state = ph.probe_all(
        tmp_path,
        runner=runner,
        openai_caller=lambda *a, **k: {"output_text": ph.CANARY_MARKER},
    )

    rows = {row["id"]: row for row in state["providers"]}
    assert rows["primary"]["status"] == "healthy"
    assert rows["backup1"]["status"] == "healthy"


def test_probe_all_preserves_real_failure_written_during_canary(tmp_path):
    _write_config(tmp_path)
    probe_started = threading.Event()
    finish_probe = threading.Event()
    result = {}

    def runner(cmd, **kwargs):
        probe_started.set()
        assert finish_probe.wait(timeout=2)
        return subprocess.CompletedProcess(
            cmd, 0, stdout=json.dumps({"result": ph.CANARY_MARKER}), stderr=""
        )

    def run_probe():
        result["state"] = ph.probe_all(
            tmp_path,
            runner=runner,
            openai_caller=lambda *a, **k: {"output_text": ph.CANARY_MARKER},
        )

    thread = threading.Thread(target=run_probe)
    thread.start()
    assert probe_started.wait(timeout=2)
    ph.observe("backup1", "unhealthy", root=tmp_path)
    finish_probe.set()
    thread.join(timeout=3)

    assert not thread.is_alive()
    row = next(
        item for item in result["state"]["providers"]
        if item["id"] == "backup1"
    )
    assert row["status"] == "unhealthy"
    assert row["observation_source"] == "real_request"
    assert row["detail"] == "real request: request_failed"


def test_provider_specs_honors_runtime_codex_binary_override(tmp_path, monkeypatch):
    _write_config(tmp_path)
    monkeypatch.setenv("CODEX_FALLBACK_BINARY", "/runtime/codex")
    monkeypatch.setenv("CODEX_BIN", "/other/codex")

    row = next(
        item for item in ph.provider_specs(ph.Config(tmp_path / "jarvis.yaml"))
        if item["id"] == "codex"
    )

    assert row["binary"] == "/runtime/codex"


def test_recent_real_failure_skips_relay_during_cooldown(tmp_path):
    _write_config(tmp_path)
    config_path = tmp_path / "jarvis.yaml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            "codex:\n  fallback_enabled: false",
            "codex:\n  fallback_enabled: true\n"
            "  fallback_model: gpt-test\n  binary: /opt/codex",
        ),
        encoding="utf-8",
    )

    ph.observe(
        "backup1", "unhealthy", "request_failed",
        root=tmp_path, now_epoch=10_000,
    )

    assert ph.preferred_fallback(
        tmp_path, now_epoch=10_100, cooldown_seconds=1800
    ) == "codex"
    assert ph.preferred_fallback(
        tmp_path, now_epoch=12_000, cooldown_seconds=1800
    ) == "backup1"


def test_preferred_fallback_honors_callers_supported_provider_set(
    tmp_path, monkeypatch
):
    _write_config(tmp_path)
    monkeypatch.setenv("CLAUDE_BACKUP_ENABLED", "false")
    monkeypatch.setenv("CLAUDE_BACKUP2_ENABLED", "false")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-only")

    assert ph.preferred_fallback(
        tmp_path, provider_ids=("backup1", "backup2", "openai")
    ) == "openai"

    monkeypatch.setenv("OPENAI_FALLBACK_ENABLED", "false")
    assert ph.preferred_fallback(
        tmp_path, provider_ids=("backup1", "backup2", "openai")
    ) == "none"


def test_real_observation_persists_only_bounded_reason_codes(tmp_path):
    _write_config(tmp_path)

    ph.observe(
        "backup1",
        "unhealthy",
        "API Error: 424 token=sk-super-secret-value",
        root=tmp_path,
        now_epoch=10_000,
    )

    saved = (tmp_path / ph.STATE_FILE).read_text(encoding="utf-8")
    row = next(
        item for item in json.loads(saved)["providers"]
        if item["id"] == "backup1"
    )
    assert row["observation_source"] == "real_request"
    assert row["detail"] == "real request: request_failed"
    assert "424" not in saved
    assert "super-secret" not in saved


def test_codex_probe_is_ephemeral_read_only_and_exact(tmp_path, monkeypatch):
    _write_config(tmp_path)
    config_path = tmp_path / "jarvis.yaml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            "codex:\n  fallback_enabled: false",
            "codex:\n  fallback_enabled: true\n  fallback_model: gpt-test\n"
            "  binary: /opt/codex",
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(ph, "resolve_codex_bin", lambda configured="": "/opt/codex")
    spec = next(
        row for row in ph.provider_specs(ph.Config(config_path))
        if row["id"] == "codex"
    )
    seen = {}

    def runner(command, **kwargs):
        seen["command"] = command
        seen["kwargs"] = kwargs
        return subprocess.CompletedProcess(
            command,
            0,
            stdout="\n".join([
                json.dumps({"type": "thread.started", "thread_id": "ignored"}),
                json.dumps({
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": ph.CANARY_MARKER},
                }),
            ]),
            stderr="",
        )

    result = ph.probe_provider(spec, root=tmp_path, runner=runner)

    assert result["status"] == "healthy"
    assert result["actual_model"] == "gpt-test"
    assert "--ephemeral" in seen["command"]
    assert seen["command"][seen["command"].index("--sandbox") + 1] == "read-only"
    assert "--approve-for-me" not in seen["command"]


def test_codex_probe_rejects_explanatory_marker(tmp_path, monkeypatch):
    _write_config(tmp_path)
    spec = {
        "id": "codex", "label": "Codex fallback", "kind": "codex",
        "enabled": True, "configured": True, "model": "gpt-test",
        "binary": "/opt/codex",
    }
    monkeypatch.setattr(ph, "resolve_codex_bin", lambda configured="": "/opt/codex")

    result = ph.probe_provider(
        spec,
        root=tmp_path,
        runner=lambda command, **kwargs: subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps({
                "type": "item.completed",
                "item": {
                    "type": "agent_message",
                    "text": f"Unable to return {ph.CANARY_MARKER}",
                },
            }),
            stderr="",
        ),
    )

    assert result["status"] == "unhealthy"


def test_codex_probe_reports_stream_error_before_incidental_stderr(
    tmp_path, monkeypatch,
):
    spec = {
        "id": "codex", "label": "Codex fallback", "kind": "codex",
        "enabled": True, "configured": True, "model": "gpt-test",
        "binary": "/opt/codex",
    }
    monkeypatch.setattr(ph, "resolve_codex_bin", lambda configured="": "/opt/codex")
    output = "\n".join([
        json.dumps({"type": "thread.started", "thread_id": "ignored"}),
        json.dumps({
            "type": "error",
            "message": "You've hit your usage limit. Try again Aug 18.",
        }),
        json.dumps({
            "type": "turn.failed",
            "error": {
                "message": "You've hit your usage limit. Try again Aug 18."
            },
        }),
    ])

    result = ph.probe_provider(
        spec,
        root=tmp_path,
        runner=lambda command, **kwargs: subprocess.CompletedProcess(
            command,
            1,
            stdout=output,
            stderr="WARN codex_rollout state db discrepancy: falling_back",
        ),
    )

    assert result["status"] == "unhealthy"
    assert result["detail"] == "You've hit your usage limit. Try again Aug 18."
    assert "state db discrepancy" not in result["detail"]


def test_codex_probe_redacts_stream_error_and_falls_back_to_stderr(
    tmp_path, monkeypatch,
):
    spec = {
        "id": "codex", "label": "Codex fallback", "kind": "codex",
        "enabled": True, "configured": True, "model": "gpt-test",
        "binary": "/opt/codex",
    }
    monkeypatch.setattr(ph, "resolve_codex_bin", lambda configured="": "/opt/codex")

    secret = ph.probe_provider(
        spec,
        root=tmp_path,
        runner=lambda command, **kwargs: subprocess.CompletedProcess(
            command,
            1,
            stdout=json.dumps({
                "type": "turn.failed",
                "error": {"message": "token=do-not-store HTTP 401"},
            }),
            stderr="incidental warning",
        ),
    )
    fallback = ph.probe_provider(
        spec,
        root=tmp_path,
        runner=lambda command, **kwargs: subprocess.CompletedProcess(
            command, 1, stdout="not json", stderr="process launch failed"
        ),
    )

    assert "do-not-store" not in secret["detail"]
    assert "[redacted]" in secret["detail"]
    assert fallback["detail"] == "process launch failed"


def test_spend_limit_canary_trips_shared_provider_gate(tmp_path):
    _write_config(tmp_path)

    def runner(cmd, **kwargs):
        # Primary never gets base_url overridden to the backup endpoint.
        if kwargs["env"].get("ANTHROPIC_BASE_URL") == "https://backup.example":
            return subprocess.CompletedProcess(
                cmd, 0, stdout=json.dumps({"result": ph.CANARY_MARKER}), stderr=""
            )
        return subprocess.CompletedProcess(
            cmd, 1, stdout="", stderr="You've hit your monthly spend limit"
        )

    ph.probe_all(
        tmp_path,
        runner=runner,
        openai_caller=lambda *a, **k: {"output_text": ph.CANARY_MARKER},
    )

    from core.model_fallback import gate

    assert gate(tmp_path, probe=False) == "backup"


def test_session_limit_canary_trips_shared_provider_gate(tmp_path):
    _write_config(tmp_path)

    def runner(cmd, **kwargs):
        if kwargs["env"].get("ANTHROPIC_BASE_URL") == "https://backup.example":
            return subprocess.CompletedProcess(
                cmd, 0, stdout=json.dumps({"result": ph.CANARY_MARKER}), stderr=""
            )
        return subprocess.CompletedProcess(
            cmd,
            1,
            stdout="",
            stderr="HTTP 429: You've hit your session limit · resets 6pm (Asia/Shanghai)",
        )

    ph.probe_all(
        tmp_path,
        runner=runner,
        openai_caller=lambda *a, **k: {"output_text": ph.CANARY_MARKER},
    )

    from core.model_fallback import gate

    assert gate(tmp_path, probe=False) == "backup"


def test_probe_failure_redacts_secret_shaped_error(tmp_path):
    _write_config(tmp_path)
    spec = ph.provider_specs(ph.Config(tmp_path / "jarvis.yaml"))[1]

    def runner(cmd, **kwargs):
        return subprocess.CompletedProcess(
            cmd, 1, stdout="", stderr="token=do-not-store HTTP 401"
        )

    result = ph.probe_provider(spec, root=tmp_path, runner=runner)

    assert result["status"] == "unhealthy"
    assert "do-not-store" not in result["detail"]
    assert "[redacted]" in result["detail"]


def test_probe_failure_reports_the_api_error_not_an_incidental_stderr_notice(
        tmp_path):
    """A relay token makes the CLI print a connector notice on stderr even
    though the real failure is the 403 in the JSON result. Reporting stderr
    first sent an operator after the wrong thing during the v1.7.0 release
    verification (backup relay was actually failing authentication).
    """
    _write_config(tmp_path)
    spec = ph.provider_specs(ph.Config(tmp_path / "jarvis.yaml"))[1]

    def runner(cmd, **kwargs):
        return subprocess.CompletedProcess(
            cmd, 1,
            stdout=json.dumps({
                "is_error": True,
                "api_error_status": 403,
                "subtype": "success",
                "result": "Failed to authenticate with the upstream relay",
            }),
            stderr=("⚠ claude.ai connectors are disabled because "
                    "ANTHROPIC_API_KEY or another auth source is set"),
        )

    result = ph.probe_provider(spec, root=tmp_path, runner=runner)

    assert result["status"] == "unhealthy"
    assert "Failed to authenticate" in result["detail"]
    assert "HTTP 403" in result["detail"]
    assert "connectors are disabled" not in result["detail"]


def test_probe_failure_still_falls_back_to_stderr_when_result_is_empty(tmp_path):
    """A CLI that dies before producing JSON must still report why."""
    _write_config(tmp_path)
    spec = ph.provider_specs(ph.Config(tmp_path / "jarvis.yaml"))[1]

    def runner(cmd, **kwargs):
        return subprocess.CompletedProcess(
            cmd, 1, stdout="", stderr="relay connection reset")

    result = ph.probe_provider(spec, root=tmp_path, runner=runner)

    assert result["status"] == "unhealthy"
    assert "relay connection reset" in result["detail"]


# ── heartbeat contract (tasks/provider_canary_pre.sh) ────────────────────
# The CLI exits 1 when a rung is unhealthy — correct for a human asking "is
# the chain OK?". The heartbeat reads a nonzero pre-script as "this task
# failed" and trips its circuit. Conflating the two took the canary dark for
# 32h during a real backup-relay outage (~3.8k circuit_open skips in a day),
# so the pre-hook must separate "found a problem" from "could not look".

import os
import subprocess as _sp
from pathlib import Path as _Path

_HOOK = _Path(__file__).resolve().parent.parent / "tasks" / "provider_canary_pre.sh"


def _run_hook(tmp_path, probe_stdout: str, probe_rc: int):
    """Run the real hook with a stub `python3 -m core.provider_health probe`."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    stub = bindir / "python3"
    payload = probe_stdout.replace("\\", "\\\\").replace("'", "'\\''")
    # The hook also pipes JSON through a real `python3 -c`. Forward those to
    # the ABSOLUTE interpreter — `env python3` would re-resolve through PATH,
    # find this stub again, and recurse forever.
    import sys as _sys
    stub.write_text(
        "#!/bin/bash\n"
        f'if [ "$1" = "-c" ]; then exec {_sys.executable} "$@"; fi\n'
        f"printf '%s' '{payload}'\n"
        f"exit {probe_rc}\n",
        encoding="utf-8")
    stub.chmod(0o755)
    env = dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}",
               JARVIS_DIR=str(_Path(_HOOK).parent.parent))
    return _sp.run(["bash", str(_HOOK)], capture_output=True, text=True, env=env)


def test_unhealthy_rung_is_a_finding_not_a_task_failure(tmp_path):
    out = _run_hook(
        tmp_path,
        '{"providers":[{"id":"backup1","status":"unhealthy",'
        '"detail":"HTTP 402: no active subscription plan"}]}', 1)
    assert out.returncode == 0, out.stderr
    assert "backup1" in out.stdout          # the finding still surfaces


def test_all_healthy_exits_zero(tmp_path):
    out = _run_hook(tmp_path, '{"providers":[{"id":"primary","status":"healthy"}]}', 0)
    assert out.returncode == 0
    assert "primary" in out.stdout


def test_probe_that_could_not_run_is_a_real_failure(tmp_path):
    assert _run_hook(tmp_path, "", 1).returncode == 1


def test_unparseable_probe_output_is_a_real_failure(tmp_path):
    out = _run_hook(tmp_path, "Traceback (most recent call last):", 1)
    assert out.returncode == 1


def test_json_without_providers_is_a_real_failure(tmp_path):
    """An empty report means the probe learned nothing — not a clean run."""
    assert _run_hook(tmp_path, '{"version":1,"providers":[]}', 0).returncode == 1
