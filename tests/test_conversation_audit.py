import json
from pathlib import Path
from datetime import datetime, timedelta, timezone

from core import conversation_audit as audit
from core.timeutil import now_local


def test_audit_ingests_logs_and_derives_issues(tmp_path):
    log = tmp_path / "jarvis.log"
    base = now_local() - timedelta(minutes=10)
    ts = [(base + timedelta(seconds=i)).strftime("%Y-%m-%d %H:%M:%S") for i in range(8)]
    log.write_text(
        "\n".join([
            f"[{ts[0]}] [INFO] Event: msg_type=text content_len=2 mid=om_1 chat_type=p2p content_head=hi",
            f"[{ts[1]}] [INFO] [37cad35c-f395-5cdd-babe-97fbef249e1c] Calling primary Claude Code model=opus",
            f"[{ts[2]}] [WARN] [37cad35c-f395-5cdd-babe-97fbef249e1c] Final empty/error answer from Claude (74 chars after 1 attempts)",
            f"[{ts[3]}] [WARN] [37cad35c-f395-5cdd-babe-97fbef249e1c] Suppressed content: You've hit your monthly spend limit",
            f"[{ts[4]}] [INFO] [37cad35c-f395-5cdd-babe-97fbef249e1c] Session busy, waiting... (30s)",
            f"[{ts[5]}] [INFO] [37cad35c-f395-5cdd-babe-97fbef249e1c] Session busy, waiting... (60s)",
            f"[{ts[6]}] [INFO] [37cad35c-f395-5cdd-babe-97fbef249e1c] Session busy, waiting... (90s)",
            f"[{ts[7]}] [INFO] Event: msg_type=text content_len=12 mid=om_2 chat_type=p2p content_head=这个我也没有收到，这也太差了",
        ]),
        encoding="utf-8",
    )
    paths = audit.AuditPaths(
        jarvis_dir=tmp_path,
        log_paths=[log],
        session_dirs=[],
        db_path=tmp_path / "audit.db",
    )

    run_id = audit.run_audit(paths, hours=48)
    report = audit.render_report(paths.db_path, run_id)

    assert "Provider/account-limit text reached" in report
    assert "same-session" in report or "Same-session" in report
    assert "important signals were not surfaced" in report
    assert "Issues derived: 3" in report


def test_report_can_be_written_from_cli(tmp_path, monkeypatch):
    log = tmp_path / "jarvis.log"
    log.write_text(
        "[2026-06-16 14:01:14] [WARN] [sid] Suppressed content: You've hit your monthly spend limit\n",
        encoding="utf-8",
    )
    paths = audit.AuditPaths(
        jarvis_dir=tmp_path,
        log_paths=[log],
        session_dirs=[],
        db_path=tmp_path / "audit.db",
    )
    monkeypatch.setattr(audit, "default_paths", lambda: paths)
    report = tmp_path / "report.md"

    assert audit.main(["--hours", "48", "--report", str(report)]) == 0
    assert report.exists()
    assert "Conversation Audit PRD" in report.read_text(encoding="utf-8")


def test_audit_flags_user_visible_provider_and_empty_replies(tmp_path):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    session = session_dir / "turn.jsonl"
    rows = [
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": "🔧 You've hit your monthly spend limit · raise it at claude.ai/settings/usage",
            },
            "timestamp": "2026-06-18T10:00:00.000Z",
        },
        {
            "type": "assistant",
            "message": {"role": "assistant", "content": "No response requested."},
            "timestamp": "2026-06-18T10:01:00.000Z",
        },
    ]
    session.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows),
        encoding="utf-8",
    )
    paths = audit.AuditPaths(
        jarvis_dir=tmp_path,
        log_paths=[],
        session_dirs=[session_dir],
        db_path=tmp_path / "audit.db",
    )

    run_id = audit.run_audit(paths, hours=48)
    report = audit.render_report(paths.db_path, run_id)

    assert "progress_provider_error_leak" in report
    assert "empty_reply_user_visible" in report
    assert "Issues derived: 2" in report


def test_audit_ingests_daemon_instability(tmp_path):
    daemon = tmp_path / "daemon.log"
    daemon.write_text(
        "\n".join([
            "[2026-06-18 11:00:00] [WARN] Observed component DOWN: admin :3456",
            "[2026-06-18 11:01:00] [WARN] Health check failed (2x): ['bot.sh is not running']",
            "[2026-06-18 11:02:00] [WARN] BRAIN-DEAD heartbeat: cross-session-sync last_success stale",
        ]),
        encoding="utf-8",
    )
    paths = audit.AuditPaths(
        jarvis_dir=tmp_path,
        log_paths=[daemon],
        session_dirs=[],
        db_path=tmp_path / "audit.db",
    )

    run_id = audit.run_audit(paths, hours=48)
    report = audit.render_report(paths.db_path, run_id)

    assert "guardian_runtime_instability" in report
    assert "admin :3456" in report
    assert "Status: `open`" in report


def test_session_ingest_filters_messages_by_timestamp(tmp_path):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    session = session_dir / "turn.jsonl"
    now = datetime.now(timezone.utc)
    old_ts = (now - timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    new_ts = (now - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    rows = [
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": "You've hit your monthly spend limit · raise it at claude.ai/settings/usage",
            },
            "timestamp": old_ts,
        },
        {
            "type": "assistant",
            "message": {"role": "assistant", "content": "No response requested."},
            "timestamp": new_ts,
        },
    ]
    session.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows),
        encoding="utf-8",
    )
    paths = audit.AuditPaths(
        jarvis_dir=tmp_path,
        log_paths=[],
        session_dirs=[session_dir],
        db_path=tmp_path / "audit.db",
    )

    run_id = audit.run_audit(paths, hours=1)
    report = audit.render_report(paths.db_path, run_id)

    assert "empty_reply_user_visible" in report
    assert "provider_error_as_answer" not in report
    assert "Issues derived: 1" in report


def test_log_ingest_interprets_timestamps_as_local_time(tmp_path):
    log = tmp_path / "jarvis.log"
    old_ts = (now_local() - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S")
    new_ts = (now_local() - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
    log.write_text(
        "\n".join([
            f"[{old_ts}] [WARN] [37cad35c-f395-5cdd-babe-97fbef249e1c] Suppressed content: You've hit your monthly spend limit",
            f"[{new_ts}] [WARN] [37cad35c-f395-5cdd-babe-97fbef249e1c] Final empty/error answer from Claude (74 chars after 1 attempts)",
        ]),
        encoding="utf-8",
    )
    paths = audit.AuditPaths(
        jarvis_dir=tmp_path,
        log_paths=[log],
        session_dirs=[],
        db_path=tmp_path / "audit.db",
    )

    run_id = audit.run_audit(paths, hours=1)
    report = audit.render_report(paths.db_path, run_id)

    assert "restart_syntax_regression" not in report
    assert "provider_error_as_answer" not in report
    assert "No issues detected" in report


def test_timestamped_shell_errors_still_flag_restart_regressions(tmp_path):
    log = tmp_path / "jarvis.log"
    ts = (now_local() - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
    log.write_text(
        f"[{ts}] [ERROR] /repo/bot.sh: line 1735: syntax error near unexpected token `}}'\n",
        encoding="utf-8",
    )
    paths = audit.AuditPaths(
        jarvis_dir=tmp_path,
        log_paths=[log],
        session_dirs=[],
        db_path=tmp_path / "audit.db",
    )

    run_id = audit.run_audit(paths, hours=1)
    report = audit.render_report(paths.db_path, run_id)

    assert "restart_syntax_regression" in report
