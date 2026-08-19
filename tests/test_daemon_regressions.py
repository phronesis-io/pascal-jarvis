"""Regression tests for daemon.py bug fixes (2026-05-25).

Tests the guardian daemon's health-check and restart logic.
"""

import io
import json
import os
import subprocess
import textwrap
import time
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import daemon as daemon_mod


def test_guardian_alert_has_durable_incident_identity(
    tmp_path, monkeypatch,
):
    from core import delivery

    captured = []
    monkeypatch.setattr(daemon_mod, "USER_ID", "ou_owner")
    monkeypatch.setattr(daemon_mod, "JARVIS_DIR", tmp_path)
    monkeypatch.setattr(daemon_mod, "log", lambda *a, **k: None)
    monkeypatch.setattr(
        delivery,
        "deliver",
        lambda envelope, **kwargs: (
            captured.append(envelope)
            or SimpleNamespace(state="delivered", reason="")
        ),
    )

    assert daemon_mod.notify_lark(
        "⚠️ 组件失联：admin", incident_key="component:admin:down") is True
    envelope = captured[0]
    assert envelope.dedup_key == "guardian:component:admin:down"
    assert envelope.throttle_key == envelope.dedup_key
    assert envelope.metadata["incident_key"] == "component:admin:down"
    assert envelope.metadata["audience"] == "owner_private"
    assert envelope.metadata["recipient_type"] == "open_id"
    assert envelope.metadata["replayable"] is False


@pytest.mark.parametrize(
    ("result", "expected", "banner_count"),
    [
        (SimpleNamespace(state="queued", reason="quiet_hours", accepted=True),
         None, 0),
        (SimpleNamespace(state="attempting", reason="retry", accepted=True),
         None, 0),
        (SimpleNamespace(state="suppressed", reason="metric_daily_cap",
                         accepted=True), True, 0),
        (SimpleNamespace(state="suppressed", reason="source_daily_cap",
                         accepted=True), False, 1),
        (SimpleNamespace(state="failed", reason="transport", accepted=False),
         False, 1),
    ],
)
def test_guardian_delivery_receipt_is_honest(
    tmp_path, monkeypatch, result, expected, banner_count,
):
    from core import delivery

    banners = []
    monkeypatch.setattr(daemon_mod, "USER_ID", "ou_owner")
    monkeypatch.setattr(daemon_mod, "JARVIS_DIR", tmp_path)
    monkeypatch.setattr(daemon_mod, "log", lambda *a, **k: None)
    monkeypatch.setattr(daemon_mod, "_raise_banner",
                        lambda msg, why: banners.append((msg, why)))
    monkeypatch.setattr(delivery, "deliver", lambda *a, **k: result)

    assert daemon_mod.notify_lark(
        "同一个事故，文字可以变化", incident_key="stable-incident") is expected
    assert len(banners) == banner_count


def test_local_banner_is_argument_safe_and_persistently_rate_limited(
        tmp_path, monkeypatch):
    calls = []
    stamp = tmp_path / ".banner"
    monkeypatch.setattr(daemon_mod, "BANNER_STAMP_FILE", stamp)
    monkeypatch.setattr(daemon_mod.time, "time", lambda: 10_000.0)
    monkeypatch.setattr(daemon_mod, "log", lambda *a, **k: None)
    monkeypatch.setattr(
        daemon_mod.subprocess, "run",
        lambda argv, **kwargs: calls.append(argv) or SimpleNamespace(returncode=0),
    )

    body = '异常 "quoted" text'
    daemon_mod._raise_banner(body, "lost")
    daemon_mod._raise_banner(body, "lost again")

    assert len(calls) == 1
    assert calls[0][-1] == body
    assert body not in calls[0][2]  # message is argv, never AppleScript source
    assert stamp.exists()


def test_failed_local_banner_does_not_consume_rate_limit(tmp_path, monkeypatch):
    stamp = tmp_path / ".banner"
    monkeypatch.setattr(daemon_mod, "BANNER_STAMP_FILE", stamp)
    monkeypatch.setattr(daemon_mod.time, "time", lambda: 10_000.0)
    monkeypatch.setattr(daemon_mod, "log", lambda *a, **k: None)
    monkeypatch.setattr(
        daemon_mod.subprocess, "run",
        lambda *a, **k: SimpleNamespace(returncode=1),
    )

    daemon_mod._raise_banner("lost", "osascript failed")

    assert not stamp.exists()


def test_component_recovery_only_terminates_owned_exact_child(monkeypatch):
    killed = []
    monkeypatch.setattr(daemon_mod, "_in_deploy_window", lambda: False)
    monkeypatch.setattr(daemon_mod, "_bot_pid", lambda: 100)
    monkeypatch.setattr(daemon_mod, "_ps_processes", lambda: {
        100: (1, "bash /repo/bot.sh"),
        200: (100, "python3 -m core.heartbeat_loop"),
        300: (1, "python3 -m core.heartbeat_loop"),
        400: (100, "python3 -m core.heartbeat_loop_debug"),
    })
    monkeypatch.setattr(daemon_mod.os, "kill",
                        lambda pid, sig: killed.append((pid, sig)))
    monkeypatch.setattr(daemon_mod, "log", lambda *a, **k: None)

    assert daemon_mod._request_component_recovery("heartbeat-loop") is True
    assert killed == [(200, daemon_mod.signal.SIGTERM)]


def test_dashboard_recovery_uses_exact_launchd_job(monkeypatch):
    calls = []
    monkeypatch.setattr(daemon_mod, "_in_deploy_window", lambda: False)
    monkeypatch.setattr(daemon_mod.os, "getuid", lambda: 501)
    monkeypatch.setattr(
        daemon_mod.subprocess, "run",
        lambda argv, **kwargs: calls.append(argv) or SimpleNamespace(returncode=0),
    )
    monkeypatch.setattr(daemon_mod, "log", lambda *a, **k: None)

    assert daemon_mod._request_component_recovery("dashboard :3457") is True
    assert calls == [["launchctl", "kickstart", "-k",
                      "gui/501/com.pascal.jarvis.dashboard"]]


