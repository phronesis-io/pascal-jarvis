from __future__ import annotations

import core.model_runtime as model_runtime
from core.config import Config
from core.model_runtime import (
    AdapterResult,
    RuntimeRequest,
    audit,
    execute,
    recent,
    recover_abandoned,
)


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


def test_runtime_routes_and_persists_attribution_without_prompt(tmp_path):
    seen = []

    def claude(route, request, model, timeout):
        seen.append((route.id, request.task_id, model, timeout))
        if route.id == "primary":
            return AdapterResult(
                status="preexecution_failure",
                reason="account_limit",
                effects_started=False,
            )
        return AdapterResult(
            status="succeeded", text="backup answer",
            observed_model="relay-actual", cost_usd=0.2,
        )

    result = execute(
        RuntimeRequest(
            task_id="task-1",
            matter_id="mat-1",
            prompt="private prompt text",
            context="auxiliary_trusted",
            timeout_seconds=30,
        ),
        {"claude_cli": claude},
        root=tmp_path,
        config=_config(tmp_path),
        observer=lambda *_args: None,
    )

    assert result.status == "succeeded"
    assert result.route_id == "backup1"
    assert result.text == "backup answer"
    assert [item[0] for item in seen] == ["primary", "backup1"]
    row = recent(1)[0]
    assert row["task_id"] == "task-1"
    assert row["matter_id"] == "mat-1"
    assert row["selected_route"] == "backup1"
    assert row["observed_model"] == "relay-actual"
    assert "private prompt text" not in str(row)
    assert audit()["healthy"] is True


def test_transport_failure_replays_only_when_effects_are_safe(tmp_path):
    routes = []

    def adapter(route, _request, _model, _timeout):
        routes.append(route.id)
        if route.id == "primary":
            return AdapterResult(
                status="transport_failure", reason="network_error",
                effects_started=None,
            )
        return AdapterResult(status="succeeded", text="recovered")

    safe = execute(
        RuntimeRequest(task_id="safe", prompt="x", timeout_seconds=30),
        {"claude_cli": adapter},
        root=tmp_path,
        config=_config(tmp_path),
        observer=lambda *_args: None,
    )
    routes.clear()
    unsafe = execute(
        RuntimeRequest(
            task_id="unsafe", prompt="x", allow_tools=True,
            effect_authority="workspace_write", timeout_seconds=30,
        ),
        {"claude_cli": adapter},
        root=tmp_path,
        config=_config(tmp_path),
        observer=lambda *_args: None,
    )

    assert safe.status == "succeeded"
    assert unsafe.status == "ambiguous"
    assert unsafe.route_id == "primary"
    assert routes == ["primary"]


def test_ambiguous_network_failure_still_marks_provider_unhealthy(tmp_path):
    observed = []

    result = execute(
        RuntimeRequest(
            task_id="ambiguous-network",
            prompt="x",
            allow_tools=True,
            effect_authority="external",
        ),
        {"claude_cli": lambda *_args: AdapterResult(
            status="ambiguous_failure",
            reason="network_error",
            effects_started=None,
        )},
        root=tmp_path,
        config=_config(tmp_path),
        observer=lambda *args: observed.append(args),
    )

    assert result.status == "ambiguous"
    assert observed == [("primary", "unhealthy", "network_error")]
    assert [attempt.route_id for attempt in result.attempts] == ["primary"]


def test_preexecution_failure_can_fail_over_for_external_authority(tmp_path):
    routes = []

    def adapter(route, _request, _model, _timeout):
        routes.append(route.id)
        if route.id == "primary":
            return AdapterResult(
                status="preexecution_failure", reason="auth_error",
                effects_started=False,
            )
        return AdapterResult(status="succeeded", text="done")

    result = execute(
        RuntimeRequest(
            task_id="external", prompt="x", allow_tools=True,
            effect_authority="external", timeout_seconds=30,
        ),
        {"claude_cli": adapter},
        root=tmp_path,
        config=_config(tmp_path),
        observer=lambda *_args: None,
    )

    assert result.status == "succeeded"
    assert routes == ["primary", "backup1"]


def test_model_fallback_is_a_separate_receipted_attempt(tmp_path):
    models = []

    def adapter(_route, _request, model, _timeout):
        models.append(model)
        if model == "opus":
            return AdapterResult(
                status="preexecution_failure", reason="model_unavailable",
                next_model="sonnet", effects_started=False,
            )
        return AdapterResult(
            status="succeeded", text="smaller model", observed_model="sonnet"
        )

    result = execute(
        RuntimeRequest(
            task_id="tier-fallback", prompt="x", requested_model="opus"
        ),
        {"claude_cli": adapter},
        root=tmp_path,
        config=_config(tmp_path),
        observer=lambda *_args: None,
    )

    assert result.status == "succeeded"
    assert models == ["opus", "sonnet"]
    assert [item.attempt for item in result.attempts] == [1, 2]


