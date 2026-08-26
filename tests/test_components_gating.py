"""Optional-feature gating in core/components.py (2026-07-13 audit).

A collaborator's default install alarmed [critical] forever on ef-stream /
lark-sidecar / admin — features doctor.sh explicitly calls optional. The
manifest now declares preconditions (requires_cmd / requires_file /
requires_config); an unmet precondition means SKIPPED (ok, never ⚠️).
"""

import json
import os
import sqlite3
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import core.components as components
from core.components import check_components, format_report


def _manifest(tmp_path, body: str):
    p = tmp_path / "components.yaml"
    p.write_text("components:\n" + body)
    return p


def test_requires_cmd_missing_skips(tmp_path):
    m = _manifest(tmp_path, """
  - name: ef-stream
    check: pgrep
    pattern: "core.ef_stream_loop"
    critical: true
    requires_cmd: definitely-not-a-real-binary-xyz
""")
    (r,) = check_components(manifest_path=m, root=tmp_path)
    assert r["ok"] is True
    assert r["skipped"] is True
    assert "not installed" in r["detail"]


def test_requires_file_missing_skips(tmp_path):
    m = _manifest(tmp_path, """
  - name: webview
    check: http
    url: "http://127.0.0.1:1/"
    critical: true
    requires_file: no/such/plist.plist
""")
    (r,) = check_components(manifest_path=m, root=tmp_path)
    assert r["ok"] is True and r["skipped"] is True


def test_requires_config_unset_skips_and_enabled_arms(tmp_path):
    body = """
  - name: admin
    check: file_age
    path: does-not-exist
    max_age_hours: 1
    critical: true
    requires_config: admin.enabled
"""
    m = _manifest(tmp_path, body)
    # No jarvis.yaml at root → default admin.enabled=False → skipped
    (r,) = check_components(manifest_path=m, root=tmp_path)
    assert r["ok"] is True and r["skipped"] is True

    # Enabled in config → gate opens → the underlying check runs (and fails,
    # proving we reached it)
    armed = tmp_path / "armed"
    armed.mkdir()
    (armed / "jarvis.yaml").write_text("admin:\n  enabled: true\n")
    m2 = _manifest(armed, body)
    (r2,) = check_components(manifest_path=m2, root=armed)
    assert r2["ok"] is False
    assert "missing" in r2["detail"]


def test_requires_config_equality(tmp_path):
    body = """
  - name: lark-sidecar
    check: file_age
    path: does-not-exist
    max_age_hours: 1
    requires_config: lark.event_backend=sidecar
"""
    (tmp_path / "jarvis.yaml").write_text("lark:\n  event_backend: cli\n")
    m = _manifest(tmp_path, body)
    (r,) = check_components(manifest_path=m, root=tmp_path)
    assert r["skipped"] is True

    other = tmp_path / "sidecar"
    other.mkdir()
    (other / "jarvis.yaml").write_text("lark:\n  event_backend: sidecar\n")
    m2 = _manifest(other, body)
    (r2,) = check_components(manifest_path=m2, root=other)
    assert "skipped" not in r2
    assert r2["ok"] is False  # gate open, real check ran


