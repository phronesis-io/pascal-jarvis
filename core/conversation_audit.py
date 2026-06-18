"""SQLite-backed audit of recent Jarvis/Lark conversation quality.

The runtime already emits enough structured-ish evidence to diagnose many
conversation failures: incoming Lark events, session-lock waits, provider
selection, background promotion, safety-filter suppressions, and deploy/restart
syntax errors. This module normalizes those lines into a small SQLite database
and derives issue rows that can be reviewed like an incident table.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from core.search import load_chat_messages


LOG_RE = re.compile(r"^\[(?P<ts>[^]]+)\] \[(?P<level>[^]]+)\] (?P<msg>.*)$")
SESSION_RE = re.compile(r"\[(?P<sid>[0-9a-f-]{36})\]")
EVENT_RE = re.compile(
    r"Event: msg_type=(?P<msg_type>\S+) .*?mid=(?P<message_id>\S+) "
    r"chat_type=(?P<chat_type>\S+) content_head=(?P<head>.*)$"
)

COMPLAINT_PATTERNS = {
    "missed_signal": ("没有收到", "为什么没有推送", "也没有收到", "太差"),
    "hallucination_or_confusion": ("乱说", "不知道你在说什么", "傻逼玩意"),
    "needs_deeper_research": ("全面调研", "所有东西都查明白", "多查查"),
}

PROVIDER_ERROR_NEEDLES = (
    "monthly spend limit",
    "raise it at claude.ai/settings/usage",
    "usage limit",
    "rate limit",
    "api error",
    "failed to authenticate",
    "not logged in",
)

EMPTY_REPLY_NEEDLES = (
    "no response requested",
    "final empty/error answer",
)


@dataclass
class AuditPaths:
    jarvis_dir: Path
    log_paths: list[Path]
    session_dirs: list[Path]
    db_path: Path


def default_paths(jarvis_dir: Path | None = None) -> AuditPaths:
    root = jarvis_dir or Path.cwd()
    return AuditPaths(
        jarvis_dir=root,
        log_paths=[
            root / "jarvis.log",
            root / "daemon.log",
            Path("/tmp/jarvis_restart.log"),
        ],
        session_dirs=[
            Path.home() / ".claude/projects/-Users-pascal-Desktop-jarvis",
            Path.home() / ".claude/projects/-Users-pascal-Desktop-jarvis-repos",
            Path.home() / ".claude/projects/-Users-pascal-Desktop-jarvis-repos-pascal-jarvis",
        ],
        db_path=root / "data/conversation_audit.db",
    )


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS audit_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL,
            since TEXT NOT NULL,
            log_events INTEGER NOT NULL DEFAULT 0,
            session_messages INTEGER NOT NULL DEFAULT 0,
            issues INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS conversation_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            ts TEXT,
            level TEXT,
            session_id TEXT,
            event_type TEXT NOT NULL,
            message_id TEXT,
            content TEXT NOT NULL DEFAULT '',
            metadata TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS idx_conv_events_run_type
            ON conversation_events(run_id, event_type);
        CREATE INDEX IF NOT EXISTS idx_conv_events_session
            ON conversation_events(run_id, session_id, ts);
        CREATE TABLE IF NOT EXISTS session_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            session_id TEXT NOT NULL,
            ts TEXT,
            role TEXT NOT NULL,
            text TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_session_messages_run
            ON session_messages(run_id, session_id, ts);
        CREATE TABLE IF NOT EXISTS audit_issues (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            severity TEXT NOT NULL,
            issue_type TEXT NOT NULL,
            title TEXT NOT NULL,
            evidence TEXT NOT NULL,
            recommendation TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open'
        );
        CREATE INDEX IF NOT EXISTS idx_audit_issues_run
            ON audit_issues(run_id, severity, issue_type);
        """
    )
    return conn