def test_adapter_exception_is_replayed_only_for_effect_free_work(tmp_path):
    called = []

    def broken(route, *_args):
        called.append(route.id)
        raise RuntimeError("private provider detail")

    result = execute(
        RuntimeRequest(task_id="broken", prompt="secret"),
        {"claude_cli": broken},
        root=tmp_path,
        config=_config(tmp_path),
        observer=lambda *_args: None,
    )

    assert result.status == "failed"
    assert result.terminal_reason == "adapter_failure"
    assert called == ["primary", "backup1"]
    assert "private provider detail" not in str(result.public())


def test_request_validation_blocks_unattributed_or_mismatched_effects(tmp_path):
    adapters = {"claude_cli": lambda *_args: AdapterResult(
        status="succeeded", text="unused"
    )}

    for request, message in (
        (RuntimeRequest(task_id="", prompt="x"), "task_id"),
        (
            RuntimeRequest(
                task_id="x", prompt="x", effect_authority="external"
            ),
            "requires a tool-capable",
        ),
        (
            RuntimeRequest(
                task_id="x", prompt="x", requested_model="gpt\nsecret"
            ),
            "requested_model",
        ),
    ):
        try:
            execute(
                request, adapters, root=tmp_path,
                config=_config(tmp_path), observer=lambda *_args: None,
            )
        except ValueError as exc:
            assert message in str(exc)
        else:
            raise AssertionError("invalid request was accepted")


def test_no_eligible_adapter_is_a_valid_zero_attempt_receipt(tmp_path):
    result = execute(
        RuntimeRequest(task_id="no-adapter", prompt="x"),
        {},
        root=tmp_path,
        config=_config(tmp_path),
        observer=lambda *_args: None,
    )

    assert result.status == "failed"
    assert result.terminal_reason == "adapter_unavailable"
    assert result.attempts == ()
    assert audit()["healthy"] is True


def test_runtime_route_narrowing_is_explicit_ordered_and_validated(tmp_path):
    routes = []

    def adapter(route, _request, _model, _timeout):
        routes.append(route.id)
        return AdapterResult(status="succeeded", text="selected")

    result = execute(
        RuntimeRequest(
            task_id="narrowed",
            prompt="x",
            route_ids=("backup1", "openai"),
        ),
        {"claude_cli": adapter, "openai_responses": adapter},
        root=tmp_path,
        config=_config(tmp_path),
        observer=lambda *_args: None,
    )

    assert result.status == "succeeded"
    assert result.route_id == "backup1"
    assert routes == ["backup1"]

    for route_ids in (
        ("primary", "primary"),
        ("missing",),
        (object(),),
    ):
        try:
            RuntimeRequest(
                task_id="invalid", prompt="x", route_ids=route_ids,
            ).validate()
        except ValueError as exc:
            assert "route_ids" in str(exc)
        else:
            raise AssertionError("invalid route narrowing was accepted")


def test_next_model_cannot_bypass_ambiguous_effect_replay_gate(tmp_path):
    models = []

    def adapter(_route, _request, model, _timeout):
        models.append(model)
        return AdapterResult(
            status="ambiguous_failure",
            reason="request_failed",
            effects_started=None,
            next_model="sonnet",
        )

    result = execute(
        RuntimeRequest(
            task_id="effect-gate",
            prompt="write something",
            requested_model="opus",
            allow_tools=True,
            effect_authority="external",
        ),
        {"claude_cli": adapter},
        root=tmp_path,
        config=_config(tmp_path),
        observer=lambda *_args: None,
    )

    assert result.status == "ambiguous"
    assert models == ["opus"]


def test_untrusted_context_cannot_enable_tools(tmp_path):
    try:
        execute(
            RuntimeRequest(
                task_id="untrusted-tools",
                prompt="x",
                context="auxiliary_untrusted",
                allow_tools=True,
                effect_authority="external",
            ),
            {"claude_cli": lambda *_args: AdapterResult(
                status="succeeded", text="unused"
            )},
            root=tmp_path,
            config=_config(tmp_path),
            observer=lambda *_args: None,
        )
    except ValueError as exc:
        assert "does not permit tools" in str(exc)
    else:
        raise AssertionError("untrusted context received tools")


