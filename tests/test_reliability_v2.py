"""Reliability v2 tests — REQ-39/40/41/42/51.

Covers: component manifest checks, the deterministic self-diagnostic alert
path, truth watermarks (last_success vs the synthetic last_run), pre-script
failure feeding the circuit breaker, and the daemon deploy guard.
"""

import json
import os
import time
from pathlib import Path

import pytest


# ── REQ-40: component manifest ──────────────────────────────────────────

def test_components_manifest_loads_and_covers_critical_set():
    from core.components import load_manifest
    comps = load_manifest()
    names = {c["name"] for c in comps}
    # The audit's dead zones must all be present — a component not listed
    # here can die silently again.
    for required in ("dashboard", "admin", "ef-stream", "lark-sidecar",
                     "bot", "heartbeat-loop", "session-backup",
                     "conversation-audit"):
        assert required in names, f"components.yaml missing {required}"
    # The 23-day corpse and the silent stream must be critical (daemon probes)
    crit = {c["name"] for c in comps if c.get("critical")}
    assert "dashboard" in crit and "ef-stream" in crit
    # REQ-82: the audit had no scheduler mount and sat idle for 13 days —
    # Freshness must come from the latest completed audit, not database mtime:
    # migrations and issue resolution also write the file. One missed daily
    # run is tolerated; two pages. The observation layer stays non-critical.
    ca = next(c for c in comps if c["name"] == "conversation-audit")
    assert ca["check"] == "audit_age"
    assert ca["path"] == "data/conversation_audit.db"
    assert ca["max_age_hours"] == 48
    assert not ca.get("critical", False)


def test_components_check_types(tmp_path):
    from core.components import check_components
    manifest = tmp_path / "components.yaml"
    pid_file = tmp_path / "alive.pid"
    pid_file.write_text(f"{os.getpid()} token123")
    stamp = tmp_path / "stamp"
    stamp.write_text("x")
    manifest.write_text(f"""
components:
  - name: alive-pid
    check: pid
    path: alive.pid
    critical: true
  - name: dead-pid
    check: pid
    path: missing.pid
  - name: fresh-file
    check: file_age
    path: stamp
    max_age_hours: 1
  - name: unknown-type
    check: quantum
""")
    results = {r["name"]: r for r in
               check_components(manifest_path=manifest, root=tmp_path)}
    assert results["alive-pid"]["ok"] is True          # two-field pidfile parsed
    assert results["dead-pid"]["ok"] is False
    assert results["fresh-file"]["ok"] is True
    assert results["unknown-type"]["ok"] is False      # reported, not crashed

    # Stale file flips to failing
    old = time.time() - 7200
    os.utime(stamp, (old, old))
    results = {r["name"]: r for r in
               check_components(manifest_path=manifest, root=tmp_path)}
    assert results["fresh-file"]["ok"] is False


def test_components_pgrep_filters_to_owner_pidfile(tmp_path, monkeypatch):
    from core.components import check_components

    (tmp_path / ".bot.pid").write_text("100 123")
    manifest = tmp_path / "components.yaml"
    manifest.write_text("""
components:
  - name: sidecar
    check: pgrep
    pattern: lark_event_sidecar.py
    owned_by_pidfile: .bot.pid
    critical: true
""")

    class Result:
        def __init__(self, stdout="", returncode=0):
            self.stdout = stdout
            self.returncode = returncode

    def fake_run(cmd, **kwargs):
        assert cmd[:3] == ["ps", "ax", "-o"]
        if cmd[3] == "pid=,command=":
            return Result(
                "200 python3 /repo/scripts/lark_event_sidecar.py\n"
                "225 /Users/pascal/.local/bin/claude --system-prompt lark_event_sidecar.py\n"
                "300 python3 /repo/scripts/lark_event_sidecar.py\n"
            )
        return Result(
            "100 1 bash /repo/bot.sh\n"
            "200 1 python3 /repo/scripts/lark_event_sidecar.py\n"
            "225 100 /Users/pascal/.local/bin/claude --system-prompt lark_event_sidecar.py\n"
            "250 100 bash pipeline subshell\n"
            "300 250 python3 /repo/scripts/lark_event_sidecar.py\n"
        )

    monkeypatch.setattr("core.components.subprocess.run", fake_run)
    result = check_components(manifest_path=manifest, root=tmp_path)[0]
    assert result["ok"] is True
    assert "300" in result["detail"]
    assert "200" not in result["detail"]
    assert "225" not in result["detail"]


