"""Regression tests for the 2026-07-08 audit fixes in daemon.py.

Covers:
  F10 — _check_diag_staleness: the daemon watches the self-diagnostic task
        (leg A: task stopped; leg B: its ⚠️ warnings died on the heartbeat-
        side delivery path). Red-team 7/9: leg B is gated on EVIDENCE the
        post ran (heartbeat_state.json's self-diagnostic last_run caught up
        to the pre file) — the old 15min wall-clock grace fired mid-cycle
        whenever pre→post exceeded it.
  F15 — _find_last_heartbeat: newest beat wins across BOTH log formats
        (beats[-1] took the last-concatenated JSON line), plus the
        data/.heartbeat_beat stamp file as the freshest source.
  F25 — _maybe_reset_restart_budget: persisted restart budget returns after a
        sustained genuinely-healthy stretch (leaked increments used to latch
        the breaker with zero restarts attempted).
  F28 — _probe_manifest_criticals: components.yaml criticals not covered by
        richer daemon paths (today: ef-stream) get alert-only coverage.
        Red-team 7/9: debounced — a page needs TWO consecutive red probes
        ≥2min apart, so a crash bot.sh's watchdog heals in ≤30s never pages.
  7/8 user request — priority_wedged-only degradation never pages (owned by
        _check_brain_health); remaining degradation pages in plain Chinese.

Every test stubs log() and notify_lark() and points path constants at
tmp_path — no test may touch the live daemon.log or data/ (072cf2f lesson).
"""

import inspect
import json
import os
import time
import urllib.error
import urllib.request
from datetime import timedelta

import pytest

import core.components as components_mod
import daemon as daemon_mod


# ── F15: _find_last_heartbeat ──

@pytest.fixture
def beat_env(tmp_path, monkeypatch):
    """Hermetic _find_last_heartbeat: logs and stamp live under tmp_path."""
    monkeypatch.setattr(daemon_mod, "JARVIS_DIR", tmp_path)
    monkeypatch.setattr(daemon_mod, "HEARTBEAT_BEAT_STAMP",
                        tmp_path / "data" / ".heartbeat_beat")
    # Divert the /tmp/jarvis_restart.log probe to a missing file so the
    # machine's real restart log can't supply a beat (existing convention).
    real_path = daemon_mod.Path
    monkeypatch.setattr(
        daemon_mod, "Path",
        lambda p="": (tmp_path / "absent_restart.log")
        if str(p) == "/tmp/jarvis_restart.log" else real_path(p))
    return tmp_path


def _local_now(daemon=daemon_mod):
    return daemon._daemon_now().replace(tzinfo=None)


def _bracket_beat(ts):
    return f"[{ts:%Y-%m-%d %H:%M:%S}] [INFO] [heartbeat] Beat sent (working)\n"


def _json_heartbeat_line(ts):
    return (f'{{"ts": "{ts:%Y-%m-%dT%H:%M:%S}", "level": "INFO", '
            f'"component": "heartbeat", "msg": "Calling Claude..."}}\n')


def test_newest_bracket_beat_wins_over_older_json_line(beat_env):
    """beats[-1] regression: JSON matches are appended AFTER bracket matches,
    so an older JSON heartbeat line used to mask a fresh 'Beat sent'."""
    now = _local_now()
    (beat_env / "jarvis.log").write_text(
        _bracket_beat(now - timedelta(seconds=60))
        + _json_heartbeat_line(now - timedelta(seconds=3600)))

    age = daemon_mod._find_last_heartbeat()

    assert age is not None
    assert age < 600, f"age must come from the fresh bracket beat, got {age}"


def test_newest_json_line_wins_over_older_bracket_beat(beat_env):
    now = _local_now()
    (beat_env / "jarvis.log").write_text(
        _bracket_beat(now - timedelta(seconds=3600))
        + _json_heartbeat_line(now - timedelta(seconds=60)))

    age = daemon_mod._find_last_heartbeat()

    assert age is not None
    assert age < 600


def test_beat_stamp_file_is_freshest_source(beat_env):
    """A fresh data/.heartbeat_beat stamp must win over stale log beats."""
    now = _local_now()
    (beat_env / "jarvis.log").write_text(
        _bracket_beat(now - timedelta(seconds=7200)))
    stamp = daemon_mod.HEARTBEAT_BEAT_STAMP
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.touch()

    age = daemon_mod._find_last_heartbeat()

    assert age is not None
    assert age < 60


