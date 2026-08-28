"""Regression tests for the 2026-07-10 audit fixes in daemon.py.

Survivor 5 — brain-health grace penetration + offline gate:
  The post-wake grace was re-armed on EVERY wake/check-gap with no memory of
  what it kept suppressing, so on a lid-open-10-40min day a wedge that had
  persisted 17.5h across sleeps never paged (7/10: intention-check, zero
  BRAIN-DEAD WARNs all day). Fix: a persisted suppression ledger
  ({"since", "windows"} in BRAIN_STATE_FILE); a verdict suppressed across a
  SECOND awake window or for a cumulative hour punches through the grace.
  Conversely, awake-but-offline (>30min, 7/9 flight day) used to page a false
  BRAIN-DEAD into the dead-letter file: a 2s TCP probe now defers the page
  and re-arms the grace while the host is offline.

Survivor 7 — acquire_singleton PID-recycling wedge:
  os.kill(pid, 0) alone is not an identity check: after a forced power-off
  the stale pidfile survives the reboot, and a recycled PID (or a root
  process → PermissionError) made every launchd respawn exit 'already
  running' — a 10s crash loop with the whole stack down and zero pages.
  Fix: alive PIDs must also LOOK like `python3 <JARVIS_DIR>/daemon.py`;
  PermissionError ⇒ foreign process ⇒ stale.

Every test stubs log() and notify_lark() and points path constants at
tmp_path — no test may touch the live daemon.log or data/ (072cf2f lesson).
"""

import json
import os
import subprocess
import time

import pytest

import daemon as daemon_mod


# ── Survivor 5: _check_brain_health grace penetration + offline gate ──

def _write_hb_state(tmp_path, wedged=True):
    """A single PRIORITY task (intention-check), wedged or healthy."""
    now = time.time()
    if wedged:
        st = {"intention-check": {
            "last_run": now - 60,
            "last_success": now - 17.5 * 3600,
            "last_status": "failed",
            "circuit": {"consecutive_failures": 5, "total_failures": 50,
                        "total_runs": 60, "disabled_until": 0}}}
    else:
        st = {"intention-check": {
            "last_run": now - 60,
            "last_success": now - 60,
            "last_status": "ok",
            "circuit": {"consecutive_failures": 0, "total_failures": 0,
                        "total_runs": 60, "disabled_until": 0}}}
    (tmp_path / "heartbeat_state.json").write_text(json.dumps(st))


def _seed_brain_state(tmp_path, **kw):
    state = {"samples": {}, "last_alert": 0, "last_check_ts": 0,
             "grace_until": 0, "suppressed": {}}
    state.update(kw)
    (tmp_path / ".daemon_brain_state.json").write_text(json.dumps(state))


def _persisted(tmp_path):
    return json.loads((tmp_path / ".daemon_brain_state.json").read_text())


@pytest.fixture
def brain_env(tmp_path, monkeypatch):
    logs, alerts = [], []
    monkeypatch.setattr(daemon_mod, "JARVIS_DIR", tmp_path)
    monkeypatch.setattr(daemon_mod, "BRAIN_STATE_FILE",
                        tmp_path / ".daemon_brain_state.json")
    monkeypatch.setattr(daemon_mod, "log",
                        lambda lvl, msg: logs.append((lvl, msg)))
    monkeypatch.setattr(daemon_mod, "notify_lark",
                        lambda msg, *a, **k: alerts.append(msg))
    monkeypatch.setattr(daemon_mod, "_in_deploy_window", lambda: False)
    monkeypatch.setattr(daemon_mod, "_find_last_heartbeat", lambda: 10)
    monkeypatch.setattr(daemon_mod, "_network_reachable", lambda: True)
    monkeypatch.setattr(daemon_mod, "_request_component_recovery",
                        lambda name: True)
    monkeypatch.setattr(daemon_mod, "last_wake_time", 0.0)
    (tmp_path / "HEARTBEAT.md").write_text(
        "### intention-check\n- interval: 30m\n")
    return tmp_path, logs, alerts


def _no_errors(logs):
    return [m for lvl, m in logs if lvl == "ERROR"] == []


def test_first_grace_window_still_suppresses_and_opens_ledger(brain_env,
                                                              monkeypatch):
    """A wedge first seen right after a wake stays suppressed (the grace's
    whole point), but the suppression is now REMEMBERED on disk."""
    tmp_path, logs, alerts = brain_env
    _write_hb_state(tmp_path, wedged=True)
    monkeypatch.setattr(daemon_mod, "last_wake_time", time.time() - 60)

    daemon_mod._check_brain_health()

    assert alerts == []
    assert any("would alert but in post-wake grace" in m
               for lvl, m in logs if lvl == "INFO")
    sup = _persisted(tmp_path)["suppressed"]
    assert sup["windows"] == 1
    assert time.time() - sup["since"] < 60
    assert _no_errors(logs)