def test_components_report_emits_warning_lines(tmp_path):
    from core.components import check_components, format_report
    manifest = tmp_path / "components.yaml"
    manifest.write_text("""
components:
  - name: ghost
    check: pid
    path: nope.pid
    critical: true
""")
    report = format_report(check_components(manifest_path=manifest, root=tmp_path))
    # ⚠️ lines are the contract with the REQ-39 alert post-script
    assert "⚠️ ghost" in report
    assert "[critical]" in report


def test_components_critical_filter(tmp_path):
    from core.components import check_components
    manifest = tmp_path / "components.yaml"
    manifest.write_text("""
components:
  - name: a
    check: pid
    path: x.pid
    critical: true
  - name: b
    check: pid
    path: y.pid
    critical: false
""")
    names = [r["name"] for r in
             check_components(critical_only=True, manifest_path=manifest,
                              root=tmp_path)]
    assert names == ["a"]


# ── REQ-39: deterministic self-diagnostic alert ─────────────────────────

def _load_diag_post():
    import importlib.util
    root = Path(__file__).parent.parent
    spec = importlib.util.spec_from_file_location(
        "self_diagnostic_post", root / "tasks" / "self_diagnostic_post.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_diag_extract_warnings_scrubs_gate_triggers():
    dp = _load_diag_post()
    pre = (
        "=== SYSTEM HEALTH CHECK ===\n"
        "✓ all good here\n"
        "⚠️ repos-sync: pre-script failing (pre_timeout ×5) — channel DEAD\n"
        "  ⚠️ backup: age 60.0h (max 48h)\n"
        "⚠️ stream saw: API Error: 403 from upstream\n"
    )
    warns = dp.extract_warnings(pre)
    assert len(warns) == 3
    # The proactive error gate must never eat the alert about an error
    assert all("API Error" not in w for w in warns)
    assert any("repos-sync" in w for w in warns)


def test_diag_user_id_accepts_production_config_key(tmp_path, monkeypatch):
    dp = _load_diag_post()
    monkeypatch.setattr(dp, "ROOT", tmp_path)
    (tmp_path / "jarvis.yaml").write_text("lark:\n  user_id: ou_production\n")
    assert dp._user_id() == "ou_production"


def test_diag_alert_dedup_window(tmp_path, monkeypatch):
    dp = _load_diag_post()
    monkeypatch.setattr(dp, "STAMP", tmp_path / "stamp.json")
    sent = []
    monkeypatch.setattr(dp, "_send", lambda text, uid: (sent.append(text), True)[1])
    monkeypatch.setattr(dp, "_user_id", lambda: "ou_test")
    pre_file = tmp_path / "pre.txt"
    pre_file.write_text("⚠️ dashboard: unreachable\n")
    monkeypatch.setenv("DIAG_PRE_FILE", str(pre_file))
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO("HEARTBEAT_OK"))

    dp.main()
    assert len(sent) == 1
    assert "自诊断" in sent[0] and "dashboard" in sent[0]

    # Second run inside the 4h window: suppressed
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO("HEARTBEAT_OK"))
    dp.main()
    assert len(sent) == 1


def test_diag_no_warnings_no_alert(tmp_path, monkeypatch):
    dp = _load_diag_post()
    monkeypatch.setattr(dp, "STAMP", tmp_path / "stamp.json")
    sent = []
    monkeypatch.setattr(dp, "_send", lambda text, uid: (sent.append(text), True)[1])
    monkeypatch.setattr(dp, "_user_id", lambda: "ou_test")
    pre_file = tmp_path / "pre.txt"
    pre_file.write_text("✓ everything healthy\n")
    monkeypatch.setenv("DIAG_PRE_FILE", str(pre_file))
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(""))
    dp.main()
    assert sent == []