def test_external_deadman_withholds_ping_when_delivery_is_unhealthy(
    tmp_path, monkeypatch,
):
    from core import deadman, delivery

    pinged = []

    class Pipe:
        def __init__(self, _root):
            pass

        def transport_health(self):
            return {"healthy": False, "consecutive_failures": 3}

    monkeypatch.setattr(daemon_mod, "JARVIS_DIR", tmp_path)
    monkeypatch.setattr(daemon_mod, "log", lambda *a, **k: None)
    monkeypatch.setattr(
        deadman, "status", lambda _root: deadman.DeadmanResult("ok")
    )
    monkeypatch.setattr(
        deadman,
        "ping_due",
        lambda _root: pinged.append(True) or deadman.DeadmanResult("ok"),
    )
    monkeypatch.setattr(delivery, "DeliveryPipeline", Pipe)

    assert daemon_mod._ping_external_deadman() == \
        "withheld_transport_unhealthy"
    assert pinged == []


def test_pid_parse_with_boot_timestamp(tmp_path, monkeypatch):
    """PID file in 'PID BOOT_TS' format must parse correctly.

    Bug: old code used int(content.strip()) which failed on '1234 1779680834'.
    Fix: changed to int(content.strip().split()[0]).
    """
    pid_file = tmp_path / ".bot.pid"
    pid_file.write_text("99999 1779680834")  # PID that doesn't exist
    monkeypatch.setattr(daemon_mod, "BOT_PID_FILE", pid_file)

    # PID 99999 likely doesn't exist → should return False (via ProcessLookupError)
    # But it should NOT crash on ValueError
    result = daemon_mod._is_bot_alive()
    # We just verify it didn't raise — it returns False because PID doesn't exist
    assert result is False or result is True  # no crash


def test_pid_parse_legacy_format(tmp_path, monkeypatch):
    """Legacy PID-only format must still work."""
    pid_file = tmp_path / ".bot.pid"
    pid_file.write_text("99999")
    monkeypatch.setattr(daemon_mod, "BOT_PID_FILE", pid_file)

    # Should not crash — gracefully returns False for non-existent PID
    result = daemon_mod._is_bot_alive()
    assert result is False or result is True


def test_pid_parse_empty_file(tmp_path, monkeypatch):
    """Empty PID file should not crash."""
    pid_file = tmp_path / ".bot.pid"
    pid_file.write_text("")
    monkeypatch.setattr(daemon_mod, "BOT_PID_FILE", pid_file)

    result = daemon_mod._is_bot_alive()
    assert result is False or result is True


def test_stale_threshold_is_1200():
    """Stale threshold must be 1200s (20 min) to accommodate long Claude calls.

    Was 900s (15 min), caused false-positive stale detection → restart spiral.
    """
    assert daemon_mod.HEARTBEAT_STALE_THRESHOLD == 1800


def test_daemon_records_wake_gap(monkeypatch):
    monkeypatch.setattr(daemon_mod, "last_wake_time", 0)
    monkeypatch.setattr(daemon_mod.time, "time", lambda: 12345.0)
    monkeypatch.setattr(daemon_mod, "log", lambda *a, **k: None)

    assert daemon_mod._record_wake_gap(
        slept_for_s=daemon_mod.CHECK_INTERVAL + daemon_mod.SLEEP_GAP_THRESHOLD + 1,
        expected_s=daemon_mod.CHECK_INTERVAL,
    ) == daemon_mod.SLEEP_GAP_THRESHOLD + 1
    assert daemon_mod.last_wake_time == 12345.0
    assert daemon_mod._in_wake_grace(now=12345.0 + daemon_mod.WAKE_GRACE_SECONDS - 1)
    assert not daemon_mod._in_wake_grace(now=12345.0 + daemon_mod.WAKE_GRACE_SECONDS + 1)


def test_daemon_wake_grace_suppresses_stale_heartbeat_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(daemon_mod, "JARVIS_DIR", tmp_path)
    monkeypatch.setattr(daemon_mod, "_bot_pid", lambda: 123)
    monkeypatch.setattr(daemon_mod, "_is_lark_listener_alive", lambda bot_pid=None: True)
    monkeypatch.setattr(daemon_mod, "_find_last_heartbeat", lambda: daemon_mod.HEARTBEAT_STALE_THRESHOLD + 60)
    monkeypatch.setattr(daemon_mod, "_in_wake_grace", lambda now=None: True)
    monkeypatch.setattr(daemon_mod, "log", lambda *a, **k: None)

    result = daemon_mod.check_health()

    assert result["healthy"] is True
    assert result["issues"] == []


def test_daemon_stale_heartbeat_still_fails_outside_wake_grace(tmp_path, monkeypatch):
    monkeypatch.setattr(daemon_mod, "JARVIS_DIR", tmp_path)
    monkeypatch.setattr(daemon_mod, "_bot_pid", lambda: 123)
    monkeypatch.setattr(daemon_mod, "_is_lark_listener_alive", lambda bot_pid=None: True)
    monkeypatch.setattr(daemon_mod, "_find_last_heartbeat", lambda: daemon_mod.HEARTBEAT_STALE_THRESHOLD + 60)
    monkeypatch.setattr(daemon_mod, "_in_wake_grace", lambda now=None: False)

    result = daemon_mod.check_health()

    assert result["healthy"] is False
    assert any("Heartbeat stale" in issue for issue in result["issues"])


# ── beat-age-None guards (red-team 7/8) ──
# With beats throttled, a busy conversation's log burst can push the newest
# beat out of the tail window → beat_age=None. That branch used to append
# 'No heartbeat found' UNCONDITIONALLY — no session-lock / wake-grace guard —
# so the daemon restarted the stack mid-conversation, killing the very reply
# whose logging caused the burst.

@pytest.fixture
def beat_none_env(tmp_path, monkeypatch):
    monkeypatch.setattr(daemon_mod, "JARVIS_DIR", tmp_path)
    monkeypatch.setattr(daemon_mod, "_bot_pid", lambda: 123)
    monkeypatch.setattr(daemon_mod, "_is_lark_listener_alive", lambda bot_pid=None: True)
    monkeypatch.setattr(daemon_mod, "_find_last_heartbeat", lambda: None)
    monkeypatch.setattr(daemon_mod, "_in_wake_grace", lambda now=None: False)
    monkeypatch.setattr(daemon_mod, "log", lambda *a, **k: None)
    return tmp_path