def test_wedge_crossing_second_wake_window_records_without_owner_page(
        brain_env, monkeypatch):
    """7/10 regression: suppressed in an earlier awake window, host slept
    (check-gap), wedge still there on the next wake ⇒ page THROUGH grace."""
    tmp_path, logs, alerts = brain_env
    now = time.time()
    _write_hb_state(tmp_path, wedged=True)
    _seed_brain_state(tmp_path, last_check_ts=now - 2 * 3600,
                      suppressed={"since": now - 10 * 3600, "windows": 1},
                      repair_requested_at=(
                          now - daemon_mod.BRAIN_RECOVERY_GRACE - 1))
    monkeypatch.setattr(daemon_mod, "last_wake_time", now - 60)  # in grace

    daemon_mod._check_brain_health()

    assert alerts == []
    warn = [m for lvl, m in logs if lvl == "WARN"]
    # the conversation-audit classifier needs the literal prefix intact
    assert warn and warn[0].startswith("BRAIN-DEAD heartbeat: ")
    assert "[persisted across post-wake grace]" in warn[0]
    st = _persisted(tmp_path)
    assert st["suppressed"] == {}          # ledger cleared once paged
    assert time.time() - st["last_alert"] < 60
    assert _no_errors(logs)


def test_cumulative_hour_of_suppression_records_without_owner_page(
        brain_env, monkeypatch):
    """Short naps (<15min check-gap) never increment windows — the cumulative
    clock is what catches an all-day lid-cycling pattern."""
    tmp_path, logs, alerts = brain_env
    now = time.time()
    _write_hb_state(tmp_path, wedged=True)
    _seed_brain_state(
        tmp_path, last_check_ts=now - 60,
        grace_until=now + 900,
        suppressed={"since": now - daemon_mod.BRAIN_SUPPRESS_MAX_SECONDS - 100,
                    "windows": 1},
        repair_requested_at=now - daemon_mod.BRAIN_RECOVERY_GRACE - 1)

    daemon_mod._check_brain_health()

    assert alerts == []
    assert _persisted(tmp_path)["suppressed"] == {}
    assert _no_errors(logs)


def test_penetrating_page_still_respects_dedup_window(brain_env, monkeypatch):
    """Penetration must not turn into a page storm: within PROBE_ALERT_WINDOW
    of the last page it only logs the suppression trace row."""
    tmp_path, logs, alerts = brain_env
    now = time.time()
    _write_hb_state(tmp_path, wedged=True)
    _seed_brain_state(tmp_path, last_alert=now - 600,
                      last_check_ts=now - 2 * 3600,
                      suppressed={"since": now - 10 * 3600, "windows": 1})
    monkeypatch.setattr(daemon_mod, "last_wake_time", now - 60)

    daemon_mod._check_brain_health()

    assert alerts == []
    assert any("would alert but in post-wake grace" in m
               for lvl, m in logs if lvl == "INFO")
    # the ledger survives (windows advanced) so the next dedup expiry pages
    assert _persisted(tmp_path)["suppressed"]["windows"] == 2
    assert _no_errors(logs)


def test_offline_defers_page_and_rearms_grace(brain_env, monkeypatch):
    """7/9 flight-day regression: awake >30min but offline used to page a
    false BRAIN-DEAD into the dead-letter file. Offline ⇒ defer + grace."""
    tmp_path, logs, alerts = brain_env
    _write_hb_state(tmp_path, wedged=True)
    monkeypatch.setattr(daemon_mod, "_network_reachable", lambda: False)

    daemon_mod._check_brain_health()   # out of grace, would page

    assert alerts == []
    assert any("host looks offline" in m for lvl, m in logs if lvl == "INFO")
    st = _persisted(tmp_path)
    assert st["grace_until"] > time.time()   # re-armed for the recovery
    assert st["suppressed"] == {}            # offline accrual never penetrates
    assert st["last_alert"] == 0             # dedup clock NOT consumed
    assert st["deadman_withhold"] is False   # unverified candidate keeps pinging
    assert _no_errors(logs)

    # Network back: the re-armed grace holds — no instant page on recovery.
    monkeypatch.setattr(daemon_mod, "_network_reachable", lambda: True)
    daemon_mod._check_brain_health()
    assert alerts == []
    assert _persisted(tmp_path)["suppressed"]["windows"] == 1


def test_out_of_grace_online_waits_without_restarting_heartbeat(
        brain_env, monkeypatch):
    tmp_path, logs, alerts = brain_env
    _write_hb_state(tmp_path, wedged=True)
    recoveries = []
    monkeypatch.setattr(
        daemon_mod,
        "_request_component_recovery",
        lambda name: recoveries.append(name) or True,
    )

    daemon_mod._check_brain_health()

    assert alerts == []
    assert recoveries == []
    assert _persisted(tmp_path)["repair_requested_at"] > 0
    assert any("without process restart" in m for _, m in logs)


