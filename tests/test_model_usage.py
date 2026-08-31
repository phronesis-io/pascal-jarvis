"""Unified model package usage, forecast, and privacy tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import core.db as db_module
from core.model_usage import (
    build_report,
    human_time,
    read_codex_rate_limits,
    status_text,
)


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "jarvis.db")
    if db_module._connection is not None:
        db_module._connection.close()
    db_module._connection = None
    yield
    if db_module._connection is not None:
        db_module._connection.close()
    db_module._connection = None


def _payload(used: float, *, resets_at: float = 10_000) -> dict:
    return {
        "rateLimits": {
            "limitId": "codex", "limitName": None,
            "primary": {
                "usedPercent": used, "windowDurationMins": 10080,
                "resetsAt": resets_at,
            },
            "secondary": None,
            "credits": {"hasCredits": False, "unlimited": False, "balance": "0"},
            "planType": "pro", "rateLimitReachedType": None,
            "spendControlReached": False,
        },
        "rateLimitsByLimitId": {},
        "rateLimitResetCredits": {
            "availableCount": 1,
            "credits": [{
                "id": "secret-opaque-credit-id", "expiresAt": 9000,
                "title": "Full reset", "description": "backend prose",
            }],
        },
    }


def _config(root: Path) -> None:
    (root / "jarvis.yaml").write_text(
        "data_dir: " + str(root / "data") + "\n"
        "claude:\n  main_model: opus\n"
        "codex:\n  fallback_enabled: true\n",
        encoding="utf-8",
    )


def test_codex_app_server_read_waits_for_initialize_and_returns_snapshot(tmp_path):
    binary = tmp_path / "codex"
    binary.write_text(
        """#!/usr/bin/env python3
import json, sys
for line in sys.stdin:
    request = json.loads(line)
    if request.get("id") == 1:
        print(json.dumps({"id": 1, "result": {"userAgent": "fake"}}), flush=True)
    elif request.get("id") == 2:
        print(json.dumps({"id": 2, "result": {"rateLimits": {"limitId": "codex"}}}), flush=True)