def test_cross_family_fallback_uses_the_route_model(tmp_path):
    seen = []

    def claude(route, _request, model, _timeout):
        seen.append((route.id, model))
        return AdapterResult(
            status="preexecution_failure",
            reason="account_limit",
            effects_started=False,
        )

    def openai(route, _request, model, _timeout):
        seen.append((route.id, model))
        return AdapterResult(
            status="succeeded", text="gpt answer", observed_model=model,
        )

    result = execute(
        RuntimeRequest(
            task_id="cross-family", prompt="x", requested_model="sonnet",
        ),
        {"claude_cli": claude, "openai_responses": openai},
        root=tmp_path,
        config=_config(tmp_path),
        observer=lambda *_args: None,
    )

    assert result.status == "succeeded"
    assert seen == [
        ("primary", "sonnet"),
        ("backup1", "sonnet"),
        ("openai", "gpt-test"),
    ]


def test_generic_opus_tier_uses_each_relay_configured_model(
    tmp_path, monkeypatch,
):
    seen = []
    monkeypatch.setenv("CLAUDE_BACKUP_MODEL", "custom-relay-model")

    def claude(route, _request, model, _timeout):
        seen.append((route.id, model))
        if route.id == "primary":
            return AdapterResult(
                status="preexecution_failure",
                reason="account_limit",
                effects_started=False,
            )
        return AdapterResult(status="succeeded", text="relay answer")

    result = execute(
        RuntimeRequest(
            task_id="generic-opus", prompt="x", requested_model="opus",
        ),
        {"claude_cli": claude},
        root=tmp_path,
        config=_config(tmp_path),
        observer=lambda *_args: None,
    )

    assert result.status == "succeeded"
    assert seen == [("primary", "opus"), ("backup1", "custom-relay-model")]


def test_observer_failure_cannot_leave_success_receipt_running(tmp_path):
    result = execute(
        RuntimeRequest(task_id="observer", prompt="x"),
        {"claude_cli": lambda *_args: AdapterResult(
            status="succeeded", text="done"
        )},
        root=tmp_path,
        config=_config(tmp_path),
        observer=lambda *_args: (_ for _ in ()).throw(RuntimeError("broken")),
    )

    assert result.status == "succeeded"
    assert recent(1)[0]["status"] == "succeeded"


def test_adapter_reason_is_sanitized_before_persistence(tmp_path):
    result = execute(
        RuntimeRequest(task_id="sanitize", prompt="x"),
        {"claude_cli": lambda *_args: AdapterResult(
            status="preexecution_failure",
            reason="Bearer private-token should not persist",
            effects_started=False,
        )},
        root=tmp_path,
        config=_config(tmp_path),
        observer=lambda *_args: None,
    )

    assert result.terminal_reason == "adapter_failure"
    assert all(item.reason == "adapter_failure" for item in result.attempts)


def test_receipt_digest_covers_system_prompt_without_persisting_it(tmp_path):
    from core.db import get_db

    def adapter(*_args):
        return AdapterResult(status="succeeded", text="done")

    for task_id, system_prompt in (
        ("digest-a", "private system a"),
        ("digest-b", "private system b"),
    ):
        execute(
            RuntimeRequest(
                task_id=task_id,
                prompt="same prompt",
                system_prompt=system_prompt,
            ),
            {"claude_cli": adapter},
            root=tmp_path,
            config=_config(tmp_path),
            observer=lambda *_args: None,
        )

    rows = get_db().execute(
        "SELECT prompt_digest FROM model_runtime_calls "
        "WHERE task_id IN ('digest-a','digest-b') ORDER BY task_id"
    ).fetchall()
    assert len(rows) == 2
    assert len({row["prompt_digest"] for row in rows}) == 2
    assert "private system" not in str([dict(row) for row in rows])


def test_contradictory_preexecution_result_fails_closed(tmp_path):
    routes = []

    def adapter(route, *_args):
        routes.append(route.id)
        return AdapterResult(
            status="preexecution_failure",
            reason="auth_error",
            effects_started=True,
        )

    result = execute(
        RuntimeRequest(
            task_id="contradictory",
            prompt="x",
            allow_tools=True,
            effect_authority="external",
        ),
        {"claude_cli": adapter},
        root=tmp_path,
        config=_config(tmp_path),
        observer=lambda *_args: None,
    )

    assert result.status == "ambiguous"
    assert result.terminal_reason == "adapter_failure"
    assert routes == ["primary"]


