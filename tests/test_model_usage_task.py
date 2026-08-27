"""Model package refresh is deterministic, stateful, and quiet."""

from __future__ import annotations

import json
import sys
from io import StringIO

from tasks import model_usage_post, model_usage_pre
from tasks.model_usage_post import render_usage_alert


def _report(*, issues):
    return {
        "schema": "jarvis.model-usage.v1",
        "observed_at": "2026-08-27T18:00+08:00",
        "fallback_order": ["primary", "codex", "openai"],
        "issues": issues,
    }


def test_normal_usage_stays_silent(tmp_path):
    state = tmp_path / "alert.json"
    assert render_usage_alert(_report(issues=[]), state_path=state) == ""
    assert not state.exists()


def test_critical_usage_alerts_once_with_reset_and_fallback(tmp_path):
    state = tmp_path / "alert.json"
    report = _report(issues=[{
        "code": "codex_critical", "route_id": "codex",
        "limit_id": "codex", "window_name": "primary", "window_label": "7 天",
        "used_percent": 93, "resets_at": "2026-09-01T10:00+08:00",
        "predicted_exhaustion_at": "2026-08-30T12:00+08:00",
    }])
    first = render_usage_alert(report, state_path=state)
    assert "7 天额度" in first
    assert "已用 93%，还剩约 7%" in first
    assert "预计 2026-08-30T12:00+08:00 用尽" in first
    assert "primary -> codex -> openai" in first
    assert render_usage_alert(report, state_path=state) == ""
    assert state.stat().st_mode & 0o777 == 0o600


def test_recovery_rearms_a_later_episode(tmp_path):
    state = tmp_path / "alert.json"
    issue = {"code": "provider_account_limited", "route_id": "primary"}
    assert render_usage_alert(_report(issues=[issue]), state_path=state)
    assert render_usage_alert(_report(issues=[]), state_path=state) == ""
    assert json.loads(state.read_text())["status"] == "clear"
    assert render_usage_alert(_report(issues=[issue]), state_path=state)


def test_critical_to_exhausted_is_one_episode_but_new_reset_rearms(tmp_path):
    state = tmp_path / "alert.json"
    issue = {
        "code": "codex_critical", "route_id": "codex",
        "limit_id": "codex", "window_name": "primary",
        "window_label": "7 天", "used_percent": 93,
        "resets_at": "2026-09-01T10:00+08:00",
    }
    assert render_usage_alert(_report(issues=[issue]), state_path=state)
    exhausted = {**issue, "code": "codex_exhausted", "used_percent": 100}
    assert render_usage_alert(_report(issues=[exhausted]), state_path=state) == ""
    next_window = {**exhausted, "resets_at": "2026-09-08T10:00+08:00"}
    assert render_usage_alert(_report(issues=[next_window]), state_path=state)


def test_recovery_of_one_issue_does_not_repeat_an_existing_issue(tmp_path):
    state = tmp_path / "alert.json"
    primary = {"code": "provider_account_limited", "route_id": "primary"}
    backup = {"code": "provider_account_limited", "route_id": "backup1"}
    assert render_usage_alert(_report(issues=[primary, backup]), state_path=state)
    assert render_usage_alert(_report(issues=[primary]), state_path=state) == ""
    saved = json.loads(state.read_text())
    assert len(saved["open_keys"]) == 1


def test_tier0_pre_and_post_exchange_the_report_schema(
        tmp_path, monkeypatch, capsys):
    report = _report(issues=[])
    monkeypatch.setattr(model_usage_pre, "build_report", lambda: report)
    assert model_usage_pre.main() == 0
    encoded = capsys.readouterr().out
    assert json.loads(encoded)["schema"] == "jarvis.model-usage.v1"

    monkeypatch.setenv("JARVIS_DIR", str(tmp_path))
    monkeypatch.setattr(sys, "stdin", StringIO(encoded))
    assert model_usage_post.main() == 0
    assert capsys.readouterr().out == ""
