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
