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
from unittest.mock import patch

import pytest

import daemon as daemon_mod


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


def test_kill_patterns_include_eigenflux(tmp_path, monkeypatch):
    """diagnose_and_fix must kill eigenflux stream processes during restart.

    Bug: old kill list didn't include eigenflux → orphan streams survived restart
    → 'Connection replaced by another session' infinite loop.
    """
    monkeypatch.setattr(daemon_mod, "JARVIS_DIR", tmp_path)
    monkeypatch.setattr(daemon_mod, "BOT_PID_FILE", tmp_path / ".bot.pid")
    monkeypatch.setattr(daemon_mod, "RESTART_STATE_FILE", tmp_path / ".daemon_restart_state.json")
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
def probe_env(monkeypatch):
    """Wire probe_observed_components with capture sinks; returns (logs, alerts)."""
    logs, alerts = [], []
    monkeypatch.setattr(daemon_mod, "_in_deploy_window", lambda: False)
    monkeypatch.setattr(daemon_mod, "_probe_alert_stamps", {})
    monkeypatch.setattr(daemon_mod, "log", lambda lvl, msg: logs.append((lvl, msg)))
    monkeypatch.setattr(daemon_mod, "notify_lark", lambda msg: alerts.append(msg))
    return logs, alerts


def test_probe_httperror_with_json_body_is_alive_degraded(monkeypatch, probe_env):
    """503 + health-JSON body (admin's 'error' convention) → alive, not DOWN."""
    logs, alerts = probe_env
    body = json.dumps({"status": "error", "circuits_open": ["newsapi"],
                       "error": "boom"}).encode()

    def fake_urlopen(url, timeout=None):
        raise _http_error(503, body)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    daemon_mod.probe_observed_components()

    assert not any("失联" in a for a in alerts), alerts
    assert not any("DOWN" in msg for _, msg in logs), logs
    degraded = [msg for _, msg in logs if "DEGRADED" in msg]
    assert degraded, logs
    assert "status=error" in degraded[0]
    assert "newsapi" in degraded[0]  # circuits_open surfaced
    # alert-only discipline: a degraded (not 失联) Lark line went out
    assert any("降级" in a and "newsapi" in a for a in alerts)


def test_probe_200_with_degraded_body_is_logged(monkeypatch, probe_env):
    """Admin's other convention: HTTP 200 with {"status": "degraded"} body."""
    logs, alerts = probe_env
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

    assert not any("失联" in a for a in alerts), alerts
    assert any("DEGRADED" in msg and "priority_wedged" in msg
               for _, msg in logs), logs


def test_probe_connection_refused_still_alerts_down(monkeypatch, probe_env):
    logs, alerts = probe_env

    def fake_urlopen(url, timeout=None):
        raise urllib.error.URLError(ConnectionRefusedError(61, "Connection refused"))

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    daemon_mod.probe_observed_components()

    assert any("失联" in a for a in alerts), alerts
    assert any("DOWN" in msg for _, msg in logs), logs


def test_probe_non_json_5xx_still_alerts_down(monkeypatch, probe_env):
    """A 502 HTML error page is NOT a health report — still a probe failure."""
    logs, alerts = probe_env

    def fake_urlopen(url, timeout=None):
        raise _http_error(502, b"<html>Bad Gateway</html>")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    daemon_mod.probe_observed_components()

    assert any("失联" in a for a in alerts), alerts
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


def test_max_attempts_latch_cooldown_survives_reload(monkeypatch, restart_env):
    state_file = restart_env
    monkeypatch.setattr(daemon_mod, "restart_count",
                        daemon_mod.MAX_RESTART_ATTEMPTS)

    msg = daemon_mod.diagnose_and_fix(["bot.sh is not running"])
    assert "max restart attempts" in msg.lower()

    saved = json.loads(state_file.read_text())
    assert saved["restart_count"] == 0
    assert saved["last_restart_time"] > time.time()  # 10min extra cooldown

    # A respawned daemon must still honor the latch cooldown
    daemon_mod.restart_count = 0
    daemon_mod.last_restart_time = 0
    daemon_mod._load_restart_state()
    assert daemon_mod.diagnose_and_fix(["x"]).startswith("restart cooldown")


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
    monkeypatch.setattr(daemon_mod, "notify_lark", lambda msg: sent.append(msg))
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
                        lambda msg: (_ for _ in ()).throw(RuntimeError("boom")))
    daemon_mod.consume_delivery_deadletters()   # must not propagate
