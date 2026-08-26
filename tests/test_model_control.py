import json

from core.config import Config
from core import model_control


def _config(tmp_path):
    path = tmp_path / "jarvis.yaml"
    path.write_text(
        """
claude:
  main_model: opus
  backup_enabled: true
  backup_auth_token: relay-secret
  backup_base_url: https://www.micuapi.ai
  backup_model: gpt-through-claude
  backup2_enabled: false
codex:
  fallback_enabled: true
  fallback_model: gpt-codex
  binary: /opt/codex
openai:
  fallback_enabled: true
  fallback_model: gpt-api
  api_key: api-secret
  base_url: https://www.micuapi.ai/v1
""",
        encoding="utf-8",
    )
    return Config(path)


def test_catalog_separates_model_adapter_and_upstream_without_secrets(tmp_path):
    routes = model_control.model_routes(_config(tmp_path), env={})
    by_id = {route.id: route for route in routes}

    assert by_id["backup1"].model == "gpt-through-claude"
    assert by_id["backup1"].adapter == "claude_cli"
    assert by_id["openai"].adapter == "openai_responses"
    assert by_id["backup1"].upstream == "micuapi.ai"
    assert by_id["openai"].upstream == "micuapi.ai"

    serialized = json.dumps(
        model_control.catalog_report(_config(tmp_path), env={}),
        ensure_ascii=False,
    )
    assert "relay-secret" not in serialized
    assert "api-secret" not in serialized


def test_catalog_reports_nominal_routes_and_real_upstream_diversity(tmp_path):
    report = model_control.catalog_report(_config(tmp_path), env={})

    assert report["configured_route_count"] == 4
    assert report["independent_upstream_count"] == 3
    assert report["shared_upstreams"] == {
        "micuapi.ai": ["backup1", "openai"],
    }


def test_explicit_upstream_never_echoes_a_credential_url(tmp_path):
    config = _config(tmp_path)
    config._raw["openai"]["upstream"] = "https://user:secret@example.com/acct"

    serialized = json.dumps(
        model_control.catalog_report(config, env={}), ensure_ascii=False,
    )

    assert "user:secret" not in serialized
    assert "example.com" in serialized


def test_route_plan_applies_context_preference_gate_and_tool_policy(tmp_path):
    config = _config(tmp_path)

    owner = model_control.route_plan(
        "owner_chat", config=config, env={}, preference="codex"
    )
    group = model_control.route_plan("group", config=config, env={})
    gated = model_control.route_plan(
        "heartbeat", config=config, env={}, gate_state="backup"
    )

    assert [route.id for route in owner.routes] == [
        "codex", "primary", "backup1", "openai",
    ]
    assert owner.allow_tools is True
    assert "codex" not in [route.id for route in group.routes]
    assert group.allow_tools is False
    assert [route.id for route in gated.routes] == ["backup1", "openai"]
    assert gated.skipped["primary"] == "account_gate"

    forced_group = model_control.route_plan(
        "group", config=config, env={}, route_ids=("codex", "openai")
    )
    assert [route.id for route in forced_group.routes] == ["openai"]
    assert forced_group.skipped["codex"] == "context_forbidden"


def test_route_plan_defers_expired_unhealthy_route_behind_healthy_route(tmp_path):
    health = [
        {
            "id": "backup1",
            "status": "unhealthy",
            "checked_epoch": 10_000,
            "detail": "real request: request_failed",
            "observation_source": "real_request",
        },
    ]

    cooling = model_control.route_plan(
        "heartbeat",
        config=_config(tmp_path),
        env={"JARVIS_PROVIDER_UNHEALTHY_COOLDOWN_SECONDS": "1800"},
        gate_state="backup",
        health_rows=health,
        now_epoch=10_100,
    )
    recovered = model_control.route_plan(
        "heartbeat",
        config=_config(tmp_path),
        env={"JARVIS_PROVIDER_UNHEALTHY_COOLDOWN_SECONDS": "1800"},
        gate_state="backup",
        health_rows=health,
        now_epoch=12_000,
    )

    assert [route.id for route in cooling.routes] == ["openai"]
    assert cooling.skipped["backup1"] == "health_cooldown"
    assert [route.id for route in recovered.routes] == ["openai", "backup1"]


def test_primary_transient_failure_gets_one_recovery_turn_after_cooldown(
        tmp_path):
    row = {
        "id": "primary",
        "status": "unhealthy",
        "observation_source": "real_request",
        "detail": "real request: server_overloaded",
        "checked_epoch": 10_000,
        "cooldown_until_epoch": 10_300,
    }

    cooling = model_control.route_plan(
        "owner_chat", config=_config(tmp_path), env={},
        health_rows=[row], now_epoch=10_299,
    )
    recovered = model_control.route_plan(
        "owner_chat", config=_config(tmp_path), env={},
        health_rows=[row], now_epoch=10_300,
    )

    assert cooling.skipped["primary"] == "health_cooldown"
    assert recovered.routes[0].id == "primary"


def test_account_gate_probe_overrides_stale_primary_health_cooldown(tmp_path):
    row = {
        "id": "primary",
        "status": "unhealthy",
        "observation_source": "real_request",
        "detail": "real request: account_limit",
        "checked_epoch": 10_000,
        "cooldown_until_epoch": 99_999,
    }

    plan = model_control.route_plan(
        "owner_chat",
        config=_config(tmp_path),
        env={},
        gate_state="probe",
        health_rows=[row],
        now_epoch=10_100,
    )

    assert plan.routes[0].id == "primary"


def test_runtime_status_exposes_plan_and_diversity_but_not_credentials(
    tmp_path,
):
    _config(tmp_path)
    health_rows = [
            {"id": "primary", "status": "healthy", "actual_model": "opus"},
            {"id": "backup1", "status": "healthy", "actual_model": ""},
            {"id": "codex", "status": "not_run", "actual_model": ""},
            {"id": "openai", "status": "healthy", "actual_model": "gpt-api"},
    ]

    status = model_control.runtime_status_text(tmp_path, health_rows=health_rows)

    assert "当前执行计划" in status
    assert "4 条已配置 route / 3 个独立上游" in status
    assert "micuapi.ai: backup1,openai" in status
    assert "relay-secret" not in status
    assert "api-secret" not in status


def test_harness_environment_is_generated_from_the_same_catalog(tmp_path):
    private = model_control.harness_environment(_config(tmp_path))

    assert private["CLAUDE_BACKUP_MODEL"] == "gpt-through-claude"
    assert private["CLAUDE_BACKUP_AUTH_TOKEN"] == "relay-secret"
    assert private["CLAUDE_RELAY_ATTEMPT_TIMEOUT"] == "120"
    assert private["OPENAI_FALLBACK_MODEL"] == "gpt-api"
    assert private["OPENAI_API_KEY_CONFIG"] == "api-secret"