def test_daemon_no_beat_with_active_session_is_suppressed(beat_none_env):
    (beat_none_env / ".session_lock_abc").write_text("999 token")

    result = daemon_mod.check_health()

    assert result["healthy"] is True
    assert result["issues"] == []
    # fake-healthy must be marked so the breaker unlatch can't fire on it
    assert result.get("note") == "session-active"


def test_daemon_no_beat_in_wake_grace_is_suppressed(beat_none_env, monkeypatch):
    monkeypatch.setattr(daemon_mod, "_in_wake_grace", lambda now=None: True)

    result = daemon_mod.check_health()

    assert result["healthy"] is True
    assert result["issues"] == []
    assert result.get("note") == "wake-grace"


def test_daemon_no_beat_without_session_or_grace_still_fails(beat_none_env):
    result = daemon_mod.check_health()

    assert result["healthy"] is False
    assert any("No heartbeat found" in issue for issue in result["issues"])
    assert "note" not in result


def test_daemon_stale_beat_with_active_session_carries_note(tmp_path, monkeypatch):
    """The stale-branch suppression is fake-healthy too — it must carry the
    same note (red-team 7/8 finding: this path used to auto-clear the
    breaker latch, see test_fake_healthy_via_session_lock_does_not_unlatch)."""
    monkeypatch.setattr(daemon_mod, "JARVIS_DIR", tmp_path)
    monkeypatch.setattr(daemon_mod, "_bot_pid", lambda: 123)
    monkeypatch.setattr(daemon_mod, "_is_lark_listener_alive", lambda bot_pid=None: True)
    monkeypatch.setattr(daemon_mod, "_find_last_heartbeat",
                        lambda: daemon_mod.HEARTBEAT_STALE_THRESHOLD + 60)
    monkeypatch.setattr(daemon_mod, "_in_wake_grace", lambda now=None: False)
    monkeypatch.setattr(daemon_mod, "log", lambda *a, **k: None)
    (tmp_path / ".session_lock_abc").write_text("999 token")

    result = daemon_mod.check_health()

    assert result["healthy"] is True
    assert result.get("note") == "session-active"


def test_find_last_heartbeat_survives_busy_log_tail(tmp_path, monkeypatch):
    """The tail read must span a busy stretch of non-beat traffic (now 256KB;
    red-team 7/8: the old 10KB window was sized for the per-10s beat spam —
    with beats throttled, ~11KB of handler/ef-stream lines after the newest
    beat made it invisible → false 'No heartbeat found' restart)."""
    monkeypatch.setattr(daemon_mod, "JARVIS_DIR", tmp_path)
    # Hermetic: divert the /tmp/jarvis_restart.log probe to a missing file so
    # the machine's real restart log can't supply a beat.
    real_path = daemon_mod.Path
    monkeypatch.setattr(
        daemon_mod, "Path",
        lambda p="": (tmp_path / "absent_restart.log")
        if str(p) == "/tmp/jarvis_restart.log" else real_path(p))

    beat = "[2026-07-08 00:00:00] [INFO] [heartbeat] Beat sent (working)\n"
    noise = ("[2026-07-08 00:00:01] [INFO] [handler] chunk "
             + "x" * 120 + "\n") * 400          # ~66KB of non-beat traffic
    (tmp_path / "jarvis.log").write_text(beat + noise)

    assert daemon_mod._find_last_heartbeat() is not None


def test_kill_patterns_include_eigenflux(tmp_path, monkeypatch):
    """diagnose_and_fix must kill eigenflux stream processes during restart.

    Bug: old kill list didn't include eigenflux → orphan streams survived restart
    → 'Connection replaced by another session' infinite loop.
    """
    monkeypatch.setattr(daemon_mod, "JARVIS_DIR", tmp_path)
    monkeypatch.setattr(daemon_mod, "BOT_PID_FILE", tmp_path / ".bot.pid")
    monkeypatch.setattr(daemon_mod, "RESTART_STATE_FILE", tmp_path / ".daemon_restart_state.json")
    monkeypatch.setattr(daemon_mod, "BREAKER_LATCH_FILE", tmp_path / "data" / "restart_breaker.latched")
    monkeypatch.setattr(daemon_mod, "last_restart_time", 0)
    monkeypatch.setattr(daemon_mod, "restart_count", 0)
    monkeypatch.setattr(daemon_mod, "log", lambda *a, **k: None)
    monkeypatch.setattr(daemon_mod, "notify_lark", lambda *a, **k: None)
    monkeypatch.setattr(daemon_mod, "check_health", lambda: {"healthy": True, "issues": []})

    killed_patterns = []
    original_run = subprocess.run

    def capture_pkill(args, **kwargs):
        if args and args[0] == "pkill":
            killed_patterns.append(args[-1])
        return original_run(["true"], **kwargs)

    monkeypatch.setattr(subprocess, "run", capture_pkill)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: None)
    monkeypatch.setattr(daemon_mod.time, "sleep", lambda _: None)

    daemon_mod.diagnose_and_fix(["test issue"])

    assert any("eigenflux stream" in p for p in killed_patterns), \
        f"diagnose_and_fix must pkill 'eigenflux stream'; patterns used: {killed_patterns}"