def test_beat_stamp_absence_falls_back_to_logs(beat_env):
    """Old loop code (pre-stamp deploy) must keep working via the log grep."""
    now = _local_now()
    (beat_env / "jarvis.log").write_text(_bracket_beat(now - timedelta(seconds=90)))
    assert not daemon_mod.HEARTBEAT_BEAT_STAMP.exists()

    age = daemon_mod._find_last_heartbeat()

    assert age is not None
    assert 0 <= age < 600


def test_stale_beat_stamp_does_not_mask_fresh_log_beat(beat_env):
    now = _local_now()
    (beat_env / "jarvis.log").write_text(_bracket_beat(now - timedelta(seconds=60)))
    stamp = daemon_mod.HEARTBEAT_BEAT_STAMP
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.touch()
    old = time.time() - 7200
    os.utime(stamp, (old, old))

    age = daemon_mod._find_last_heartbeat()

    assert age is not None
    assert age < 600  # min(log 60s, stamp 7200s) — freshest wins


def test_no_sources_returns_none(beat_env):
    assert daemon_mod._find_last_heartbeat() is None


# ── F10: _check_diag_staleness ──

@pytest.fixture
def diag_env(tmp_path, monkeypatch):
    """Wire _check_diag_staleness against tmp_path with all guards open;
    returns (pre_file, stamp_file, alerts)."""
    pre = tmp_path / ".diag_last_pre.txt"
    stamp = tmp_path / ".diag_last_alert.json"
    monkeypatch.setattr(daemon_mod, "JARVIS_DIR", tmp_path)
    monkeypatch.setattr(daemon_mod, "DIAG_PRE_FILE", pre)
    monkeypatch.setattr(daemon_mod, "DIAG_ALERT_STAMP", stamp)
    monkeypatch.setattr(daemon_mod, "PROBE_ALERT_STATE_FILE",
                        tmp_path / ".daemon_probe_alert_state.json")
    monkeypatch.setattr(daemon_mod, "_probe_alert_stamps", {})
    monkeypatch.setattr(daemon_mod, "_in_deploy_window", lambda: False)
    monkeypatch.setattr(daemon_mod, "last_wake_time", 0.0)
    monkeypatch.setattr(daemon_mod, "_daemon_started",
                        time.time() - daemon_mod.BRAIN_WAKE_GRACE - 10)
    monkeypatch.setattr(daemon_mod, "_find_last_heartbeat", lambda: 60.0)
    monkeypatch.setattr(daemon_mod, "log", lambda *a, **k: None)
    alerts = []
    monkeypatch.setattr(daemon_mod, "notify_lark",
                        lambda msg, *a, **k: alerts.append(msg))
    monkeypatch.setattr(daemon_mod, "_request_component_recovery",
                        lambda name: True)
    return pre, stamp, alerts


def _age_file(path, seconds):
    ts = time.time() - seconds
    os.utime(path, (ts, ts))


def _write_diag_state(tmp_path, last_run):
    """heartbeat_state.json shape leg B's evidence gate reads. A completed
    cycle stamps last_run with the cycle-START time, which precedes the pre
    file's mtime by the pre-script phase (seconds to minutes)."""
    (tmp_path / "heartbeat_state.json").write_text(
        json.dumps({"self-diagnostic": {"last_run": last_run}}))


def _diag_cycle_completed(pre):
    """Realistic completed-cycle evidence: cycle started 30s before the pre
    file was written (inside DIAG_CYCLE_START_SKEW)."""
    _write_diag_state(pre.parent, pre.stat().st_mtime - 30)


def test_diag_missing_pre_pages_once(diag_env):
    pre, stamp, alerts = diag_env
    assert not pre.exists()

    daemon_mod._check_diag_staleness()
    assert alerts == []  # first observation repairs, it does not page
    daemon_mod._probe_alert_stamps["self-diagnostic|repair"] = (
        time.time() - daemon_mod.DIAG_RECOVERY_GRACE - 1)
    daemon_mod._check_diag_staleness()
    daemon_mod._check_diag_staleness()  # dedup: still one page

    assert len(alerts) == 1
    assert "自诊断" in alerts[0]