def test_persistent_failure_after_recovery_grace_stays_internal(brain_env):
    tmp_path, logs, alerts = brain_env
    now = time.time()
    _write_hb_state(tmp_path, wedged=True)
    _seed_brain_state(
        tmp_path,
        repair_requested_at=now - daemon_mod.BRAIN_RECOVERY_GRACE - 1,
    )

    daemon_mod._check_brain_health()

    assert alerts == []
    warn = [m for lvl, m in logs if lvl == "WARN"]
    assert warn and warn[0].startswith("BRAIN-DEAD heartbeat: ")
    assert "persisted across post-wake grace" not in warn[0]
    assert _persisted(tmp_path)["deadman_withhold"] is True
    assert _no_errors(logs)


def test_healthy_check_clears_suppression_ledger(brain_env, monkeypatch):
    tmp_path, logs, alerts = brain_env
    now = time.time()
    _write_hb_state(tmp_path, wedged=False)
    _seed_brain_state(tmp_path, last_check_ts=now - 60,
                      suppressed={"since": now - 50000, "windows": 5},
                      repair_requested_at=now - 60)

    daemon_mod._check_brain_health()

    assert alerts == []
    assert _persisted(tmp_path)["suppressed"] == {}
    assert _persisted(tmp_path)["repair_requested_at"] == 0
    assert _persisted(tmp_path)["deadman_withhold"] is False
    assert _no_errors(logs)


# ── Survivor 7: acquire_singleton identity check ──

@pytest.fixture
def pid_env(tmp_path, monkeypatch):
    pid_file = tmp_path / ".daemon.pid"
    monkeypatch.setattr(daemon_mod, "DAEMON_PID_FILE", pid_file)
    return pid_file


class _PsResult:
    def __init__(self, out):
        self.stdout = out


def _stub_ps(monkeypatch, out):
    monkeypatch.setattr(daemon_mod.subprocess, "run",
                        lambda *a, **k: _PsResult(out))


def test_pid_is_ours_accepts_launchd_and_restart_sh_argv(monkeypatch):
    d = daemon_mod.JARVIS_DIR
    _stub_ps(monkeypatch, f"/opt/homebrew/bin/python3 {d}/daemon.py")
    assert daemon_mod._daemon_pid_is_ours(123) is True
    _stub_ps(monkeypatch, f"python3 {d}/daemon.py")
    assert daemon_mod._daemon_pid_is_ours(123) is True


def test_pid_is_ours_rejects_recycled_pids(monkeypatch):
    d = daemon_mod.JARVIS_DIR
    for argv in ("/usr/libexec/trustd",                       # boot process
                 f"/opt/homebrew/bin/python3 {d}/admin.py",   # our other py
                 "/opt/homebrew/bin/python3 /other/repo/daemon.py",  # foreign
                 ""):                                          # already gone
        _stub_ps(monkeypatch, argv)
        assert daemon_mod._daemon_pid_is_ours(123) is False, argv


def test_pid_is_ours_fails_closed_when_ps_errors(monkeypatch):
    def boom(*a, **k):
        raise subprocess.TimeoutExpired("ps", 5)
    monkeypatch.setattr(daemon_mod.subprocess, "run", boom)
    assert daemon_mod._daemon_pid_is_ours(123) is True


def test_recycled_alive_pid_is_overwritten_not_fatal(pid_env, monkeypatch):
    """The 7/10 wedge: pidfile points at an ALIVE process that is not a
    daemon.py — must overwrite and start, not exit into a launchd loop."""
    pid_env.write_text(str(os.getpid()))  # alive, but argv is pytest's
    _stub_ps(monkeypatch, "/usr/bin/some-boot-process --flag")

    daemon_mod.acquire_singleton()        # must NOT raise SystemExit

    assert pid_env.read_text() == str(os.getpid())


def test_permission_error_treated_as_foreign_and_stale(pid_env, monkeypatch):
    """We run as a user LaunchAgent: EPERM means a root process recycled the
    PID. The old code exited 1 here — the permanent launchd crash loop."""
    pid_env.write_text("12345")

    def eperm(pid, sig):
        raise PermissionError

    monkeypatch.setattr(daemon_mod.os, "kill", eperm)

    daemon_mod.acquire_singleton()        # must NOT raise SystemExit

    assert pid_env.read_text() == str(os.getpid())


def test_genuine_daemon_still_blocks_second_instance(pid_env, monkeypatch):
    pid_env.write_text(str(os.getpid()))  # alive
    monkeypatch.setattr(daemon_mod, "_daemon_pid_is_ours", lambda pid: True)

    with pytest.raises(SystemExit):
        daemon_mod.acquire_singleton()

    assert pid_env.read_text() == str(os.getpid())  # untouched


def test_dead_pid_remains_stale_path(pid_env, monkeypatch):
    pid_env.write_text("54321")

    def gone(pid, sig):
        raise ProcessLookupError

    monkeypatch.setattr(daemon_mod.os, "kill", gone)

    daemon_mod.acquire_singleton()

    assert pid_env.read_text() == str(os.getpid())