def _parse_ts(raw: str) -> datetime | None:
    try:
        return datetime.strptime(raw, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _event_type(message: str) -> tuple[str, str, dict]:
    meta: dict = {}
    event = EVENT_RE.search(message)
    if event:
        meta = event.groupdict()
        return "incoming_message", event.group("head"), meta
    if "Received:" in message:
        return "handler_received", message.split("Received:", 1)[1].strip(), meta
    if "Calling primary Claude Code model=" in message:
        return "provider_call", message, {"provider": "Claude primary"}
    if "Calling Claude Code backup provider model=" in message:
        return "provider_call", message, {"provider": "Claude backup"}
    if "trying OpenAI fallback" in message:
        return "provider_call", message, {"provider": "GPT fallback"}
    if "Replied (" in message:
        return "reply_sent", message, meta
    if "Suppressed content:" in message:
        return "suppressed_content", message.split("Suppressed content:", 1)[1].strip(), meta
    if "Error-looking answer from" in message:
        return "provider_error_detected", message, meta
    if "Final empty/error answer" in message:
        return "empty_or_error_answer", message, meta
    if "Session busy, waiting" in message:
        return "session_busy_wait", message, meta
    if "Promoted to background job" in message:
        return "background_promoted", message, meta
    if "Observed component DOWN:" in message:
        return "component_down", message, meta
    if "Health check failed" in message:
        return "daemon_health_failed", message, meta
    if "Auto-restart successful" in message:
        return "daemon_auto_restart", message, meta
    if "BRAIN-DEAD heartbeat:" in message:
        return "brain_dead_heartbeat", message, meta
    if "syntax error near unexpected token" in message or "unbound variable" in message:
        return "runtime_shell_error", message, meta
    return "log", message, meta


def ingest_logs(conn: sqlite3.Connection, run_id: int, paths: Iterable[Path],
                since: datetime) -> int:
    count = 0
    seen: set[tuple[str, str, str, str, str]] = set()
    for path in paths:
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            m = LOG_RE.match(line)
            if not m:
                if "syntax error near unexpected token" in line or "unbound variable" in line:
                    ts = ""
                    level = "ERROR"
                    message = line
                else:
                    continue
            else:
                ts = m.group("ts")
                parsed = _parse_ts(ts)
                if parsed and parsed < since:
                    continue
                level = m.group("level")
                message = m.group("msg")
            session = SESSION_RE.search(message)
            event_type, content, metadata = _event_type(message)
            if "message_id" in metadata:
                message_id = metadata["message_id"]
            else:
                message_id = ""
            key = (
                ts,
                session.group("sid") if session else "",
                event_type,
                message_id,
                content,
            )
            if key in seen:
                continue
            seen.add(key)
            conn.execute(
                """
                INSERT INTO conversation_events
                (run_id, ts, level, session_id, event_type, message_id, content, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    ts,
                    level,
                    session.group("sid") if session else "",
                    event_type,
                    message_id,
                    content,
                    json.dumps(metadata, ensure_ascii=False),
                ),
            )
            count += 1
    return count


def ingest_sessions(conn: sqlite3.Connection, run_id: int, session_dirs: Iterable[Path],
                    since: datetime) -> int:
    count = 0
    seen: set[Path] = set()
    for session_dir in session_dirs:
        if not session_dir.is_dir():
            continue
        for path in session_dir.glob("*.jsonl"):
            if path in seen:
                continue
            seen.add(path)
            if datetime.fromtimestamp(path.stat().st_mtime, timezone.utc) < since:
                continue
            for msg in load_chat_messages(path):
                conn.execute(
                    """
                    INSERT INTO session_messages (run_id, session_id, ts, role, text)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (run_id, path.stem, msg.get("timestamp", ""),
                     msg.get("role", ""), msg.get("text", "")),
                )
                count += 1
    return count


def _add_issue(conn: sqlite3.Connection, run_id: int, severity: str,
               issue_type: str, title: str, evidence: str,
               recommendation: str, status: str = "open") -> None:
    conn.execute(
        """
        INSERT INTO audit_issues
        (run_id, severity, issue_type, title, evidence, recommendation, status)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (run_id, severity, issue_type, title, evidence[:2000],
         recommendation, status),
    )


def _has_any(text: str, needles: Iterable[str]) -> bool:
    lowered = text.lower()
    return any(needle in lowered for needle in needles)


def _excerpt(text: str, limit: int = 220) -> str:
    single_line = " ".join(text.split())
    if len(single_line) <= limit:
        return single_line
    return single_line[:limit - 1] + "…"


def _is_direct_provider_surface(text: str) -> bool:
    stripped = text.strip()
    if not stripped or not _has_any(stripped, PROVIDER_ERROR_NEEDLES):
        return False
    first_line = stripped.splitlines()[0].lstrip()
    if len(stripped) <= 220:
        return True
    return first_line.startswith((
        "🔧",
        "🛠",
        "⚙",
        "you've hit your monthly spend limit",
        "you have hit your monthly spend limit",
        "api error",
        "failed to authenticate",
    ))


def _is_direct_empty_surface(text: str) -> bool:
    stripped = text.strip()
    return bool(stripped) and len(stripped) <= 120 and _has_any(stripped, EMPTY_REPLY_NEEDLES)


def _provider_status_for_log(content: str) -> str:
    """Return issue status for provider errors seen in internal logs.

    A suppressed-content line means the error hit the safety boundary, not
    necessarily the user. Provider-error-detected means the newer fallback path
    recognized the failure before answering, so it is treated as fixed evidence.
    """
    if "error-looking answer from" in content.lower():
        return "fixed"
    return "open"


def _add_user_visible_provider_issue(
    conn: sqlite3.Connection,
    run_id: int,
    rows: list[sqlite3.Row],
    issue_type: str,
    title: str,
    recommendation: str,
) -> None:
    if not rows:
        return
    examples = "\n".join(
        f"{row['ts']} session={row['session_id']} text={_excerpt(row['text'])}"
        for row in rows[:5]
    )
    evidence = f"count={len(rows)}\n{examples}"
    _add_issue(conn, run_id, "P0", issue_type, title, evidence, recommendation)


def derive_issues(conn: sqlite3.Connection, run_id: int) -> int:
    start_count = conn.execute(
        "SELECT COUNT(*) FROM audit_issues WHERE run_id=?", (run_id,)
    ).fetchone()[0]

    suppressed = conn.execute(
        """
        SELECT ts, session_id, content FROM conversation_events
        WHERE run_id=? AND event_type='suppressed_content'
        ORDER BY ts DESC LIMIT 10
        """,
        (run_id,),
    ).fetchall()
    for row in suppressed:
        _add_issue(
            conn, run_id, "P0", "provider_error_as_answer",
            "Provider/account-limit text reached the reply safety boundary",
            f"{row['ts']} session={row['session_id']} content={row['content']}",
            "Treat error-looking stdout as a provider failure and continue the fallback chain before user-facing safety copy.",
            status=_provider_status_for_log(row["content"]),
        )

    provider_errors = conn.execute(
        """
        SELECT ts, session_id, content FROM conversation_events
        WHERE run_id=? AND event_type='provider_error_detected'
        ORDER BY ts DESC LIMIT 200
        """,
        (run_id,),
    ).fetchall()
    if provider_errors:
        examples = "\n".join(
            f"{row['ts']} session={row['session_id']} content={_excerpt(row['content'])}"
            for row in provider_errors[:5]
        )
        _add_issue(
            conn, run_id, "P1", "provider_fallback_exercised",
            "Provider/account-limit failure was detected before fallback",
            f"count={len(provider_errors)}\n{examples}",
            "Keep this as regression evidence: any future user-visible provider-error text should fail the conversation audit.",
            status="fixed",
        )

    visible_provider_errors = conn.execute(
        """
        SELECT ts, session_id, role, text FROM session_messages
        WHERE run_id=? AND role='assistant'
        ORDER BY ts DESC LIMIT 200
        """,
        (run_id,),
    ).fetchall()
    direct_provider_rows = [
        row for row in visible_provider_errors
        if _is_direct_provider_surface(row["text"])
    ]
    direct_empty_rows = [
        row for row in visible_provider_errors
        if _is_direct_empty_surface(row["text"])
    ]
    provider_issue_type = (
        "progress_provider_error_leak"
        if any(row["text"].lstrip().startswith(("🔧", "🛠", "⚙")) for row in direct_provider_rows)
        else "provider_error_as_answer"
    )
    _add_user_visible_provider_issue(
        conn, run_id, direct_provider_rows, provider_issue_type,
        "Provider/account-limit text was visible in assistant conversation",
        "Never narrate raw provider/account failures. Classify them as provider failures, advance the fallback chain, and expose only provider/model status when the response is successful.",
    )
    _add_user_visible_provider_issue(
        conn, run_id, direct_empty_rows, "empty_reply_user_visible",
        "Assistant surfaced an empty/no-op response to the user",
        "Treat empty/no-op provider output as a failed turn: retry/fallback internally, then send either a real answer or one concise recovery notice.",
    )

    shell_errors = conn.execute(
        """
        SELECT ts, content FROM conversation_events
        WHERE run_id=? AND event_type='runtime_shell_error'
        ORDER BY ts DESC LIMIT 10
        """,
        (run_id,),
    ).fetchall()
    if shell_errors:
        evidence = "\n".join(f"{r['ts']} {r['content']}" for r in shell_errors[:5])
        _add_issue(
            conn, run_id, "P0", "restart_syntax_regression",
            "Deploy/restart can briefly launch a syntactically broken bot",
            evidence,
            "Keep bash -n in pre-commit/deploy checks and prefer detached restart verification after every bot.sh change.",
            status="fixed",
        )

    daemon_instability = conn.execute(
        """
        SELECT ts, event_type, content FROM conversation_events
        WHERE run_id=?
          AND event_type IN (
            'component_down',
            'daemon_health_failed',
            'brain_dead_heartbeat',
            'daemon_auto_restart'
          )
        ORDER BY ts
        """,
        (run_id,),
    ).fetchall()
    failures = [
        row for row in daemon_instability
        if row["event_type"] in {"component_down", "daemon_health_failed", "brain_dead_heartbeat"}
    ]
    if failures:
        restarts = [row for row in daemon_instability if row["event_type"] == "daemon_auto_restart"]
        last_failure_ts = max((row["ts"] or "") for row in failures)
        last_restart_ts = max((row["ts"] or "") for row in restarts) if restarts else ""
        status = "fixed" if last_restart_ts and last_restart_ts >= last_failure_ts else "open"
        evidence = "\n".join(
            f"{r['ts']} {r['event_type']}: {r['content']}" for r in failures[-5:]
        )
        _add_issue(
            conn, run_id, "P1", "guardian_runtime_instability",
            "Guardian observed Jarvis runtime instability during the audit window",
            evidence,
            "Make daemon health failures first-class audit inputs and require post-deploy checks for admin :3456, bot.sh, listener, and heartbeat task success.",
            status=status,
        )

    busy = conn.execute(
        """
        SELECT session_id, COUNT(*) AS n, MIN(ts) AS first_ts, MAX(ts) AS last_ts
        FROM conversation_events
        WHERE run_id=? AND event_type='session_busy_wait'
        GROUP BY session_id HAVING n >= 3
        """,
        (run_id,),
    ).fetchall()
    for row in busy:
        _add_issue(
            conn, run_id, "P1", "silent_same_session_queue",
            "Follow-up messages can wait behind a busy session with no user-visible queue state",
            f"session={row['session_id']} waits={row['n']} first={row['first_ts']} last={row['last_ts']}",
            "Send one reply-to queue acknowledgement after the first 30s wait; avoid repeated countdown spam.",
            status="fixed",
        )

    incoming = conn.execute(
        """
        SELECT ts, message_id, content FROM conversation_events
        WHERE run_id=? AND event_type='incoming_message'
        ORDER BY ts
        """,
        (run_id,),
    ).fetchall()
    for row in incoming:
        text = row["content"]
        for issue_type, patterns in COMPLAINT_PATTERNS.items():
            if any(p in text for p in patterns):
                severity = "P1" if issue_type != "needs_deeper_research" else "P2"
                _add_issue(
                    conn, run_id, severity, issue_type,
                    {
                        "missed_signal": "User complained that important signals were not surfaced",
                        "hallucination_or_confusion": "User challenged an answer as confusing or fabricated",
                        "needs_deeper_research": "User asked for a deeper, broader investigation pass",
                    }[issue_type],
                    f"{row['ts']} message_id={row['message_id']} content={text}",
                    {
                        "missed_signal": "Route this to the PGC/source-health audit: compare feed ingestion, ranking, and delivery gates for the cited item.",
                        "hallucination_or_confusion": "When challenged, prefer evidence-first correction and cite the exact source/session artifact used.",
                        "needs_deeper_research": "Auto-promote broad research requests earlier and send progress checkpoints with sources inspected.",
                    }[issue_type],
                )

    after_count = conn.execute(
        "SELECT COUNT(*) FROM audit_issues WHERE run_id=?", (run_id,)
    ).fetchone()[0]
    return after_count - start_count


def run_audit(paths: AuditPaths, hours: int = 24) -> int:
    since_dt = datetime.now(timezone.utc) - timedelta(hours=hours)
    conn = connect(paths.db_path)
    started = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        "INSERT INTO audit_runs (started_at, since) VALUES (?, ?)",
        (started, since_dt.isoformat()),
    )
    run_id = int(cur.lastrowid)
    log_events = ingest_logs(conn, run_id, paths.log_paths, since_dt)
    session_messages = ingest_sessions(conn, run_id, paths.session_dirs, since_dt)
    issues = derive_issues(conn, run_id)
    conn.execute(
        """
        UPDATE audit_runs
        SET log_events=?, session_messages=?, issues=?
        WHERE id=?
        """,
        (log_events, session_messages, issues, run_id),
    )
    conn.commit()
    conn.close()
    return run_id


def render_report(db_path: Path, run_id: int) -> str:
    conn = connect(db_path)
    run = conn.execute(
        "SELECT * FROM audit_runs WHERE id=?", (run_id,)
    ).fetchone()
    issues = conn.execute(
        """
        SELECT severity, issue_type, title, evidence, recommendation, status
        FROM audit_issues WHERE run_id=?
        ORDER BY CASE severity WHEN 'P0' THEN 0 WHEN 'P1' THEN 1 ELSE 2 END, id
        """,
        (run_id,),
    ).fetchall()
    lines = [
        f"# Jarvis Conversation Audit PRD — Run {run_id}",
        "",
        "## Scope",
        f"- Since: {run['since']}",
        f"- Log events ingested: {run['log_events']}",
        f"- Session messages ingested: {run['session_messages']}",
        f"- Issues derived: {run['issues']}",
        "",
        "## Findings",
    ]
    if not issues:
        lines.append("- No issues detected in this window.")
    for row in issues:
        lines.extend([
            f"### {row['severity']} — {row['title']}",
            f"- Type: `{row['issue_type']}`",
            f"- Status: `{row['status']}`",
            f"- Evidence: {row['evidence']}",
            f"- Recommendation: {row['recommendation']}",
            "",
        ])
    lines.extend([
        "## Acceptance Criteria",
        "- Provider/account-limit text must trigger fallback before any safety-filter user message.",
        "- Lark replies must show the actual provider/model used.",
        "- Same-session follow-ups waiting longer than 30s must receive one queue acknowledgement.",
        "- Each audit run must be reproducible from `data/conversation_audit.db`.",
    ])
    conn.close()
    return "\n".join(lines).rstrip() + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hours", type=int, default=24)
    parser.add_argument("--db", default="")
    parser.add_argument("--report", default="")
    args = parser.parse_args(argv)
    paths = default_paths()
    if args.db:
        paths.db_path = Path(args.db)
    run_id = run_audit(paths, hours=args.hours)
    report = render_report(paths.db_path, run_id)
    if args.report:
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report).write_text(report, encoding="utf-8")
    else:
        print(report, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