def test_diag_stale_pre_pages_with_hours(diag_env):
    pre, stamp, alerts = diag_env
    pre.write_text("=== SYSTEM HEALTH CHECK ===\n")
    _age_file(pre, 10 * 3600)

    daemon_mod._check_diag_staleness()
    assert alerts == []
    daemon_mod._probe_alert_stamps["self-diagnostic|repair"] = (
        time.time() - daemon_mod.DIAG_RECOVERY_GRACE - 1)
    daemon_mod._check_diag_staleness()

    assert len(alerts) == 1
    assert "自诊断" in alerts[0] and "10 小时" in alerts[0]
    # persisted dedup stamp uses the stable key
    assert "self-diagnostic|stale" in daemon_mod._probe_alert_stamps


def test_diag_fresh_pre_rearms_staleness_dedup(diag_env):
    pre, stamp, alerts = diag_env
    pre.write_text("✓ all good\n")  # fresh mtime, no warnings
    daemon_mod._probe_alert_stamps["self-diagnostic|stale"] = time.time() - 10
    daemon_mod._probe_alert_stamps["self-diagnostic|repair"] = time.time() - 20
    daemon_mod._probe_alert_stamps["self-diagnostic|repair-ok"] = time.time() - 20

    daemon_mod._check_diag_staleness()

    assert alerts == []
    assert "self-diagnostic|stale" not in daemon_mod._probe_alert_stamps
    assert "self-diagnostic|repair" not in daemon_mod._probe_alert_stamps
    assert "self-diagnostic|repair-ok" not in daemon_mod._probe_alert_stamps


def test_diag_redelivers_undelivered_warnings_and_writes_stamp(diag_env):
    pre, stamp, alerts = diag_env
    pre.write_text("⚠️ 备份已经 60 小时没跑了\n✓ other stuff\n")
    _age_file(pre, 20 * 60)
    _diag_cycle_completed(pre)          # the post had its turn...
    assert not stamp.exists()           # ...but never managed a send

    daemon_mod._check_diag_staleness()

    assert len(alerts) == 1
    assert "自诊断发现 1 个问题" in alerts[0]
    assert "备份已经 60 小时没跑了" in alerts[0]
    saved = json.loads(stamp.read_text())
    assert saved["ts"] > 0                        # post-compatible shape
    assert saved["lines"] == ["⚠️ 备份已经 60 小时没跑了"]

    daemon_mod._check_diag_staleness()            # stamp now fresh → silent
    assert len(alerts) == 1


def test_diag_genuine_delivery_loss_does_not_consume_shared_stamp(
        diag_env, monkeypatch):
    pre, stamp, alerts = diag_env
    pre.write_text("⚠️ 备份已经 60 小时没跑了\n")
    _age_file(pre, 20 * 60)
    _diag_cycle_completed(pre)
    monkeypatch.setattr(daemon_mod, "notify_lark", lambda *a, **k: False)

    daemon_mod._check_diag_staleness()

    assert not stamp.exists()


def test_diag_no_redelivery_while_cycle_in_flight(diag_env):
    """Red-team 7/9 replay: pre 20min old (past the OLD 15min wall-clock
    grace) but the cycle is still in flight — self-diagnostic's last_run is
    the PREVIOUS run, ~4h back. A failover retry chain or 4-pre batch takes
    16-27min pre→post; the daemon must not page 'delivery path dead' while
    the warnings are merely queued behind the in-flight Claude call."""
    pre, stamp, alerts = diag_env
    pre.write_text("⚠️ something broke\n")
    _age_file(pre, 20 * 60)
    _write_diag_state(pre.parent, time.time() - 4 * 3600)

    daemon_mod._check_diag_staleness()

    assert alerts == []
    assert not stamp.exists()   # and it must not pre-empt the post's own send


def test_diag_no_redelivery_without_state_evidence(diag_env):
    """No readable heartbeat_state.json ⇒ the post's turn cannot be proven —
    never re-deliver on a guess."""
    pre, stamp, alerts = diag_env
    pre.write_text("⚠️ something broke\n")
    _age_file(pre, 20 * 60)
    assert not (pre.parent / "heartbeat_state.json").exists()

    daemon_mod._check_diag_staleness()

    assert alerts == []