def test_silent_tasks_with_report_prompts_have_post_alert_path():
    """Rule (REQ-39): a SILENT task whose prompt promises reports MUST have a
    deterministic post-script alert path — otherwise its alarms are muted."""
    from core.heartbeat import HeartbeatRunner, parse_heartbeat
    root = Path(__file__).parent.parent
    tasks = {t["name"]: t for t in parse_heartbeat(root / "HEARTBEAT.md")}
    for name in HeartbeatRunner.SILENT_TASKS:
        t = tasks.get(name)
        if t and "report" in t["prompt"].lower():
            assert t["post"], (
                f"SILENT task {name} promises reports but has no post-script "
                "alert path — its alarms can never reach the user")


# ── REQ-51: truth watermark ─────────────────────────────────────────────

def test_taskstate_persists_truth_watermark():
    from core.task_protocol import TaskState
    ts = TaskState()
    ts.last_run = 1000
    ts.last_success = 900
    ts.last_status = "pre_timeout"
    d = ts.to_dict()
    assert d["last_success"] == 900
    assert d["last_status"] == "pre_timeout"
    rt = TaskState.from_dict(d)
    assert rt.last_success == 900 and rt.last_status == "pre_timeout"


def test_watermarks_flag_pre_failing_channel(tmp_path):
    """repos-sync class: pre-script fails 100% of runs but the synthetic
    last_run stays fresh — the report must flag it anyway."""
    from core.watermarks import channel_watermark_report
    hb = tmp_path / "HEARTBEAT.md"
    hb.write_text("### repos-sync\n- interval: 6h\n- prompt: x\n")
    now = int(time.time())
    state = {"repos-sync": {
        "last_run": now - 60,                  # scheduler watermark: fresh
        "last_success": now - 5 * 86400,       # truth: dead for 5 days
        "last_status": "pre_timeout",
        "circuit": {"consecutive_failures": 5, "last_failure_time": now,
                     "disabled_until": 0, "total_failures": 19, "total_runs": 19},
    }}
    (tmp_path / "heartbeat_state.json").write_text(json.dumps(state))
    report = channel_watermark_report(tmp_path, hb)
    assert "repos-sync" in report
    assert "⚠️" in report
    assert "DEAD" in report or "STARVED" in report


def test_watermarks_healthy_channel_quiet(tmp_path):
    from core.watermarks import channel_watermark_report
    hb = tmp_path / "HEARTBEAT.md"
    hb.write_text("### good-task\n- interval: 1h\n- prompt: x\n")
    now = int(time.time())
    state = {"good-task": {"last_run": now - 60, "last_success": now - 60,
                            "last_status": "ok"}}
    (tmp_path / "heartbeat_state.json").write_text(json.dumps(state))
    report = channel_watermark_report(tmp_path, hb)
    assert "good-task" not in report or "⚠️" not in report


# ── REQ-51: pre-script failure feeds the breaker ────────────────────────

def test_pre_timeout_records_circuit_failure(tmp_path, monkeypatch):
    from core.heartbeat import HeartbeatRunner
    hb = tmp_path / "HEARTBEAT.md"
    hb.write_text("### t\n- interval: 1h\n- pre: tasks/pre.sh\n- prompt: x\n")
    jarvis_dir = tmp_path / "jarvis"
    (jarvis_dir / "tasks").mkdir(parents=True)
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    runner = HeartbeatRunner(jarvis_dir=jarvis_dir, heartbeat_file=hb,
                             state_file=tmp_path / "state.json",
                             memory_dir=memory_dir, idle_judge=False)
    # Pre-script that "fails" — simulate by patching run_script outcome
    def fake_run_script(path, stdin_data=""):
        runner._last_script_outcome = "timeout"
        return ""
    monkeypatch.setattr(runner, "run_script", fake_run_script)
    monkeypatch.setattr(runner, "claude_call", lambda p: "HEARTBEAT_OK")
    runner.run_cycle(force=True)
    state = runner.load_state()
    assert state["t"]["last_status"] == "pre_timeout"
    assert state["t"]["circuit"]["consecutive_failures"] == 1