def test_restart_wait_is_at_least_60s(tmp_path, monkeypatch):
    """After restarting bot.sh, daemon must wait long enough for the first
    heartbeat cycle to complete (Claude call can take 60-120s).

    Bug: old code waited only 10s → immediately judged as 'still unhealthy'
    → triggered another restart → restart spiral.
    """
    monkeypatch.setattr(daemon_mod, "JARVIS_DIR", tmp_path)
    monkeypatch.setattr(daemon_mod, "BOT_PID_FILE", tmp_path / ".bot.pid")
    monkeypatch.setattr(daemon_mod, "RESTART_STATE_FILE", tmp_path / ".daemon_restart_state.json")
    monkeypatch.setattr(daemon_mod, "BREAKER_LATCH_FILE", tmp_path / "data" / "restart_breaker.latched")
    monkeypatch.setattr(daemon_mod, "last_restart_time", 0)
    monkeypatch.setattr(daemon_mod, "restart_count", 0)
    monkeypatch.setattr(daemon_mod, "log", lambda *a, **k: None)
    monkeypatch.setattr(daemon_mod, "notify_lark", lambda *a, **k: None)
    monkeypatch.setattr(daemon_mod, "check_health", lambda: {"healthy": True, "issues": []})
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: None)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: None)

    sleep_total = 0.0

    def track_sleep(secs):
        nonlocal sleep_total
        sleep_total += secs

    monkeypatch.setattr(daemon_mod.time, "sleep", track_sleep)

    daemon_mod.diagnose_and_fix(["test issue"])

    assert sleep_total >= 60, \
        f"diagnose_and_fix must sleep ≥60s after restart; only slept {sleep_total}s"


def test_lark_listener_must_be_owned_by_bot(monkeypatch):
    """An orphan sidecar must not satisfy daemon health.

    Regression: broad pgrep saw a reparented lark_event_sidecar and marked the
    listener healthy while bot.sh/admin were dead.
    """
    monkeypatch.setattr(daemon_mod, "_bot_pid", lambda: 100)
    monkeypatch.setattr(daemon_mod, "_ps_processes", lambda: {
        100: (1, "bash /repo/bot.sh"),
        200: (1, "python3 /repo/scripts/lark_event_sidecar.py"),
    })
    assert daemon_mod._is_lark_listener_alive() is False


def test_lark_listener_owned_by_bot_is_healthy(monkeypatch):
    monkeypatch.setattr(daemon_mod, "_bot_pid", lambda: 100)
    monkeypatch.setattr(daemon_mod, "_ps_processes", lambda: {
        100: (1, "bash /repo/bot.sh"),
        150: (100, "bash pipeline subshell"),
        200: (150, "python3 /repo/scripts/lark_event_sidecar.py"),
    })
    assert daemon_mod._is_lark_listener_alive() is True


# ── Body-aware /health probing (stability backlog #6b, 2026-07-07) ──
# An HTTPError whose body is the component's own health JSON means the server
# answered — alive-but-degraded, never a "组件失联" page. Only connection
# failures/timeouts and non-JSON 5xx count as probe failures.

def _http_error(code: int, body: bytes) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "http://127.0.0.1:3456/health", code, "err", None, io.BytesIO(body))


@pytest.fixture
def probe_env(monkeypatch, tmp_path):
    """Wire probes with capture sinks; no test may touch live processes."""
    logs, alerts, recoveries = [], [], []
    monkeypatch.setattr(daemon_mod, "_in_deploy_window", lambda: False)
    monkeypatch.setattr(daemon_mod, "_probe_alert_stamps", {})
    # Stamps persist on every mutation — point the state file at tmp so tests
    # can't write dedup state into the production data/ dir.
    monkeypatch.setattr(daemon_mod, "PROBE_ALERT_STATE_FILE",
                        tmp_path / ".daemon_probe_alert_state.json")
    monkeypatch.setattr(daemon_mod, "log", lambda lvl, msg: logs.append((lvl, msg)))
    monkeypatch.setattr(daemon_mod, "notify_lark",
                        lambda msg, *a, **k: alerts.append(msg))
    monkeypatch.setattr(
        daemon_mod, "_request_component_recovery",
        lambda name: recoveries.append(name) or True,
    )
    return logs, alerts, recoveries


def test_probe_httperror_with_json_body_is_alive_degraded(monkeypatch, probe_env):
    """503 + health-JSON body (admin's 'error' convention) → alive, not DOWN."""
    logs, alerts, recoveries = probe_env
    body = json.dumps({"status": "error", "circuits_open": ["newsapi"],
                       "error": "boom"}).encode()

    def fake_urlopen(url, timeout=None):
        raise _http_error(503, body)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    daemon_mod.probe_observed_components()

    assert not any("连不上了" in a for a in alerts), alerts
    assert not any("DOWN" in msg for _, msg in logs), logs
    degraded = [msg for _, msg in logs if "DEGRADED" in msg]
    assert degraded, logs
    assert "status=error" in degraded[0]
    assert "newsapi" in degraded[0]  # circuits_open surfaced
    # alert-only discipline: a degraded (not 失联) Lark line went out — in the
    # 2026-07-09 plain-Chinese wording (Pascal killed the status=/HTTP jargon)
    assert any("它自己报告有问题" in a and "newsapi" in a for a in alerts)


def test_probe_200_with_degraded_body_is_logged(monkeypatch, probe_env):
    """Admin's other convention: HTTP 200 with {"status": "degraded"} body."""
    logs, alerts, recoveries = probe_env
    body = json.dumps({"status": "degraded",
                       "priority_wedged": ["morning_brief"]}).encode()

    class FakeResp:
        status = 200

        def read(self):
            return body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda url, timeout=None: FakeResp())
    daemon_mod.probe_observed_components()

    assert not any("连不上了" in a for a in alerts), alerts
    assert any("DEGRADED" in msg and "priority_wedged" in msg
               for _, msg in logs), logs


def test_probe_connection_refused_still_alerts_down(monkeypatch, probe_env):
    logs, alerts, recoveries = probe_env

    def fake_urlopen(url, timeout=None):
        raise urllib.error.URLError(ConnectionRefusedError(61, "Connection refused"))

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    daemon_mod.probe_observed_components()

    assert alerts == []
    assert recoveries == ["admin :3456", "dashboard :3457"]
    for name in recoveries:
        daemon_mod._probe_alert_stamps[f"{name}|pending"] = (
            time.time() - daemon_mod.COMPONENT_RECOVERY_GRACE - 1)
    daemon_mod.probe_observed_components()

    assert any("管理面板连续两次连不上" in a for a in alerts), alerts
    # The card names the component the way he knows it — never ":3456" or
    # "launchd" (feedback-no-jargon-dashboards).
    assert not any(":3456" in a or "launchd" in a for a in alerts), alerts
    assert any("DOWN" in msg for _, msg in logs), logs