def test_diag_backdated_last_run_blocks_redelivery(diag_env):
    """A FAILED cycle backdates last_run by ~a full interval for the fast
    retry — that retry will rewrite the pre and deliver on its own, so the
    daemon must stay out of the way."""
    pre, stamp, alerts = diag_env
    pre.write_text("⚠️ something broke\n")
    _age_file(pre, 20 * 60)
    _write_diag_state(pre.parent,
                      pre.stat().st_mtime - daemon_mod.DIAG_CYCLE_START_SKEW - 60)

    daemon_mod._check_diag_staleness()

    assert alerts == []


def test_diag_no_redelivery_when_post_suppressed_on_purpose(diag_env):
    """A stamp within the 4h window BEFORE the pre run means the post-script
    deduped deliberately — the daemon must not undo that suppression."""
    pre, stamp, alerts = diag_env
    pre.write_text("⚠️ persistent warning\n")
    _age_file(pre, 20 * 60)
    _diag_cycle_completed(pre)
    pre_mtime = pre.stat().st_mtime
    stamp.write_text(json.dumps({"ts": pre_mtime - 3600, "lines": []}))

    daemon_mod._check_diag_staleness()

    assert alerts == []


def test_diag_no_redelivery_when_post_already_delivered(diag_env):
    pre, stamp, alerts = diag_env
    pre.write_text("⚠️ fresh warning\n")
    _age_file(pre, 20 * 60)
    _diag_cycle_completed(pre)
    stamp.write_text(json.dumps({"ts": time.time(), "lines": []}))

    daemon_mod._check_diag_staleness()

    assert alerts == []


def test_diag_redelivers_when_stamp_predates_pre_beyond_window(diag_env):
    pre, stamp, alerts = diag_env
    pre.write_text("⚠️ warning the post failed to send\n")
    _age_file(pre, 20 * 60)
    _diag_cycle_completed(pre)
    pre_mtime = pre.stat().st_mtime
    stamp.write_text(json.dumps({"ts": pre_mtime - 5 * 3600, "lines": []}))

    daemon_mod._check_diag_staleness()

    assert len(alerts) == 1
    assert "代发" in alerts[0]


def test_diag_no_redelivery_without_warnings(diag_env):
    pre, stamp, alerts = diag_env
    pre.write_text("✓ everything healthy\n")
    _age_file(pre, 20 * 60)
    _diag_cycle_completed(pre)

    daemon_mod._check_diag_staleness()

    assert alerts == []
    assert not stamp.exists()


@pytest.mark.parametrize("guard", ["deploy", "wake", "young_daemon", "dead_loop"])
def test_diag_guards_suppress_everything(diag_env, monkeypatch, guard):
    pre, stamp, alerts = diag_env       # pre missing → would page unguarded
    if guard == "deploy":
        monkeypatch.setattr(daemon_mod, "_in_deploy_window", lambda: True)
    elif guard == "wake":
        monkeypatch.setattr(daemon_mod, "last_wake_time", time.time())
    elif guard == "young_daemon":
        monkeypatch.setattr(daemon_mod, "_daemon_started", time.time())
    elif guard == "dead_loop":
        # a stopped loop already pages via the bot/heartbeat checks
        monkeypatch.setattr(daemon_mod, "_find_last_heartbeat", lambda: None)

    daemon_mod._check_diag_staleness()

    assert alerts == []


def test_diag_check_never_raises(diag_env, monkeypatch):
    pre, stamp, alerts = diag_env
    pre.write_text("⚠️ boom\n")
    _age_file(pre, 20 * 60)
    _diag_cycle_completed(pre)
    monkeypatch.setattr(daemon_mod, "notify_lark",
                        lambda msg, *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))

    daemon_mod._check_diag_staleness()  # must not propagate


# ── F25: _maybe_reset_restart_budget ──

@pytest.fixture
def budget_env(tmp_path, monkeypatch):
    state_file = tmp_path / ".daemon_restart_state.json"
    monkeypatch.setattr(daemon_mod, "RESTART_STATE_FILE", state_file)
    monkeypatch.setattr(daemon_mod, "log", lambda *a, **k: None)
    monkeypatch.setattr(daemon_mod, "notify_lark", lambda *a, **k: None)
    monkeypatch.setattr(daemon_mod, "restart_count", 2)
    monkeypatch.setattr(
        daemon_mod, "last_restart_time",
        time.time() - daemon_mod.RESTART_BUDGET_RESET_WINDOW - 60)
    return state_file