def test_cancelled_effectful_call_requires_reconciliation(tmp_path):
    result = execute(
        RuntimeRequest(
            task_id="cancelled-effect",
            prompt="x",
            allow_tools=True,
            effect_authority="workspace_write",
        ),
        {"claude_cli": lambda *_args: AdapterResult(
            status="cancelled",
            reason="cancelled",
            effects_started=None,
        )},
        root=tmp_path,
        config=_config(tmp_path),
        observer=lambda *_args: None,
    )

    assert result.status == "ambiguous"
    assert result.terminal_reason == "cancelled"
    assert len(result.attempts) == 1


def test_recovery_only_closes_stale_receipt_from_dead_executor(tmp_path):
    from core.db import get_db

    db = get_db()
    db.executemany(
        """INSERT INTO model_runtime_calls
           (id,task_id,context,effect_authority,prompt_digest,status,
            executor_pid,started_epoch)
           VALUES (?,?,'auxiliary_trusted','none','sha256:test','running',?,?)""",
        (
            ("dead-stale", "dead-stale", 101, 1.0),
            ("live-stale", "live-stale", 202, 1.0),
            ("dead-fresh", "dead-fresh", 303, 3900.0),
        ),
    )
    db.commit()

    recovered = recover_abandoned(
        now_epoch=4000.0,
        stale_after=1800,
        pid_alive=lambda pid: pid == 202,
    )

    assert recovered == ["dead-stale"]
    rows = {
        row["id"]: dict(row)
        for row in db.execute(
            "SELECT id,status,terminal_reason FROM model_runtime_calls"
        )
    }
    assert rows["dead-stale"]["status"] == "failed"
    assert rows["dead-stale"]["terminal_reason"] == "process_interrupted"
    assert rows["live-stale"]["status"] == "running"
    assert rows["dead-fresh"]["status"] == "running"
    assert audit(now_epoch=4000.0)["stale_running"] == [
        {
            "id": "live-stale",
            "task_id": "live-stale",
            "executor_pid": 202,
            "started_epoch": 1.0,
        }
    ]


def test_pid_probe_is_fail_closed_for_live_or_inaccessible_process(
    monkeypatch,
):
    assert model_runtime._pid_alive(0) is False

    monkeypatch.setattr(model_runtime.os, "kill", lambda _pid, _signal: None)
    assert model_runtime._pid_alive(101) is True

    def missing(_pid, _signal):
        raise ProcessLookupError

    monkeypatch.setattr(model_runtime.os, "kill", missing)
    assert model_runtime._pid_alive(202) is False

    def inaccessible(_pid, _signal):
        raise PermissionError

    monkeypatch.setattr(model_runtime.os, "kill", inaccessible)
    assert model_runtime._pid_alive(303) is True


def test_recovery_marks_interrupted_effectful_call_ambiguous(tmp_path):
    from core.db import get_db

    db = get_db()
    db.execute(
        """INSERT INTO model_runtime_calls
           (id,task_id,context,effect_authority,prompt_digest,status,
            executor_pid,started_epoch)
           VALUES ('effectful','effectful','owner_conversation','external',
                   'sha256:test','running',101,1)"""
    )
    db.commit()

    recovered = recover_abandoned(
        now_epoch=4000.0,
        stale_after=1800,
        pid_alive=lambda _pid: False,
    )
    assert "effectful" in recovered
    row = db.execute(
        "SELECT status,terminal_reason FROM model_runtime_calls "
        "WHERE id='effectful'"
    ).fetchone()
    assert tuple(row) == ("ambiguous", "process_interrupted")


def test_execute_recovers_abandoned_receipt_before_new_call(
    tmp_path, monkeypatch,
):
    from core.db import get_db

    db = get_db()
    db.execute(
        """INSERT INTO model_runtime_calls
           (id,task_id,context,effect_authority,prompt_digest,status,
            executor_pid,started_epoch)
           VALUES ('abandoned','old','auxiliary_trusted','none',
                   'sha256:test','running',99999999,1)"""
    )
    db.commit()
    monkeypatch.setattr("core.model_runtime._pid_alive", lambda _pid: False)

    result = execute(
        RuntimeRequest(task_id="new", prompt="x"),
        {"claude_cli": lambda *_args: AdapterResult(
            status="succeeded", text="done"
        )},
        root=tmp_path,
        config=_config(tmp_path),
        observer=lambda *_args: None,
    )

    assert result.status == "succeeded"
    old = db.execute(
        "SELECT status,terminal_reason FROM model_runtime_calls "
        "WHERE id='abandoned'"
    ).fetchone()
    assert tuple(old) == ("failed", "process_interrupted")