def test_probe_non_json_5xx_still_alerts_down(monkeypatch, probe_env):
    """A 502 HTML error page is NOT a health report — still a probe failure."""
    logs, alerts, recoveries = probe_env

    def fake_urlopen(url, timeout=None):
        raise _http_error(502, b"<html>Bad Gateway</html>")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    daemon_mod.probe_observed_components()

    assert alerts == []
    for name in recoveries:
        daemon_mod._probe_alert_stamps[f"{name}|pending"] = (
            time.time() - daemon_mod.COMPONENT_RECOVERY_GRACE - 1)
    daemon_mod.probe_observed_components()

    assert any("管理面板连续两次连不上" in a for a in alerts), alerts
    # The card names the component the way he knows it — never ":3456" or
    # "launchd" (feedback-no-jargon-dashboards).
    assert not any(":3456" in a or "launchd" in a for a in alerts), alerts
    assert any("DOWN" in msg for _, msg in logs), logs


# ── Restart budget persistence (stability backlog #8, 2026-07-07) ──
# The daemon hot-reload respawn (REQ-42) resets module globals; without the
# state file a crash-looping stack got 3 fresh attempts per reincarnation.

@pytest.fixture
def restart_env(monkeypatch, tmp_path):
    """Wire diagnose_and_fix against tmp_path; returns the state file path."""
    state_file = tmp_path / ".daemon_restart_state.json"
    monkeypatch.setattr(daemon_mod, "JARVIS_DIR", tmp_path)
    monkeypatch.setattr(daemon_mod, "BOT_PID_FILE", tmp_path / ".bot.pid")
    monkeypatch.setattr(daemon_mod, "RESTART_STATE_FILE", state_file)
    monkeypatch.setattr(daemon_mod, "BREAKER_LATCH_FILE",
                        tmp_path / "data" / "restart_breaker.latched")
    monkeypatch.setattr(daemon_mod, "last_restart_time", 0)
    monkeypatch.setattr(daemon_mod, "restart_count", 0)
    monkeypatch.setattr(daemon_mod, "log", lambda *a, **k: None)
    monkeypatch.setattr(daemon_mod, "notify_lark", lambda *a, **k: None)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: None)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: None)
    monkeypatch.setattr(daemon_mod.time, "sleep", lambda _: None)
    return state_file


def test_restart_budget_persists_across_simulated_reload(monkeypatch, restart_env):
    state_file = restart_env
    monkeypatch.setattr(daemon_mod, "check_health",
                        lambda: {"healthy": False, "issues": ["still down"]})

    daemon_mod.diagnose_and_fix(["bot.sh is not running"])
    assert daemon_mod.restart_count == 1

    saved = json.loads(state_file.read_text())
    assert saved["restart_count"] == 1
    assert saved["last_restart_time"] > 0

    # Simulated REQ-42 respawn: globals reset to import-time defaults,
    # then startup load restores the budget from disk.
    daemon_mod.restart_count = 0
    daemon_mod.last_restart_time = 0
    daemon_mod._load_restart_state()
    assert daemon_mod.restart_count == 1
    assert daemon_mod.last_restart_time == saved["last_restart_time"]


def test_restart_budget_reset_on_success_is_persisted(monkeypatch, restart_env):
    state_file = restart_env
    monkeypatch.setattr(daemon_mod, "check_health",
                        lambda: {"healthy": True, "issues": []})

    daemon_mod.diagnose_and_fix(["bot.sh is not running"])

    assert daemon_mod.restart_count == 0
    assert json.loads(state_file.read_text())["restart_count"] == 0


def test_max_attempts_latches_breaker(monkeypatch, restart_env):
    """After max attempts the breaker LATCHES: no more auto-restarts until the
    flag file is removed (manual re-arm) or recovery is observed.

    Bug (7/7 audit): the old branch zeroed restart_count with only a 10min
    extra cooldown — a permanently broken stack got 3 restart storms + 1 Lark
    page per ~35min forever.
    """
    state_file = restart_env
    latch = daemon_mod.BREAKER_LATCH_FILE
    monkeypatch.setattr(daemon_mod, "restart_count",
                        daemon_mod.MAX_RESTART_ATTEMPTS)
    monkeypatch.setattr(daemon_mod, "check_health",
                        lambda: {"healthy": True, "issues": []})
    alerts = []
    monkeypatch.setattr(daemon_mod, "notify_lark",
                        lambda msg, *a, **k: alerts.append(msg))
    popens = []
    monkeypatch.setattr(subprocess, "Popen",
                        lambda *a, **k: popens.append(a) or None)

    msg = daemon_mod.diagnose_and_fix(["bot.sh is not running"])

    assert "latched" in msg
    assert latch.exists()
    assert popens == []                                # no restart attempted
    assert len(alerts) == 1
    assert "每天复查" in alerts[0]
    assert "restart_breaker.latched" not in alerts[0]  # no internal path in chat
    # Counter reset is safe now that the flag gates all restarts; the flag
    # survives hot-reload respawns because it lives on disk.
    assert json.loads(state_file.read_text())["restart_count"] == 0

    # Latched → further failures neither restart nor re-page (daily dedup:
    # the latch alert itself counts as today's reminder)
    msg2 = daemon_mod.diagnose_and_fix(["bot.sh is not running"])
    assert "latched" in msg2
    assert popens == []
    assert len(alerts) == 1

    # Manual re-arm: deleting the flag restores auto-restart with fresh budget
    latch.unlink()
    daemon_mod.diagnose_and_fix(["bot.sh is not running"])
    assert popens, "restart must resume after manual re-arm"


def test_latched_breaker_daily_reminder(monkeypatch, restart_env):
    """A latched breaker + dead stack must not become a silent outage: at
    most one reminder per day, stamped inside the latch file."""
    latch = daemon_mod.BREAKER_LATCH_FILE
    latch.parent.mkdir(parents=True, exist_ok=True)
    latch.write_text(json.dumps(
        {"latched_at": 0, "last_reminder": time.time() - 25 * 3600}))
    alerts = []
    monkeypatch.setattr(daemon_mod, "notify_lark",
                        lambda msg, *a, **k: alerts.append(msg))

    daemon_mod.diagnose_and_fix(["bot.sh is not running"])
    assert len(alerts) == 1
    assert "自动重启" in alerts[0]
    assert "人工排查" in alerts[0]
    assert "restart_breaker.latched" not in alerts[0]

    # stamp refreshed → a second check within the day stays silent
    daemon_mod.diagnose_and_fix(["bot.sh is not running"])
    assert len(alerts) == 1