def test_nonzero_pre_with_json_output_is_still_a_failure(tmp_path, monkeypatch):
    from core.heartbeat import HeartbeatRunner

    hb = tmp_path / "HEARTBEAT.md"
    hb.write_text(
        "### provider-canary\n- interval: 1h\n"
        "- pre: tasks/provider.sh\n- prompt: x\n"
    )
    jarvis_dir = tmp_path / "jarvis"
    (jarvis_dir / "tasks").mkdir(parents=True)
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    runner = HeartbeatRunner(
        jarvis_dir=jarvis_dir,
        heartbeat_file=hb,
        state_file=tmp_path / "state.json",
        memory_dir=memory_dir,
        idle_judge=False,
    )

    def failed_with_output(_path, stdin_data=""):
        runner._last_script_outcome = "nonzero"
        return '{"ok": false}'

    monkeypatch.setattr(runner, "run_script", failed_with_output)
    runner.run_cycle(force=True)

    state = runner.load_state()["provider-canary"]
    assert state["last_status"] == "pre_nonzero"
    assert state.get("last_success", 0) == 0


def test_tier0_post_failure_is_not_recorded_as_success(
    tmp_path, monkeypatch, capsys
):
    from core.heartbeat import HeartbeatRunner

    hb = tmp_path / "HEARTBEAT.md"
    hb.write_text(
        "### calendar-sync\n- interval: 1h\n"
        "- pre: tasks/pre.sh\n- post: tasks/post.sh\n- prompt: x\n"
    )
    jarvis_dir = tmp_path / "jarvis"
    (jarvis_dir / "tasks").mkdir(parents=True)
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    runner = HeartbeatRunner(
        jarvis_dir=jarvis_dir,
        heartbeat_file=hb,
        state_file=tmp_path / "state.json",
        memory_dir=memory_dir,
        idle_judge=False,
    )

    def scripts(path, stdin_data=""):
        if path.endswith("pre.sh"):
            runner._last_script_outcome = "ok"
            return "payload"
        runner._last_script_outcome = "nonzero"
        return '{"ok": false}'

    monkeypatch.setattr(runner, "run_script", scripts)
    runner.run_cycle(force=True)

    state = runner.load_state()["calendar-sync"]
    assert state["last_status"] == "post_nonzero"
    assert state.get("last_success", 0) == 0
    assert "FAILED (calendar-sync:post_nonzero)" in capsys.readouterr().err


# ── REQ-42: daemon deploy guard ─────────────────────────────────────────

def test_daemon_deploy_guard(tmp_path, monkeypatch):
    import daemon as daemon_mod
    monkeypatch.setattr(daemon_mod, "JARVIS_DIR", tmp_path)
    # Sever every live-machine probe (7/8 audit): BOT_PID_FILE binds the real
    # .bot.pid at import, check_health calls _bot_pid directly (never
    # _is_bot_alive), _find_last_heartbeat reads the real /tmp restart log,
    # and last_wake_time is process-global — unhealthy must come from these
    # stubs, not from whatever the production stack happens to be doing.
    monkeypatch.setattr(daemon_mod, "BOT_PID_FILE", tmp_path / ".bot.pid")
    monkeypatch.setattr(daemon_mod, "_bot_pid", lambda: None)
    monkeypatch.setattr(daemon_mod, "_find_last_heartbeat", lambda: None)
    monkeypatch.setattr(daemon_mod, "last_wake_time", 0.0)
    # Fresh .deploying → suspended, healthy
    (tmp_path / ".deploying").write_text("")
    result = daemon_mod.check_health()
    assert result["healthy"] is True
    assert "deploy" in result.get("note", "")
    # Stale .deploying (>30min) → ignored and removed, checks run again
    old = time.time() - 3600
    os.utime(tmp_path / ".deploying", (old, old))
    result = daemon_mod.check_health()
    assert result["healthy"] is False
    assert "bot.sh is not running" in result["issues"]
    assert not (tmp_path / ".deploying").exists()