def test_budget_resets_after_sustained_healthy_stretch(budget_env):
    daemon_mod._maybe_reset_restart_budget({"healthy": True, "issues": []})

    assert daemon_mod.restart_count == 0
    assert json.loads(budget_env.read_text())["restart_count"] == 0


def test_budget_not_reset_on_fake_healthy_note(budget_env):
    daemon_mod._maybe_reset_restart_budget(
        {"healthy": True, "issues": [], "note": "session-active"})

    assert daemon_mod.restart_count == 2
    assert not budget_env.exists()


def test_budget_not_reset_on_unhealthy(budget_env):
    daemon_mod._maybe_reset_restart_budget(
        {"healthy": False, "issues": ["bot.sh is not running"]})

    assert daemon_mod.restart_count == 2


def test_budget_not_reset_inside_window(budget_env, monkeypatch):
    monkeypatch.setattr(daemon_mod, "last_restart_time", time.time() - 600)

    daemon_mod._maybe_reset_restart_budget({"healthy": True, "issues": []})

    assert daemon_mod.restart_count == 2


def test_budget_not_reset_when_last_restart_time_in_future(budget_env, monkeypatch):
    """Failed-latch-write fallback stores now+600 in last_restart_time — the
    window arithmetic must treat a future timestamp as 'too recent'."""
    monkeypatch.setattr(daemon_mod, "last_restart_time", time.time() + 600)

    daemon_mod._maybe_reset_restart_budget({"healthy": True, "issues": []})

    assert daemon_mod.restart_count == 2


def test_budget_zero_count_is_noop(budget_env, monkeypatch):
    monkeypatch.setattr(daemon_mod, "restart_count", 0)

    daemon_mod._maybe_reset_restart_budget({"healthy": True, "issues": []})

    assert daemon_mod.restart_count == 0
    assert not budget_env.exists()      # no pointless state churn


# ── F28: _probe_manifest_criticals ──

def _manifest_result(name, ok, critical=True, detail="d"):
    return {"name": name, "ok": ok, "detail": detail, "critical": critical}


@pytest.fixture
def manifest_env(tmp_path, monkeypatch):
    """Wire _probe_manifest_criticals with capture sinks; check_components is
    stubbed per-test via core.components. Returns (logs, alerts)."""
    logs, alerts = [], []
    monkeypatch.setattr(daemon_mod, "_in_deploy_window", lambda: False)
    monkeypatch.setattr(daemon_mod, "_probe_alert_stamps", {})
    monkeypatch.setattr(daemon_mod, "PROBE_ALERT_STATE_FILE",
                        tmp_path / ".daemon_probe_alert_state.json")
    monkeypatch.setattr(daemon_mod, "last_restart_time", 0)
    monkeypatch.setattr(daemon_mod, "_bot_pid", lambda: 123)
    monkeypatch.setattr(daemon_mod, "log", lambda lvl, msg: logs.append((lvl, msg)))
    monkeypatch.setattr(daemon_mod, "notify_lark",
                        lambda msg, *a, **k: alerts.append(msg))
    monkeypatch.setattr(daemon_mod, "_request_component_recovery",
                        lambda name: True)
    return logs, alerts


def _confirm_pending(name="ef-stream"):
    """Replay the wait between two consecutive red probes: age the persisted
    first-seen mark past MANIFEST_CONFIRM_WINDOW."""
    daemon_mod._probe_alert_stamps[f"{name}|pending"] = (
        time.time() - daemon_mod.MANIFEST_CONFIRM_WINDOW - 5)


def test_manifest_dead_ef_stream_pages_on_second_confirmed_probe(
        manifest_env, monkeypatch):
    logs, alerts = manifest_env
    monkeypatch.setattr(
        components_mod, "check_components",
        lambda critical_only=False: [
            _manifest_result("ef-stream", False, detail="no process owned by pid 123")])

    daemon_mod._probe_manifest_criticals()   # 1st red probe: pending only
    assert alerts == []
    assert "ef-stream|pending" in daemon_mod._probe_alert_stamps
    assert "ef-stream|repair" in daemon_mod._probe_alert_stamps

    daemon_mod._probe_manifest_criticals()   # 2nd probe too soon (<2min)
    assert alerts == []

    _confirm_pending()
    daemon_mod._probe_manifest_criticals()   # confirmed: pages
    daemon_mod._probe_manifest_criticals()   # 4h dedup: still one page

    assert len(alerts) == 1
    assert "停了" in alerts[0]
    assert "EigenFlux" in alerts[0]
    # no raw checker detail leaks into the Lark line
    assert "no process" not in alerts[0]
    assert any("ef-stream" in msg for _, msg in logs)