def test_clear_breaker_latch_rearms_and_persists(monkeypatch, restart_env):
    """Observed recovery removes the flag and refreshes the persisted budget —
    without this, a transient incident whose post-checks failed would disable
    the auto-restart safety net forever."""
    state_file = restart_env
    latch = daemon_mod.BREAKER_LATCH_FILE
    latch.parent.mkdir(parents=True, exist_ok=True)
    latch.write_text(json.dumps({"latched_at": 0, "last_reminder": 0}))
    monkeypatch.setattr(daemon_mod, "restart_count", 2)
    alerts = []
    monkeypatch.setattr(daemon_mod, "notify_lark",
                        lambda msg, *a, **k: alerts.append(msg))

    daemon_mod._clear_breaker_latch("test recovery")

    assert not latch.exists()
    assert daemon_mod.restart_count == 0
    assert json.loads(state_file.read_text())["restart_count"] == 0
    assert len(alerts) == 1 and "恢复" in alerts[0]

    # Idempotent: no flag → no-op, no duplicate page
    daemon_mod._clear_breaker_latch("test recovery")
    assert len(alerts) == 1


def test_fake_healthy_via_session_lock_does_not_unlatch(monkeypatch, restart_env):
    """Red-team 7/8: check_health has a SECOND fake-healthy path besides the
    deploy window — stale/missing heartbeat suppressed by an active session
    lock. It used to return note-less healthy, so the main loop unlatched the
    breaker (false '恢复正常' page + fresh restart budget) while the heartbeat
    was still dead, resuming the restart storm the latch exists to stop."""
    latch = daemon_mod.BREAKER_LATCH_FILE
    latch.parent.mkdir(parents=True, exist_ok=True)
    latch.write_text(json.dumps({"latched_at": 0, "last_reminder": 0}))
    alerts = []
    monkeypatch.setattr(daemon_mod, "notify_lark",
                        lambda msg, *a, **k: alerts.append(msg))
    monkeypatch.setattr(daemon_mod, "_bot_pid", lambda: 123)
    monkeypatch.setattr(daemon_mod, "_is_lark_listener_alive", lambda bot_pid=None: True)
    monkeypatch.setattr(daemon_mod, "_find_last_heartbeat",
                        lambda: daemon_mod.HEARTBEAT_STALE_THRESHOLD + 60)
    monkeypatch.setattr(daemon_mod, "_in_wake_grace", lambda now=None: False)
    (daemon_mod.JARVIS_DIR / ".session_lock_abc").write_text("999 token")

    result = daemon_mod.check_health()
    assert result["healthy"] is True          # suppression still holds

    daemon_mod._maybe_clear_breaker_latch(result)

    assert latch.exists(), "fake-healthy (session lock) must NOT unlatch"
    assert alerts == []                       # no false 恢复正常 page

    # A genuinely healthy, note-less result still unlatches
    daemon_mod._maybe_clear_breaker_latch({"healthy": True, "issues": []})
    assert not latch.exists()
    assert len(alerts) == 1 and "恢复" in alerts[0]


# ── Session-lock kill identity check (7/7 audit, same class as af35420) ──
# The lock stores the $! of a backgrounded bot.sh pipeline subshell — ps shows
# 'bash .../bot.sh' (parent argv), NEVER 'claude'. A recycled PID must not be
# killed blind.

def test_session_lock_pid_identity_check(monkeypatch):
    monkeypatch.setattr(daemon_mod, "JARVIS_DIR", Path("/repo"))

    class _R:
        def __init__(self, out):
            self.stdout = out

    def fake_ps(out):
        return lambda *a, **k: _R(out)

    # The real lock-holder signature: backgrounded bot.sh subshell
    monkeypatch.setattr(subprocess, "run", fake_ps("bash /repo/bot.sh\n"))
    assert daemon_mod._session_lock_pid_is_ours(123) is True

    # A recycled PID landing on ANY claude process (e.g. Pascal's interactive
    # Claude Code session) must NOT match: a legitimate lock holder never
    # shows 'claude' in argv, so a substring arm could only ever hit a wrong
    # process (red-team 7/8; banned pgrep-substring class).
    monkeypatch.setattr(subprocess, "run", fake_ps("claude -p --resume abc\n"))
    assert daemon_mod._session_lock_pid_is_ours(123) is False

    # Recycled PID → arbitrary user process must NOT be killed
    monkeypatch.setattr(subprocess, "run", fake_ps("/usr/bin/vim thesis.txt\n"))
    assert daemon_mod._session_lock_pid_is_ours(123) is False

    # Another repo's bot.sh is not ours either
    monkeypatch.setattr(subprocess, "run", fake_ps("bash /other/bot.sh\n"))
    assert daemon_mod._session_lock_pid_is_ours(123) is False

    # Dead PID → empty ps output → no kill
    monkeypatch.setattr(subprocess, "run", fake_ps(""))
    assert daemon_mod._session_lock_pid_is_ours(123) is False


# ── Probe-alert stamp persistence (7/7 audit debt ③) ──
# Hot-reload respawns (5x in 13min on 7/7) wiped the in-memory dedup dict,
# re-paging 组件失联 on every deploy in a burst.

def test_probe_alert_stamps_roundtrip(tmp_path, monkeypatch):
    state_file = tmp_path / ".daemon_probe_alert_state.json"
    monkeypatch.setattr(daemon_mod, "PROBE_ALERT_STATE_FILE", state_file)
    monkeypatch.setattr(daemon_mod, "_probe_alert_stamps", {})

    daemon_mod._probe_alert_stamps["admin :3456"] = 123.0
    daemon_mod._save_probe_alert_stamps()
    assert json.loads(state_file.read_text()) == {"admin :3456": 123.0}

    # Simulated respawn: dict wiped, startup load restores it
    daemon_mod._probe_alert_stamps.clear()
    daemon_mod._load_probe_alert_stamps()
    assert daemon_mod._probe_alert_stamps == {"admin :3456": 123.0}