def test_empty_pre_advances_last_success(tmp_path, monkeypatch):
    """Red-team fix: a clean empty pre (exit 0, nothing to do) is a HEALTHY
    cycle and must advance last_success — else empty-pre-dominant tasks
    (intention-check, memory-hourly) are falsely flagged STARVED."""
    from core.heartbeat import HeartbeatRunner
    hb = tmp_path / "HEARTBEAT.md"
    hb.write_text("### t\n- interval: 1h\n- pre: tasks/pre.sh\n- prompt: x\n")
    jarvis_dir = tmp_path / "jarvis"
    (jarvis_dir / "tasks").mkdir(parents=True)
    memory_dir = tmp_path / "memory"; memory_dir.mkdir()
    runner = HeartbeatRunner(jarvis_dir=jarvis_dir, heartbeat_file=hb,
                             state_file=tmp_path / "state.json",
                             memory_dir=memory_dir, idle_judge=False)
    def fake_run_script(path, stdin_data=""):
        runner._last_script_outcome = "ok"   # clean exit, just empty output
        return ""
    monkeypatch.setattr(runner, "run_script", fake_run_script)
    monkeypatch.setattr(runner, "claude_call", lambda p: "HEARTBEAT_OK")
    runner.run_cycle(force=True)
    state = runner.load_state()
    assert state["t"]["last_status"] == "empty_pre"
    assert state["t"]["last_success"] > 0       # healthy empty advances truth watermark


def test_pre_timeout_does_not_advance_last_success(tmp_path, monkeypatch):
    """Contrast: a pre TIMEOUT leaves last_success stale (the real channel-dead
    signal watermarks must catch)."""
    from core.heartbeat import HeartbeatRunner
    hb = tmp_path / "HEARTBEAT.md"
    hb.write_text("### t\n- interval: 1h\n- pre: tasks/pre.sh\n- prompt: x\n")
    jarvis_dir = tmp_path / "jarvis"
    (jarvis_dir / "tasks").mkdir(parents=True)
    memory_dir = tmp_path / "memory"; memory_dir.mkdir()
    runner = HeartbeatRunner(jarvis_dir=jarvis_dir, heartbeat_file=hb,
                             state_file=tmp_path / "state.json",
                             memory_dir=memory_dir, idle_judge=False)
    def fake_run_script(path, stdin_data=""):
        runner._last_script_outcome = "timeout"
        return ""
    monkeypatch.setattr(runner, "run_script", fake_run_script)
    monkeypatch.setattr(runner, "claude_call", lambda p: "HEARTBEAT_OK")
    runner.run_cycle(force=True)
    state = runner.load_state()
    assert state["t"]["last_status"] == "pre_timeout"
    assert state["t"].get("last_success", 0) == 0   # stays stale → STARVED-detectable


# ── REQ-109: self-diagnostic content-aware dedup ─────────────────────────

def test_diag_should_alert_content_aware():
    import importlib.util
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(
        "self_diagnostic_post_req109", root / "tasks" / "self_diagnostic_post.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    W = ["⚠️ a 挂了", "⚠️ b 超时"]
    now = 1_000_000.0
    h4, h5, h25 = 4 * 3600, 5 * 3600, 25 * 3600

    # no warnings → never
    assert not mod.should_alert([], {}, now=now)
    # empty stamp (never alerted) → alert
    assert mod.should_alert(W, {}, now=now)
    # inside 4h hard floor → suppressed even with new content
    assert not mod.should_alert(W, {"ts": now - h4 + 60, "lines": []}, now=now)
    # past floor, same content → suppressed (this was the 8h relay ping-pong)
    assert not mod.should_alert(W, {"ts": now - h5, "lines": W}, now=now)
    # past floor, NEW line → alert
    assert mod.should_alert(W + ["⚠️ c 新问题"], {"ts": now - h5, "lines": W},
                            now=now)
    # unchanged content, >24h → one daily reminder
    assert mod.should_alert(W, {"ts": now - h25, "lines": W}, now=now)
