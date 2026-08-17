import json
import subprocess
from pathlib import Path

import pytest

from dashboard import telemetry
from dashboard.pages.eigenflux import (
    create_official_dashboard_link,
    load_network_overview,
)


def _write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def test_network_overview_reads_real_local_contracts(tmp_path):
    root = tmp_path / "jarvis"
    home = tmp_path / "eigenflux-home"
    _write(
        home / "config.json",
        {"kv": {"recurring_publish": "true", "auto_comment": True}},
    )
    _write(
        root / "eigenflux" / "pending_publish" / "123_456.json",
        {
            "id": "123_456",
            "content": "candidate",
            "created_at": 100,
            "notes": {"summary": "draft"},
        },
    )
    _write(
        root / "eigenflux" / "publish_state.json",
        {
            "recent": [
                {"epoch": 10, "summary": "older"},
                {"epoch": 20, "summary": "newer"},
            ]
        },
    )
    _write(
        root / "heartbeat_state.json",
        {
            "eigenflux-feed-triage": {
                "last_status": "ok",
                "last_success": 100,
                "circuit": {"disabled_until": 0},
            },
            "eigenflux-publish": {
                "last_status": "empty_pre",
                "last_success": 100,
                "circuit": {"disabled_until": 0},
            },
        },
    )
    telemetry.reset_cache()

    overview = load_network_overview(
        root, eigenflux_home=home, now_epoch=200
    )

    assert overview["recurring_publish"] is True
    assert overview["auto_comment"] is True
    assert [item["id"] for item in overview["pending"]] == ["123_456"]
    assert [item["summary"] for item in overview["recent"]] == [
        "newer",
        "older",
    ]
    task_by_id = {item["id"]: item for item in overview["tasks"]}
    assert task_by_id["eigenflux-feed-triage"]["healthy"] is True
    assert task_by_id["eigenflux-publish"]["healthy"] is True
    assert task_by_id["eigenflux-profile"]["healthy"] is False


def test_network_overview_marks_stale_success_unhealthy(tmp_path):
    root = tmp_path / "jarvis"
    _write(
        root / "heartbeat_state.json",
        {
            "eigenflux-feed-triage": {
                "last_status": "ok",
                "last_success": 100,
                "circuit": {"disabled_until": 0},
            },
        },
    )
    telemetry.reset_cache()

    overview = load_network_overview(root, now_epoch=100 + 31 * 60)

    task = next(
        item for item in overview["tasks"]
        if item["id"] == "eigenflux-feed-triage"
    )
    assert task["healthy"] is False
    assert task["detail"] == "超过应有周期未成功"


def test_network_overview_reports_realtime_stream_separately(tmp_path):
    root = tmp_path / "jarvis"
    _write(
        root / "data" / "ef_stream_health.json",
        {
            "status": "degraded",
            "updated_epoch": 100,
            "quiet_streak": 6,
            "detail": "no protocol output",
        },
    )
    telemetry.reset_cache()

    overview = load_network_overview(root, now_epoch=200)

    assert overview["stream"]["healthy"] is False
    assert overview["stream"]["status"] == "degraded"
    assert overview["stream"]["quiet_streak"] == 6

    _write(
        root / "data" / "ef_stream_health.json",
        {
            "status": "connecting",
            "updated_epoch": 200,
            "quiet_streak": 0,
            "detail": "protocol not yet verified",
        },
    )
    telemetry.reset_cache()
    overview = load_network_overview(root, now_epoch=201)
    assert overview["stream"]["healthy"] is False
    assert overview["stream"]["status"] == "connecting"


def test_network_overview_uses_content_overrides_but_ignores_infra_drift(
        tmp_path):
    root = tmp_path / "jarvis"
    now = 500_000
    _write(
        root / "heartbeat_state.json",
        {
            "eigenflux-feed-triage": {
                "last_status": "empty_pre",
                "last_success": now - 31 * 60,
                "circuit": {"disabled_until": 0},
            },
            "eigenflux-preinstall": {
                "last_status": "idle",
                "last_success": now - 40 * 3600,
                "circuit": {"disabled_until": 0},
            },
            "eigenflux-friends": {
                "last_status": "ok",
                "last_success": now - 31 * 60,
                "effective_interval": 40 * 60,
                "circuit": {"disabled_until": 0},
            },
        },
    )
    _write(
        root / "interval_overrides.json",
        {
            "eigenflux-feed-triage": 40 * 60,
            "eigenflux-preinstall": 48 * 3600,
        },
    )
    telemetry.reset_cache()

    overview = load_network_overview(root, now_epoch=now)
    task_by_id = {item["id"]: item for item in overview["tasks"]}

    assert task_by_id["eigenflux-feed-triage"]["healthy"] is True
    assert task_by_id["eigenflux-preinstall"]["healthy"] is True
    assert task_by_id["eigenflux-preinstall"]["detail"] == "正常"
    assert task_by_id["eigenflux-friends"]["healthy"] is False

    stale = load_network_overview(root, now_epoch=now + 50 * 60)
    stale_by_id = {item["id"]: item for item in stale["tasks"]}
    assert stale_by_id["eigenflux-feed-triage"]["healthy"] is False
    assert stale_by_id["eigenflux-friends"]["healthy"] is False

    much_later = load_network_overview(root, now_epoch=now + 9 * 3600)
    much_later_by_id = {item["id"]: item for item in much_later["tasks"]}
    assert much_later_by_id["eigenflux-preinstall"]["healthy"] is False


def test_network_overview_rejects_entire_invalid_override_sidecar(tmp_path):
    root = tmp_path / "jarvis"
    _write(
        root / "heartbeat_state.json",
        {
            "eigenflux-feed-triage": {
                "last_status": "ok",
                "last_success": 100,
                "circuit": {"disabled_until": 0},
            },
        },
    )
    _write(
        root / "interval_overrides.json",
        {
            "eigenflux-feed-triage": 40 * 60,
            "unrelated-broken-task": "soon",
        },
    )
    telemetry.reset_cache()

    overview = load_network_overview(root, now_epoch=100 + 31 * 60)
    task = next(
        item for item in overview["tasks"]
        if item["id"] == "eigenflux-feed-triage"
    )

    assert task["healthy"] is False
    assert task["detail"] == "超过应有周期未成功"


def test_dashboard_link_accepts_json_or_plain_output(monkeypatch):
    result = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout='{"url":"https://www.eigenflux.ai/dashboard/session/abc"}',
        stderr="",
    )
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: result)
    assert create_official_dashboard_link().endswith("/session/abc")


def test_dashboard_link_fails_closed(monkeypatch):
    result = subprocess.CompletedProcess(
        args=[], returncode=1, stdout="", stderr="auth required"
    )
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: result)
    with pytest.raises(RuntimeError, match="auth required"):
        create_official_dashboard_link()