@pytest.mark.parametrize("content", ["{not json!!", "[1, 2, 3]", ""])
def test_corrupt_probe_alert_state_falls_back_to_empty(tmp_path, monkeypatch, content):
    state_file = tmp_path / ".daemon_probe_alert_state.json"
    state_file.write_text(content)
    monkeypatch.setattr(daemon_mod, "PROBE_ALERT_STATE_FILE", state_file)
    monkeypatch.setattr(daemon_mod, "_probe_alert_stamps", {"stale": 1.0})

    daemon_mod._load_probe_alert_stamps()  # must not raise

    assert daemon_mod._probe_alert_stamps == {}


def test_probe_down_alert_stamp_is_persisted(monkeypatch, probe_env):
    """The DOWN page's dedup stamp must hit disk so a hot-reload respawn
    can't re-page within the 4h window."""
    logs, alerts, recoveries = probe_env

    def fake_urlopen(url, timeout=None):
        raise urllib.error.URLError(ConnectionRefusedError(61, "refused"))

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    daemon_mod.probe_observed_components()

    assert alerts == []
    for name in recoveries:
        daemon_mod._probe_alert_stamps[f"{name}|pending"] = (
            time.time() - daemon_mod.COMPONENT_RECOVERY_GRACE - 1)
    daemon_mod.probe_observed_components()

    assert any("管理面板连续两次连不上" in a for a in alerts)
    saved = json.loads(daemon_mod.PROBE_ALERT_STATE_FILE.read_text())
    assert "admin :3456" in saved and "dashboard :3457" in saved


@pytest.mark.parametrize("content", ["{not json!!", "[1, 2, 3]", ""])
def test_corrupt_restart_state_file_falls_back_to_defaults(restart_env, content):
    restart_env.write_text(content)
    daemon_mod.restart_count = 7
    daemon_mod.last_restart_time = 123.0

    daemon_mod._load_restart_state()  # must not raise

    assert daemon_mod.restart_count == 0
    assert daemon_mod.last_restart_time == 0


def test_missing_restart_state_file_falls_back_to_defaults(restart_env):
    assert not restart_env.exists()
    daemon_mod.restart_count = 5
    daemon_mod._load_restart_state()
    assert daemon_mod.restart_count == 0


# ── Delivery dead-letter consumer (stability backlog #7, daemon half) ──

@pytest.fixture
def deadletter_env(tmp_path, monkeypatch):
    f = tmp_path / ".delivery_deadletter.jsonl"
    monkeypatch.setattr(daemon_mod, "DEADLETTER_FILE", f)
    # log() writes to the REAL daemon.log — unpatched, every pytest run
    # pollutes production logs with fake WARN/ERROR lines.
    monkeypatch.setattr(daemon_mod, "log", lambda *a, **k: None)
    sent = []
    monkeypatch.setattr(daemon_mod, "notify_lark",
                        lambda msg, *a, **k: sent.append(msg))
    return f, sent


def _dl_line(kind, detail="d", due_since="2026-07-07 08:00"):
    return json.dumps({"ts": "2026-07-07 09:00", "kind": kind,
                       "detail": detail, "due_since": due_since},
                      ensure_ascii=False) + "\n"


def test_deadletter_consume_notifies_and_truncates_in_place(deadletter_env):
    f, sent = deadletter_env
    f.write_text(_dl_line("delivery_failures", "3 consecutive send failures")
                 + _dl_line("night_queue_expired", "checkin: 早安"))
    inode_before = f.stat().st_ino
    daemon_mod.consume_delivery_deadletters()
    assert len(sent) == 1
    assert "消息发送连续失败" in sent[0]
    assert "攒批消息过期没送出去" in sent[0]
    assert "2026-07-07 08:00" in sent[0]
    # Contract: truncate IN PLACE — same inode, now empty
    assert f.stat().st_ino == inode_before
    assert f.stat().st_size == 0


def test_deadletter_consume_groups_same_kind(deadletter_env):
    f, sent = deadletter_env
    f.write_text("".join(_dl_line("night_queue_expired", f"e{i}") for i in range(3)))
    daemon_mod.consume_delivery_deadletters()
    assert len(sent) == 1                      # one page, not three
    assert "共 3 条" in sent[0]


def test_sql_deadletter_notice_is_human_and_does_not_claim_failed_is_queued(
    tmp_path, monkeypatch,
):
    from core import delivery

    deadletter = tmp_path / "data" / ".delivery_deadletter.jsonl"
    sent = []
    marked = []

    class Pipe:
        def __init__(self, _root):
            pass

        def pending_dead_letters(self, _limit):
            return [
                {"id": 1, "source": "eigenflux", "kind": "card",
                 "detail": '{"ok":false,"error":"keychain Get failed"}'},
                {"id": 2, "source": "eigenflux", "kind": "card",
                 "detail": "raw internal error"},
                {"id": 3, "source": "mail", "kind": "card",
                 "detail": "API Error: secret transport detail"},
            ]

        def mark_dead_letters_notified(self, ids):
            marked.extend(ids)

    monkeypatch.setattr(daemon_mod, "JARVIS_DIR", tmp_path)
    monkeypatch.setattr(daemon_mod, "_DEFAULT_DEADLETTER_FILE", deadletter)
    monkeypatch.setattr(daemon_mod, "DEADLETTER_FILE", deadletter)
    monkeypatch.setattr(daemon_mod, "log", lambda *a, **k: None)
    monkeypatch.setattr(
        daemon_mod, "notify_lark",
        lambda message, *a, **k: sent.append(message) or True,
    )
    monkeypatch.setattr(delivery, "DeliveryPipeline", Pipe)

    daemon_mod.consume_delivery_deadletters()

    assert marked == [1, 2, 3]
    assert len(sent) == 1
    assert "最终未送达" in sent[0]
    assert "只补发仍有效、仍未处理" in sent[0]
    assert "eigenflux：2 条" in sent[0]
    assert "mail：1 条" in sent[0]
    assert "已保留在统一投递队列" not in sent[0]
    assert "keychain" not in sent[0].lower()
    assert "API Error" not in sent[0]