def test_manifest_watchdog_healed_transient_never_pages(manifest_env, monkeypatch):
    """Red-team 7/9: bot.sh's watchdog respawns a dead ef-stream within ≤30s.
    A single red probe landing inside that window must not page — and the
    green probe after the heal must clear the pending mark so a LATER,
    unrelated transient starts its own confirmation from scratch."""
    logs, alerts = manifest_env
    down = [_manifest_result("ef-stream", False)]
    up = [_manifest_result("ef-stream", True)]
    results = {"v": down}
    monkeypatch.setattr(components_mod, "check_components",
                        lambda critical_only=False: results["v"])

    daemon_mod._probe_manifest_criticals()   # crash caught by one probe
    results["v"] = up
    daemon_mod._probe_manifest_criticals()   # watchdog healed it
    assert "ef-stream|pending" not in daemon_mod._probe_alert_stamps
    results["v"] = down
    daemon_mod._probe_manifest_criticals()   # later transient: pending anew

    assert alerts == []


def test_manifest_skips_components_covered_elsewhere(manifest_env, monkeypatch):
    """bot/heartbeat-loop/lark-sidecar drive the restart path; admin/dashboard
    keep the richer degraded-aware probe — none of them may page here."""
    logs, alerts = manifest_env
    monkeypatch.setattr(
        components_mod, "check_components",
        lambda critical_only=False: [
            _manifest_result(n, False) for n in
            ("bot", "heartbeat-loop", "lark-sidecar", "admin", "dashboard")])

    daemon_mod._probe_manifest_criticals()

    assert alerts == []


def test_manifest_recovery_rearms_dedup(manifest_env, monkeypatch):
    logs, alerts = manifest_env
    down = [_manifest_result("ef-stream", False)]
    up = [_manifest_result("ef-stream", True)]
    results = {"v": down}
    monkeypatch.setattr(components_mod, "check_components",
                        lambda critical_only=False: results["v"])

    daemon_mod._probe_manifest_criticals()   # pending
    _confirm_pending()
    daemon_mod._probe_manifest_criticals()   # confirmed: 1st page
    results["v"] = up
    daemon_mod._probe_manifest_criticals()   # recovers → stamps popped
    results["v"] = down
    daemon_mod._probe_manifest_criticals()   # second incident: pending anew
    _confirm_pending()
    daemon_mod._probe_manifest_criticals()   # confirmed: re-pages

    assert len(alerts) == 2


def test_manifest_probe_skipped_during_deploy_window(manifest_env, monkeypatch):
    logs, alerts = manifest_env
    called = []
    monkeypatch.setattr(components_mod, "check_components",
                        lambda critical_only=False: called.append(1) or [])
    monkeypatch.setattr(daemon_mod, "_in_deploy_window", lambda: True)

    daemon_mod._probe_manifest_criticals()

    assert called == [] and alerts == []


def test_manifest_probe_skipped_when_bot_down(manifest_env, monkeypatch):
    """With bot.sh down every owned_by_pidfile check is red — that outage
    belongs to the restart path, not a page per child."""
    logs, alerts = manifest_env
    called = []
    monkeypatch.setattr(components_mod, "check_components",
                        lambda critical_only=False: called.append(1) or [])
    monkeypatch.setattr(daemon_mod, "_bot_pid", lambda: None)

    daemon_mod._probe_manifest_criticals()

    assert called == [] and alerts == []


def test_manifest_probe_skipped_right_after_own_restart(manifest_env, monkeypatch):
    logs, alerts = manifest_env
    called = []
    monkeypatch.setattr(components_mod, "check_components",
                        lambda critical_only=False: called.append(1) or [])
    monkeypatch.setattr(daemon_mod, "last_restart_time", time.time())

    daemon_mod._probe_manifest_criticals()

    assert called == [] and alerts == []


