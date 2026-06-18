import json
from pathlib import Path

from core import conversation_audit as audit


def test_audit_ingests_logs_and_derives_issues(tmp_path):
    log = tmp_path / "jarvis.log"
    log.write_text(
        "\n".join([
            "[2026-06-16 14:01:06] [INFO] Event: msg_type=text content_len=2 mid=om_1 chat_type=p2p content_head=hi",
            "[2026-06-16 14:01:06] [INFO] [37cad35c-f395-5cdd-babe-97fbef249e1c] Calling primary Claude Code model=opus",
            "[2026-06-16 14:01:14] [WARN] [37cad35c-f395-5cdd-babe-97fbef249e1c] Final empty/error answer from Claude (74 chars after 1 attempts)",
            "[2026-06-16 14:01:14] [WARN] [37cad35c-f395-5cdd-babe-97fbef249e1c] Suppressed content: You've hit your monthly spend limit",
            "[2026-06-16 14:02:00] [INFO] [37cad35c-f395-5cdd-babe-97fbef249e1c] Session busy, waiting... (30s)",
            "[2026-06-16 14:02:30] [INFO] [37cad35c-f395-5cdd-babe-97fbef249e1c] Session busy, waiting... (60s)",
            "[2026-06-16 14:03:00] [INFO] [37cad35c-f395-5cdd-babe-97fbef249e1c] Session busy, waiting... (90s)",
            "[2026-06-16 14:04:00] [INFO] Event: msg_type=text content_len=12 mid=om_2 chat_type=p2p content_head=这个我也没有收到，这也太差了",
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