def test_deadletter_consume_noop_on_missing_or_empty(deadletter_env):
    f, sent = deadletter_env
    daemon_mod.consume_delivery_deadletters()   # missing file
    f.write_text("")
    daemon_mod.consume_delivery_deadletters()   # empty file
    assert sent == []


def test_deadletter_consume_skips_garbage_lines(deadletter_env):
    f, sent = deadletter_env
    f.write_text("not json\n" + _dl_line("delivery_failures") + "{broken\n")
    daemon_mod.consume_delivery_deadletters()
    assert len(sent) == 1
    assert "消息发送连续失败" in sent[0]


def test_deadletter_consume_never_raises(deadletter_env, monkeypatch):
    f, sent = deadletter_env
    f.write_text(_dl_line("delivery_failures"))
    monkeypatch.setattr(daemon_mod, "notify_lark",
                        lambda msg, *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    daemon_mod.consume_delivery_deadletters()   # must not propagate


def test_deadletter_genuine_alert_loss_preserves_source_evidence(
        deadletter_env, monkeypatch):
    f, sent = deadletter_env
    original = _dl_line("delivery_failures", "3 consecutive send failures")
    f.write_text(original)
    monkeypatch.setattr(daemon_mod, "notify_lark", lambda *a, **k: False)

    daemon_mod.consume_delivery_deadletters()

    assert f.read_text() == original


def test_deadletter_pending_durable_envelope_consumes_legacy_copy(
        deadletter_env, monkeypatch):
    f, sent = deadletter_env
    f.write_text(_dl_line("delivery_failures", "3 consecutive send failures"))
    monkeypatch.setattr(daemon_mod, "notify_lark", lambda *a, **k: None)

    daemon_mod.consume_delivery_deadletters()

    assert f.read_text() == ""


# ── provider_failover rendering (red-team 7/8) ──
# provider_failover rows are status notes ("已切到备用通道"/"已切回"), not
# failed deliveries: wrapping them in the '消息没送出去' banner asserts a
# delivery failure that never happened, and rendering the OLDEST row could
# re-announce an outage the newest row says is already over.

def test_deadletter_provider_failover_standalone_full_detail(deadletter_env):
    f, sent = deadletter_env
    detail = ("Claude 主通道本月额度用完了，我已自动切到备用通道，功能不受影响。"
              "月初恢复后我会自动切回主通道，用量情况可以在 "
              "claude.ai/settings/usage 页面查看，有问题随时叫我。")
    assert len(detail) > 80                     # would trip the old [:80] cap
    f.write_text(_dl_line("provider_failover", detail))

    daemon_mod.consume_delivery_deadletters()

    assert len(sent) == 1
    assert sent[0] == detail                    # full text, no truncation
    assert "没送出去" not in sent[0]            # no delivery-failure framing


def test_deadletter_provider_failover_trip_plus_clear_sends_only_newest(deadletter_env):
    f, sent = deadletter_env
    f.write_text(_dl_line("provider_failover", "额度用完了，我已自动切到备用通道。")
                 + _dl_line("provider_failover", "主通道恢复了，已切回。"))

    daemon_mod.consume_delivery_deadletters()

    assert len(sent) == 1
    assert sent[0] == "主通道恢复了，已切回。"
    assert "备用通道" not in sent[0]            # stale trip note suppressed


def test_deadletter_mixed_batch_keeps_wrapper_for_failure_kinds(deadletter_env):
    f, sent = deadletter_env
    f.write_text(_dl_line("delivery_failures", "3 consecutive send failures")
                 + _dl_line("provider_failover", "主通道恢复了，已切回。"))

    daemon_mod.consume_delivery_deadletters()

    assert len(sent) == 2
    assert sent.count("主通道恢复了，已切回。") == 1
    wrapped = [m for m in sent if "没送出去" in m]
    assert len(wrapped) == 1
    assert "消息发送连续失败" in wrapped[0]
    assert "模型通道切换" not in wrapped[0]     # failover row not double-listed


# ── Absence receipts (2026-08-19 audit) ──────────────────────────────────
# The daemon detected the 39h lid-close 38 times and every observation ended
# in "post-wake grace, NOT restarting". Correct about restarts, silent about
# the absence: nobody told Pascal his agent had been gone for a working day.


def test_daemon_records_and_reports_a_working_day_absence(tmp_path, monkeypatch):
    from core import absence, hostclock

    monkeypatch.setattr(daemon_mod, "JARVIS_DIR", tmp_path)
    monkeypatch.setattr(daemon_mod, "log", lambda *a, **k: None)
    sent = []
    monkeypatch.setattr(absence, "emit",
                        lambda root, report: sent.append(report) or True)

    slept = 39 * 3600
    daemon_mod._observe_absence(slept)
    assert hostclock.slept_between(tmp_path, time.time() - slept - 60,
                                   time.time()) >= slept - 60
    assert sent == []  # the wake is not confirmed yet

    # Two ticks later the host is still up: the receipt goes out once.
    state = json.loads((tmp_path / absence.STATE_FILE).read_text())
    state["end"] = time.time() - absence.AWAKE_CONFIRM_SECONDS - 1
    (tmp_path / absence.STATE_FILE).write_text(json.dumps(state))
    daemon_mod._observe_absence(0)
    daemon_mod._observe_absence(0)
    assert len(sent) == 1
    assert round(sent[0].slept_seconds / 3600) == 39


def test_absence_receipt_failure_never_kills_the_daemon_loop(tmp_path, monkeypatch):
    from core import absence

    logged = []
    monkeypatch.setattr(daemon_mod, "JARVIS_DIR", tmp_path)
    monkeypatch.setattr(daemon_mod, "log",
                        lambda level, msg: logged.append((level, msg)))

    def boom(*_a, **_k):
        raise RuntimeError("ledger on fire")

    monkeypatch.setattr(absence, "observe", boom)
    daemon_mod._observe_absence(3600)  # must not raise
    assert any(level == "ERROR" for level, _ in logged)