def test_manifest_probe_never_raises(manifest_env, monkeypatch):
    logs, alerts = manifest_env
    monkeypatch.setattr(
        components_mod, "check_components",
        lambda critical_only=False: (_ for _ in ()).throw(RuntimeError("boom")))

    daemon_mod._probe_manifest_criticals()  # must not propagate

    assert alerts == []


# ── 7/8 user request: no priority_wedged duplicate page; plain-Chinese text ──

@pytest.fixture
def degraded_env(tmp_path, monkeypatch):
    logs, alerts = [], []
    monkeypatch.setattr(daemon_mod, "_probe_alert_stamps", {})
    monkeypatch.setattr(daemon_mod, "PROBE_ALERT_STATE_FILE",
                        tmp_path / ".daemon_probe_alert_state.json")
    monkeypatch.setattr(daemon_mod, "log", lambda lvl, msg: logs.append((lvl, msg)))
    monkeypatch.setattr(daemon_mod, "notify_lark",
                        lambda msg, *a, **k: alerts.append(msg))
    return logs, alerts


def test_wedged_only_degradation_never_pages(degraded_env):
    """Pascal 7/8: PRIORITY wedging is _check_brain_health's alert — the
    degraded-health page for the same condition was a duplicate channel."""
    logs, alerts = degraded_env

    daemon_mod._note_degraded_health(
        "admin :3456",
        {"status": "degraded", "priority_wedged": ["intention-check"]},
        http_code=200)

    assert alerts == []
    # still visible in the daemon log for diagnosis
    assert any("DEGRADED" in msg and "intention-check" in msg for _, msg in logs)


def test_wedged_only_degradation_via_full_probe_never_pages(degraded_env, monkeypatch):
    """End-to-end regression for the exact alert Pascal quoted: 200 +
    {"status": "degraded", "priority_wedged": [...]} through the real probe."""
    logs, alerts = degraded_env
    monkeypatch.setattr(daemon_mod, "_in_deploy_window", lambda: False)
    body = json.dumps({"status": "degraded",
                       "priority_wedged": ["intention-check"]}).encode()

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

    assert alerts == []


def test_genuine_degradation_pages_in_plain_chinese(degraded_env):
    logs, alerts = degraded_env

    daemon_mod._note_degraded_health(
        "admin :3456",
        {"status": "degraded", "circuits_open": ["newsapi"],
         "priority_wedged": ["intention-check"]},
        http_code=200)

    assert len(alerts) == 1
    msg = alerts[0]
    assert "管理面板" in msg
    assert "newsapi" in msg           # which circuits — useful, not jargon
    # no SRE jargon / raw field dumps (the exact strings Pascal quoted)
    assert "status=" not in msg
    assert "HTTP" not in msg
    assert "priority_wedged" not in msg
    assert "intention-check" not in msg   # wedge belongs to brain-health page
    assert "degraded" not in msg
    assert "降级" not in msg


def test_error_degradation_pages_without_raw_error_string(degraded_env):
    logs, alerts = degraded_env

    daemon_mod._note_degraded_health(
        "admin :3456",
        {"status": "error", "error": "Traceback: boom at line 3"},
        http_code=503)

    assert len(alerts) == 1
    msg = alerts[0]
    assert "管理面板" in msg
    assert "Traceback" not in msg and "boom" not in msg
    assert "503" not in msg
    # the raw detail is still logged for diagnosis
    assert any("status=error" in m for _, m in logs)


def test_unexplained_degradation_still_pages(degraded_env):
    logs, alerts = degraded_env

    daemon_mod._note_degraded_health(
        "admin :3456", {"status": "degraded"}, http_code=200)

    assert len(alerts) == 1
    assert "没说具体原因" in alerts[0]


def test_degraded_recovery_still_rearms(degraded_env):
    logs, alerts = degraded_env
    daemon_mod._probe_alert_stamps["admin :3456|degraded"] = time.time()

    daemon_mod._note_degraded_health("admin :3456", {"status": "ok"})

    assert "admin :3456|degraded" not in daemon_mod._probe_alert_stamps
    assert alerts == []


# ── main-loop wiring (the part unit tests above can't exercise) ──

def test_main_loop_wires_new_checks():
    src = inspect.getsource(daemon_mod.main)
    assert "_probe_manifest_criticals()" in src
    assert "_check_diag_staleness()" in src
    assert "_maybe_reset_restart_budget(result)" in src