def test_audit_age_uses_completed_run_not_database_mtime(tmp_path):
    database = tmp_path / "conversation_audit.db"
    stale = (datetime.now(timezone.utc) - timedelta(hours=72)).isoformat()
    db = sqlite3.connect(database)
    db.execute(
        """
        CREATE TABLE audit_runs (
            id INTEGER PRIMARY KEY,
            started_at TEXT NOT NULL,
            since TEXT NOT NULL,
            log_events INTEGER NOT NULL DEFAULT 0,
            session_messages INTEGER NOT NULL DEFAULT 0,
            issues INTEGER NOT NULL DEFAULT 0,
            completed_at TEXT
        )
        """
    )
    db.execute(
        """
        INSERT INTO audit_runs(id,started_at,since,completed_at)
        VALUES (1,?,?,?)
        """,
        (stale, stale, stale),
    )
    db.commit()
    db.close()
    os.utime(database, None)
    manifest = _manifest(tmp_path, """
  - name: conversation-audit
    check: audit_age
    path: conversation_audit.db
    max_age_hours: 48
""")

    (stale_result,) = check_components(
        manifest_path=manifest,
        root=tmp_path,
    )

    assert stale_result["ok"] is False
    assert "completed age 72." in stale_result["detail"]

    recent = datetime.now(timezone.utc).isoformat()
    db = sqlite3.connect(database)
    db.execute(
        """
        INSERT INTO audit_runs(id,started_at,since,completed_at)
        VALUES (2,?,?,?)
        """,
        (recent, recent, recent),
    )
    db.commit()
    db.close()
    old_mtime = (
        datetime.now(timezone.utc) - timedelta(hours=72)
    ).timestamp()
    os.utime(database, (old_mtime, old_mtime))

    (healthy_result,) = check_components(
        manifest_path=manifest,
        root=tmp_path,
    )

    assert healthy_result["ok"] is True
    assert "completed age 0.0h" in healthy_result["detail"]