""",
        encoding="utf-8",
    )
    binary.chmod(0o755)

    assert read_codex_rate_limits(str(binary))["rateLimits"]["limitId"] == "codex"


def test_report_keeps_exact_usage_separate_from_unknown_routes(tmp_path):
    _config(tmp_path)
    report = build_report(
        tmp_path, now=1000,
        codex_reader=lambda: _payload(46),
        claude_reader=lambda: {
            "logged_in": True, "subscription_type": "max",
            "auth_method": "claude.ai", "usage_evidence": "unknown",
        },
    )

    assert report["codex"]["source"] == "codex_app_server"
    assert report["codex"]["windows"][0]["used_percent"] == 46
    assert report["codex"]["windows"][0]["window_label"] == "7 天"
    assert report["claude_account"]["subscription_type"] == "max"
    by_id = {row["id"]: row for row in report["routes"]}
    assert by_id["codex"]["quota_evidence"] == "exact"
    assert by_id["openai"]["quota_evidence"] == "unknown"
    assert by_id["openai"]["owner_label"] == "GPT 备用通道"
    assert report["fallback_labels"][0] == "Claude 主通道"
    encoded = json.dumps(report, ensure_ascii=False)
    assert "secret-opaque-credit-id" not in encoded
    assert "backend prose" not in encoded
    assert (tmp_path / "data" / "model_usage_latest.json").stat().st_mode & 0o777 == 0o600


def test_owner_usage_copy_uses_human_time_and_route_names(tmp_path):
    _config(tmp_path)
    report = build_report(
        tmp_path, now=1000,
        codex_reader=lambda: _payload(46),
        claude_reader=lambda: {},
    )

    rendered = status_text(report)

    assert human_time("2026-09-01T10:00:00+08:00") == "9月1日 10:00"
    assert "Claude 主通道" in rendered
    assert "->" not in rendered
    assert "T10:00" not in rendered


def test_usage_history_predicts_exhaustion_before_reset(tmp_path):
    _config(tmp_path)
    build_report(
        tmp_path, now=1000, codex_reader=lambda: _payload(80),
        claude_reader=lambda: {},
    )
    report = build_report(
        tmp_path, now=1600, codex_reader=lambda: _payload(90),
        claude_reader=lambda: {},
    )
    window = report["codex"]["windows"][0]
    assert window["predicted_exhaustion_epoch"] == pytest.approx(2200)
    assert window["risk"] == "critical"
    assert report["issues"][0]["code"] == "codex_critical"


def test_low_early_usage_forecast_never_becomes_an_alert(tmp_path):
    _config(tmp_path)
    reset = 1000 + 6 * 86400
    build_report(
        tmp_path, now=1000,
        codex_reader=lambda: _payload(5, resets_at=reset),
        claude_reader=lambda: {},
    )
    report = build_report(
        tmp_path, now=1600,
        codex_reader=lambda: _payload(7, resets_at=reset),
        claude_reader=lambda: {},
    )

    window = report["codex"]["windows"][0]
    assert window["predicted_exhaustion_epoch"] == 0
    assert window["risk"] == "ok"
    assert report["issues"] == []


def test_august_audit_observations_at_twenty_three_percent_stay_quiet(tmp_path):
    _config(tmp_path)
    reset = 1_788_485_553.0
    build_report(
        tmp_path, now=1_787_902_166.0,
        codex_reader=lambda: _payload(10, resets_at=reset),
        claude_reader=lambda: {},
    )
    report = build_report(
        tmp_path, now=1_787_912_977.0,
        codex_reader=lambda: _payload(23, resets_at=reset),
        claude_reader=lambda: {},
    )

    window = report["codex"]["windows"][0]
    assert window["risk"] == "ok"
    assert window["predicted_exhaustion_epoch"] == 0
    assert report["issues"] == []


def test_mature_low_usage_prediction_is_informational_only(tmp_path):
    _config(tmp_path)
    reset = 8 * 86400
    build_report(
        tmp_path, now=3 * 86400,
        codex_reader=lambda: _payload(5, resets_at=reset),
        claude_reader=lambda: {},
    )
    report = build_report(
        tmp_path, now=3 * 86400 + 600,
        codex_reader=lambda: _payload(7, resets_at=reset),
        claude_reader=lambda: {},
    )

    window = report["codex"]["windows"][0]
    assert window["predicted_exhaustion_epoch"] > 0
    assert window["risk"] == "ok"
    assert report["issues"] == []


def test_unavailable_quota_is_labeled_unknown_not_healthy_or_remaining(tmp_path):
    _config(tmp_path)
    report = build_report(
        tmp_path, now=1000,
        codex_reader=lambda: (_ for _ in ()).throw(RuntimeError("offline")),
        claude_reader=lambda: {
            "logged_in": True, "subscription_type": "max",
            "auth_method": "claude.ai", "usage_evidence": "unknown",
        },
    )
    assert report["codex"]["source"] == "unknown"
    assert report["codex"]["windows"] == []
    rendered = status_text(report)
    assert "暂时读不到套餐窗口" in rendered
    assert "官方 CLI 暂未提供剩余额度数字" in rendered
    assert "还有额度" not in rendered


def test_claude_account_failure_does_not_hide_codex_usage(tmp_path):
    _config(tmp_path)
    report = build_report(
        tmp_path, now=1000,
        codex_reader=lambda: _payload(46),
        claude_reader=lambda: (_ for _ in ()).throw(RuntimeError("offline")),
    )

    assert report["codex"]["windows"][0]["used_percent"] == 46
    assert report["claude_account"]["subscription_type"] == "unknown"
    by_id = {row["id"]: row for row in report["routes"]}
    assert by_id["primary"]["quota_evidence"] == "unknown"