def test_audit_age_migrates_legacy_database_before_health_read(tmp_path):
    database = tmp_path / "conversation_audit.db"
    completed = datetime.now(timezone.utc).isoformat()
    db = sqlite3.connect(database)
    db.execute(
        """
        CREATE TABLE audit_runs (
            id INTEGER PRIMARY KEY,
            started_at TEXT NOT NULL,
            since TEXT NOT NULL,
            log_events INTEGER NOT NULL DEFAULT 0,
            session_messages INTEGER NOT NULL DEFAULT 0,
            issues INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    db.execute(
        "INSERT INTO audit_runs(id,started_at,since) VALUES (1,?,?)",
        (completed, completed),
    )
    db.commit()
    db.close()
    manifest = _manifest(tmp_path, """
  - name: conversation-audit
    check: audit_age
    path: conversation_audit.db
    max_age_hours: 48
""")

    (result,) = check_components(
        manifest_path=manifest,
        root=tmp_path,
    )

    assert result["ok"] is True
    migrated = sqlite3.connect(database)
    value = migrated.execute(
        "SELECT completed_at FROM audit_runs WHERE id=1"
    ).fetchone()[0]
    migrated.close()
    assert value == completed


def test_format_report_marks_skipped_not_warned(tmp_path):
    m = _manifest(tmp_path, """
  - name: ef-stream
    check: pgrep
    pattern: "x"
    critical: true
    requires_cmd: definitely-not-a-real-binary-xyz
""")
    report = format_report(check_components(manifest_path=m, root=tmp_path))
    assert "○ ef-stream" in report
    assert "⚠️" not in report
    assert "skipped" in report


def _delivery_db(tmp_path):
    path = tmp_path / "data" / "jarvis.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.execute(
        """
        CREATE TABLE delivery_envelopes (
            id TEXT PRIMARY KEY,
            state TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            created_epoch REAL NOT NULL,
            next_attempt_epoch REAL,
            delivered_epoch REAL
        )
        """
    )
    return path, db


def test_delivery_health_detects_recent_failure_streak(tmp_path):
    _path, db = _delivery_db(tmp_path)
    now = time.time()
    db.executemany(
        "INSERT INTO delivery_envelopes "
        "(id,state,attempts,created_epoch,next_attempt_epoch) "
        "VALUES (?,?,?,?,?)",
        [
            (f"failed-{index}", "failed", 9, now - index, now - index)
            for index in range(3)
        ],
    )
    db.commit()
    db.close()
    manifest = _manifest(tmp_path, """
  - name: unified-delivery
    check: delivery
    path: data/jarvis.db
    failure_streak: 3
""")

    (result,) = check_components(manifest_path=manifest, root=tmp_path)

    assert result["ok"] is False
    assert "3 consecutive failed" in result["detail"]


def test_delivery_health_recovers_after_real_receipt(tmp_path):
    _path, db = _delivery_db(tmp_path)
    now = time.time()
    db.executemany(
        "INSERT INTO delivery_envelopes "
        "(id,state,attempts,created_epoch,next_attempt_epoch,delivered_epoch) "
        "VALUES (?,?,?,?,?,?)",
        [
            ("old-failure", "failed", 9, now - 30, now - 30, None),
            ("receipt", "delivered", 1, now - 5, None, now - 4),
        ],
    )
    db.commit()
    db.close()
    manifest = _manifest(tmp_path, """
  - name: unified-delivery
    check: delivery
    path: data/jarvis.db
""")

    (result,) = check_components(manifest_path=manifest, root=tmp_path)

    assert result["ok"] is True
    assert "failure streak 0" in result["detail"]


def test_delivery_health_ignores_future_quiet_queue(tmp_path):
    _path, db = _delivery_db(tmp_path)
    now = time.time()
    db.execute(
        "INSERT INTO delivery_envelopes "
        "(id,state,attempts,created_epoch,next_attempt_epoch) "
        "VALUES ('quiet','queued',0,?,?)",
        (now - 7200, now + 3600),
    )
    db.commit()
    db.close()
    manifest = _manifest(tmp_path, """
  - name: unified-delivery
    check: delivery
    path: data/jarvis.db
""")

    (result,) = check_components(manifest_path=manifest, root=tmp_path)

    assert result["ok"] is True
    assert "delivery quiet" in result["detail"]


def test_delivery_health_detects_overdue_retry_queue(tmp_path):
    _path, db = _delivery_db(tmp_path)
    now = time.time()
    db.execute(
        "INSERT INTO delivery_envelopes "
        "(id,state,attempts,created_epoch,next_attempt_epoch) "
        "VALUES ('overdue','queued',1,?,?)",
        (now - 1800, now - 1200),
    )
    db.commit()
    db.close()
    manifest = _manifest(tmp_path, """
  - name: unified-delivery
    check: delivery
    path: data/jarvis.db
    max_overdue_seconds: 900
""")

    (result,) = check_components(manifest_path=manifest, root=tmp_path)

    assert result["ok"] is False
    assert "delivery stalled" in result["detail"]
    assert "oldest 20min" in result["detail"]


def test_ef_stream_check_combines_process_and_protocol_health(
        tmp_path, monkeypatch):
    monkeypatch.setattr(
        components,
        "_check_pgrep",
        lambda _comp, _root: (True, "pid 123 owned"),
    )
    state = tmp_path / "data" / "ef_stream_health.json"
    state.parent.mkdir()
    state.write_text(json.dumps({
        "status": "degraded",
        "updated_epoch": time.time(),
        "quiet_streak": 6,
        "detail": "no protocol output",
    }))
    comp = {"path": "data/ef_stream_health.json", "max_age_seconds": 2400}

    assert components._check_ef_stream(comp, tmp_path)[0] is False
    state.write_text(json.dumps({
        "status": "active",
        "updated_epoch": time.time(),
        "quiet_streak": 0,
        "detail": "output observed",
    }))
    assert components._check_ef_stream(comp, tmp_path)[0] is True


def test_ef_stream_connecting_requires_fresh_poll_after_startup_grace(
        tmp_path, monkeypatch):
    now = time.time()
    monkeypatch.setattr(
        components,
        "_check_pgrep",
        lambda _comp, _root: (True, "pid 123 owned"),
    )
    data = tmp_path / "data"
    data.mkdir()
    (data / "ef_stream_health.json").write_text(json.dumps({
        "status": "connecting",
        "updated_epoch": now,
        "started_epoch": now - 700,
        "quiet_streak": 0,
    }))
    comp = {
        "path": "data/ef_stream_health.json",
        "reconcile_path": "data/ef_ingress_health.json",
        "max_age_seconds": 2400,
        "reconcile_max_age_seconds": 900,
        "connect_grace_seconds": 600,
    }

    ok, detail = components._check_ef_stream(comp, tmp_path)
    assert ok is False
    assert "polling safety net" in detail

    (data / "ef_ingress_health.json").write_text(json.dumps({
        "status": "ok",
        "last_success_epoch": now,
    }))
    ok, detail = components._check_ef_stream(comp, tmp_path)
    assert ok is True
    assert "poll verified" in detail


def test_ef_stream_active_path_does_not_depend_on_polling_fallback(
        tmp_path, monkeypatch):
    """8/26 incident: a stale independent poll must not make Guardian kill a
    live stream whose own protocol health is fresh and active."""
    now = time.time()
    monkeypatch.setattr(
        components,
        "_check_pgrep",
        lambda _comp, _root: (True, "pids ['74530'] owned by 9467"),
    )
    data = tmp_path / "data"
    data.mkdir()
    (data / "ef_stream_health.json").write_text(json.dumps({
        "status": "active",
        "updated_epoch": now,
        "started_epoch": now - 10_000,
        "quiet_streak": 0,
    }))
    (data / "ef_ingress_health.json").write_text(json.dumps({
        "status": "error",
        "last_success_epoch": now - 10_000,
    }))
    comp = {
        "path": "data/ef_stream_health.json",
        "reconcile_path": "data/ef_ingress_health.json",
        "max_age_seconds": 2400,
        "reconcile_max_age_seconds": 900,
        "connect_grace_seconds": 600,
    }

    ok, detail = components._check_ef_stream(comp, tmp_path)

    assert ok is True
    assert "active" in detail
    assert "poll error/stale" in detail


def test_ef_stream_missing_process_is_covered_by_fresh_polling_fallback(
        tmp_path, monkeypatch):
    """Real-time is latency, polling is durability: losing the sidecar does
    not become a user-facing outage while reconciliation is current."""
    now = time.time()
    monkeypatch.setattr(
        components,
        "_check_pgrep",
        lambda _comp, _root: (
            False, "not running (bot pid 9467 has no matching child process)"
        ),
    )
    data = tmp_path / "data"
    data.mkdir()
    (data / "ef_ingress_health.json").write_text(json.dumps({
        "status": "ok",
        "last_success_epoch": now,
    }))
    comp = {
        "reconcile_path": "data/ef_ingress_health.json",
        "reconcile_max_age_seconds": 900,
    }

    ok, detail = components._check_ef_stream(comp, tmp_path)

    assert ok is True
    assert "polling fallback verified" in detail
    assert "real-time stream unavailable" in detail


def test_ef_stream_missing_process_and_stale_poll_is_a_real_outage(
        tmp_path, monkeypatch):
    now = time.time()
    monkeypatch.setattr(
        components,
        "_check_pgrep",
        lambda _comp, _root: (False, "not running"),
    )
    data = tmp_path / "data"
    data.mkdir()
    (data / "ef_ingress_health.json").write_text(json.dumps({
        "status": "error",
        "last_success_epoch": now - 10_000,
    }))
    comp = {
        "reconcile_path": "data/ef_ingress_health.json",
        "reconcile_max_age_seconds": 900,
    }

    ok, detail = components._check_ef_stream(comp, tmp_path)

    assert ok is False
    assert "not running" in detail
    assert "polling safety net error/stale" in detail


def test_daemon_critical_probe_ignores_skipped(tmp_path):
    """daemon._probe_manifest_criticals only alerts on ok=False — a skipped
    component must present ok=True through the critical_only path too."""
    m = _manifest(tmp_path, """
  - name: ef-stream
    check: pgrep
    pattern: "x"
    critical: true
    requires_cmd: definitely-not-a-real-binary-xyz
  - name: bot
    check: pid
    path: .bot.pid
    critical: true
""")
    results = check_components(critical_only=True, manifest_path=m,
                               root=tmp_path)
    by_name = {r["name"]: r for r in results}
    assert by_name["ef-stream"]["ok"] is True     # skipped → never pages
    assert by_name["bot"]["ok"] is False          # ungated check still real


def test_taskline_component_checks_readiness_not_launchd_registration(
    tmp_path, monkeypatch,
):
    binary = tmp_path / "taskline-server"
    binary.write_text("binary")
    manifest = _manifest(tmp_path, f"""
  - name: taskline
    check: taskline
    requires_file: {binary}
""")

    monkeypatch.setattr(
        components.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command,
            0,
            json.dumps({"healthy": False, "registered": True}),
            "",
        ),
    )
    (unhealthy,) = check_components(
        manifest_path=manifest, root=tmp_path
    )
    assert unhealthy["ok"] is False

    monkeypatch.setattr(
        components.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command,
            0,
            json.dumps({"healthy": True, "registered": True}),
            "",
        ),
    )
    (healthy,) = check_components(
        manifest_path=manifest, root=tmp_path
    )
    assert healthy["ok"] is True


def test_launchd_installer_skips_optional_taskline_before_bootstrap():
    script = (
        Path(__file__).parent.parent / "scripts" / "launchd" / "install.sh"
    ).read_text(encoding="utf-8")
    guard = script.index(
        '[[ "$label" == "com.pascal.jarvis.taskline"'
    )
    bootstrap = script.index("bootstrap_launchd_job", guard)

    assert guard < bootstrap
    assert "optional Taskline binary not installed" in script


# ── Host-sleep-aware ages (2026-08-19 audit) ─────────────────────────────
# A 39h lid-close turned every age check red at once (companion-voice
# "48.5h", ef-stream "protocol health stale") while the components themselves
# were fine. What these checks mean is "we were up this long and never heard
# from it", so recorded host sleep is discounted.


def test_file_age_discounts_host_sleep(tmp_path):
    from core import hostclock

    stamp = tmp_path / ".last_backup_ok"
    stamp.write_text("")
    now = time.time()
    os.utime(stamp, (now - 47 * 3600, now - 47 * 3600))
    hostclock.record(tmp_path, 40 * 3600, end_epoch=now - 600)

    m = _manifest(tmp_path, """
  - name: session-backup
    check: file_age
    path: .last_backup_ok
    max_age_hours: 24
""")
    (r,) = check_components(manifest_path=m, root=tmp_path)
    assert r["ok"] is True
    assert "host sleep not counted" in r["detail"]


def test_file_age_still_fails_when_the_host_was_up(tmp_path):
    stamp = tmp_path / ".last_backup_ok"
    stamp.write_text("")
    now = time.time()
    os.utime(stamp, (now - 47 * 3600, now - 47 * 3600))

    m = _manifest(tmp_path, """
  - name: session-backup
    check: file_age
    path: .last_backup_ok
    max_age_hours: 24
""")
    (r,) = check_components(manifest_path=m, root=tmp_path)
    assert r["ok"] is False


def test_ef_stream_staleness_discounts_host_sleep(tmp_path, monkeypatch):
    from core import hostclock

    monkeypatch.setattr(components, "_check_pgrep",
                        lambda comp, root: (True, "pids ['1']"))
    now = time.time()
    (tmp_path / "ef_stream_health.json").write_text(json.dumps({
        "status": "active", "quiet_streak": 0,
        "updated_epoch": now - 9 * 3600,
    }))
    hostclock.record(tmp_path, 8.4 * 3600, end_epoch=now - 600)

    m = _manifest(tmp_path, """
  - name: ef-stream
    check: ef_stream
    path: ef_stream_health.json
    max_age_seconds: 2400
""")
    (r,) = check_components(manifest_path=m, root=tmp_path)
    assert r["ok"] is True, r["detail"]
