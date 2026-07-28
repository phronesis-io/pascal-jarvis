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

from core.claude_projects import jarvis_project_dirs
from core.search import load_chat_messages
from core.timeutil import now_local


def _jarvis_session_dirs() -> list[Path]:
    """Session dirs for all three jarvis project roots."""
    return jarvis_project_dirs()


LOG_RE = re.compile(r"^\[(?P<ts>[^]]+)\] \[(?P<level>[^]]+)\] (?P<msg>.*)$")
SESSION_RE = re.compile(r"\[(?P<sid>[0-9a-f-]{36})\]")
EVENT_RE = re.compile(
    r"Event: msg_type=(?P<msg_type>\S+) .*?mid=(?P<message_id>\S+) "
    r"chat_type=(?P<chat_type>\S+) content_head=(?P<head>.*)$"
)

COMPLAINT_PATTERNS = {
    "missed_signal": {
        "severity": "P1",
        "title": "User complained that important signals were not surfaced",
        "patterns": ("没有收到", "为什么没有推送", "也没有收到", "太差"),
        "recommendation": "Route this to the PGC/source-health audit: compare feed ingestion, ranking, and delivery gates for the cited item.",
    },
    "hallucination_or_confusion": {
        "severity": "P1",
        "title": "User challenged an answer as confusing or fabricated",
        "patterns": ("乱说", "不知道你在说什么", "傻逼玩意"),
        "recommendation": "When challenged, prefer evidence-first correction and cite the exact source/session artifact used.",
    },
    "needs_deeper_research": {
        "severity": "P2",
        "title": "User asked for a deeper, broader investigation pass",
        "patterns": ("全面调研", "所有东西都查明白", "多查查", "全查了", "查呀"),
        "recommendation": "Auto-promote broad research requests earlier and send progress checkpoints with sources inspected.",
    },
    "model_transparency_requested": {
        "severity": "P1",
        "title": "User explicitly asked which model/provider is answering",
        "patterns": ("什么模型", "哪个模型", "what model", "model am i using", "which model"),
        "recommendation": "Keep provider/model footer telemetry on every successful Lark reply and include fallback/provider switches in audit reports.",
    },
    "status_uncertainty": {
        "severity": "P1",
        "title": "User repeatedly asked whether work is done or what is happening",
        "patterns": ("all done", "done?", "done？", "干完了吗", "咋样了", "这个啥情况", "what's next step", "next steps", "how about now", "what now"),
        "recommendation": "For long-running work, send compact status checkpoints with current phase, last verified evidence, and next irreversible step.",
    },
    "awkward_progress_copy": {
        "severity": "P2",
        "title": "User criticized progress/acknowledgement copy as unnatural or noisy",
        "patterns": ("不是中文", "很蠢", "you should not show me this", "用人话", "简洁明了"),
        "recommendation": "Keep acknowledgement copy short and natural; suppress low-value heartbeat/status narration unless it changes user action.",
    },
    "pgc_latency_quality": {
        "severity": "P1",
        "title": "User reported PGC source latency or recommendation-chain quality problems",
        "patterns": ("时效性差", "推荐系统有问题", "让他这么晚收到", "PGC信源", "信源过时"),
        "recommendation": "Record this as an external PGC handoff signal with enough context for the owning workflow; do not silently fold it into Jarvis-only self-evolution work.",
    },
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
        session_dirs=_jarvis_session_dirs(),
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
            issues INTEGER NOT NULL DEFAULT 0,
            completed_at TEXT
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
        CREATE TABLE IF NOT EXISTS schema_migrations (
            name TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL
        );
        """
    )
    # Closure workflow (REQ-105, approved 2026-07-14): resolved findings carry
    # a resolution note. Migration guard for pre-existing DBs.
    try:
        conn.execute(
            "ALTER TABLE audit_issues ADD COLUMN resolution TEXT NOT NULL DEFAULT ''")
    except sqlite3.OperationalError:
        pass  # column already exists
    try:
        conn.execute("ALTER TABLE audit_runs ADD COLUMN completed_at TEXT")
    except sqlite3.OperationalError:
        pass  # column already exists
    migration = "audit_runs_completed_at_backfill_v1"
    migrated = conn.execute(
        "SELECT 1 FROM schema_migrations WHERE name=?",
        (migration,),
    ).fetchone()
    if migrated is None:
        # The immediately preceding build could add the column without
        # backfilling it. Its persisted rows were atomically completed, so
        # mark them once; later interrupted runs keep their NULL marker.
        conn.execute(
            "UPDATE audit_runs SET completed_at=started_at "
            "WHERE completed_at IS NULL"
        )
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations(name,applied_at) "
            "VALUES (?,?)",
            (migration, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
    return conn


def _parse_ts(raw: str) -> datetime | None:
    try:
        parsed = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    local_tz = now_local().tzinfo
    if local_tz is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.replace(tzinfo=local_tz).astimezone(timezone.utc)


def _parse_message_ts(raw: str) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.strptime(raw[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
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


_MAX_LOG_BYTES = 5 * 1024 * 1024  # 5 MB cap per log file


def _tail_read(path: Path, max_bytes: int = _MAX_LOG_BYTES) -> str:
    """Read up to the last *max_bytes* of a text file, dropping any partial first line."""
    try:
        size = path.stat().st_size
    except OSError:
        return ""
    if size <= max_bytes:
        return path.read_text(encoding="utf-8", errors="ignore")
    with open(path, "rb") as f:
        f.seek(size - max_bytes)
        raw = f.read().decode("utf-8", errors="ignore")
    first_nl = raw.find("\n")
    return raw[first_nl + 1:] if first_nl >= 0 else raw


def ingest_logs(conn: sqlite3.Connection, run_id: int, paths: Iterable[Path],
                since: datetime) -> int:
    count = 0
    seen: set[tuple[str, str, str, str, str]] = set()
    for path in paths:
        if not path.exists():
            continue
        for line in _tail_read(path).splitlines():
            m = LOG_RE.match(line)
            if not m:
                # Windowed audits need timestamped evidence. Long-lived logs can
                # retain old unstructured shell stderr forever while their mtime
                # stays fresh because new structured lines are appended.
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
                msg_ts = _parse_message_ts(msg.get("timestamp", ""))
                if msg_ts and msg_ts < since:
                    continue
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
    evidence = evidence[:2000]
    resolution = ""
    if status == "open":
        # Closure carry-forward (REQ-105): a finding someone already
        # adjudicated must not reopen just because the daily run re-derives
        # the same evidence inside its 24h window. Exact (type, evidence)
        # match — same message re-derived produces the same evidence string.
        prior = conn.execute(
            """
            SELECT status, resolution FROM audit_issues
            WHERE issue_type=? AND evidence=? AND status='resolved'
            ORDER BY id DESC LIMIT 1
            """,
            (issue_type, evidence),
        ).fetchone()
        if prior:
            status, resolution = "resolved", prior["resolution"]
    conn.execute(
        """
        INSERT INTO audit_issues
        (run_id, severity, issue_type, title, evidence, recommendation, status,
         resolution)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (run_id, severity, issue_type, title, evidence,
         recommendation, status, resolution),
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


def _sessions_that_replied(conn: sqlite3.Connection, run_id: int) -> set[str]:
    """Sessions that actually delivered at least one reply in this run.

    `session_messages` is a transcript, not a delivery record. A Claude Code
    CLI session, a heartbeat task session, or a background job all produce
    assistant turns that were never sent to anyone. Corroborating against
    `reply_sent` is the audit's own local evidence of what left the system.
    """
    return {
        str(row["session_id"])
        for row in conn.execute(
            "SELECT DISTINCT session_id FROM conversation_events "
            "WHERE run_id=? AND event_type='reply_sent' "
            "AND session_id IS NOT NULL AND session_id!=''",
            (run_id,),
        )
    }


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

    _provider_sql_filter = " OR ".join(
        f"LOWER(text) LIKE '%{needle}%'" for needle in PROVIDER_ERROR_NEEDLES
    )
    candidate_provider = conn.execute(
        f"""
        SELECT ts, session_id, role, text FROM session_messages
        WHERE run_id=? AND role='assistant' AND ({_provider_sql_filter})
        ORDER BY ts DESC
        """,
        (run_id,),
    ).fetchall()
    # Same delivery-evidence rule as the empty-reply detector below. The
    # 2026-07-27 fix was applied to only one of the two symmetrical detectors,
    # so this one kept raising P0s ("surfaced to the user") about transcripts
    # from local Claude Code CLI sessions with zero reply_sent events — 5 open
    # findings across runs 50-56, none of which ever reached anyone.
    # Nothing goes dark: every detected provider error is still recorded by the
    # `provider_fallback_exercised` P1 above, and anything that got past the
    # safety boundary is a `provider_error_as_answer` P0 from suppressed_content.
    replying_sessions = _sessions_that_replied(conn, run_id)
    direct_provider_rows = [
        row for row in candidate_provider
        if _is_direct_provider_surface(row["text"])
        and str(row["session_id"]) in replying_sessions
    ]

    _empty_sql_filter = " OR ".join(
        f"LOWER(text) LIKE '%{needle}%'" for needle in EMPTY_REPLY_NEEDLES
    )
    candidate_empty = conn.execute(
        f"""
        SELECT ts, session_id, role, text FROM session_messages
        WHERE run_id=? AND role='assistant'
          AND LENGTH(text) <= 120
          AND ({_empty_sql_filter})
        ORDER BY ts DESC
        """,
        (run_id,),
    ).fetchall()
    # "Surfaced to the user" is a claim about delivery, so it needs delivery
    # evidence. Until 2026-07-27 it was inferred from transcript text alone,
    # and the two P0s raised that day were "No response requested." turns in
    # local Claude Code CLI sessions that had zero reply_sent events and zero
    # matching rows in the delivery ledger — nothing was ever sent to anyone.
    # Residual, stated rather than hidden: a session that did reply and also
    # emitted an internal no-op turn can still be flagged. Closing that needs
    # per-message receipts, which the audit does not yet ingest.
    direct_empty_rows = [
        row for row in candidate_empty
        if _is_direct_empty_surface(row["text"])
        and str(row["session_id"]) in replying_sessions
    ]
    provider_issue_type = (
        "progress_provider_error_leak"
        if any(row["text"].lstrip().startswith(("🔧", "🛠", "⚙")) for row in direct_provider_rows)
        else "provider_error_in_assistant_transcript"
    )
    _add_user_visible_provider_issue(
        conn, run_id, direct_provider_rows, provider_issue_type,
        "Provider/account-limit text was recorded in assistant transcript",
        "Treat transcript-level provider errors as failed attempts, not answers: keep advancing fallback, and only expose provider/model status after a successful reply.",
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
        lowered = text.lower()
        for issue_type, cfg in COMPLAINT_PATTERNS.items():
            patterns = cfg["patterns"]
            if any(p.lower() in lowered for p in patterns):
                _add_issue(
                    conn, run_id, cfg["severity"], issue_type,
                    cfg["title"],
                    f"{row['ts']} message_id={row['message_id']} content={text}",
                    cfg["recommendation"],
                )

    after_count = conn.execute(
        "SELECT COUNT(*) FROM audit_issues WHERE run_id=?", (run_id,)
    ).fetchone()[0]
    return after_count - start_count


# ── Card leak sentinel (REQ-104) ─────────────────────────────────────────
# Template/JSON/prompt-framing residue reaching Pascal's cards is a recurring
# family (HEARTBEAT_OK 7/15 "这卡片非常蠢", raw {"response":...} ×4, a card
# whose whole body was "send", "=== TASK: checkin ===", "[2026-07-19 09:16]
# checkin" headers, raw "OPTIONS:" lines when the button parser missed).
# Every incident was patched per-task; this scans what was actually put on
# cards so the NEXT escape shows up as an open P0 by itself.
_CARD_LEAK_SIGNATURES: list[tuple[str, re.Pattern]] = [
    ("sentinel_token", re.compile(r"HEARTBEAT_OK")),
    ("raw_json_envelope", re.compile(r'^\s*\{"(?:response|tasks)"', re.M)),
    # ASCII-only tail — same rationale as safety._FRAMING_LINE_RE: a \S tail
    # would flag every card quoting a "[ts] 中文" timeline line as a leak.
    ("task_framing", re.compile(
        r"^\s*(?:===\s*TASK[^=\n]*===|\[[A-Z][A-Z_ -]{1,24}\]"
        r"|\[20\d\d-\d\d-\d\d[ T]\d\d:\d\d(?::\d\d)?\]\s*[A-Za-z0-9_./-]{0,40})\s*$",
        re.M)),
    ("bare_send", re.compile(r"\A\s*send\s*\Z")),
    ("raw_options_line", re.compile(r"^\s*(?:OPTIONS|选项)\s*[:：]", re.M)),
]


def derive_card_leak_issues(conn: sqlite3.Connection, run_id: int,
                            jarvis_dir: Path, since: datetime) -> int:
    ledger = jarvis_dir / "memorials.jsonl"
    found = 0
    try:
        lines = ledger.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return 0
    for line in lines:
        try:
            rec = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if rec.get("ev") != "create":
            continue
        raw_ts = str(rec.get("ts", ""))
        ts = _parse_ts(raw_ts if raw_ts.count(":") >= 2 else raw_ts + ":00")
        if ts is None or ts < since:
            continue
        body = str(rec.get("body", ""))
        for kind, pat in _CARD_LEAK_SIGNATURES:
            m = pat.search(body)
            if not m:
                continue
            _add_issue(
                conn, run_id, "P0", "card_template_leak",
                f"Internal residue ({kind}) reached a user-facing card",
                f"{rec.get('ts')} memorial={rec.get('id')} "
                f"source={rec.get('source')} match={m.group(0)[:80]!r} "
                f"body_head={_excerpt(str(rec.get('body', '')), 160)}",
                "Fix the emitting task's output handling (strip_task_framing / "
                "envelope unwrap / OPTIONS extraction) — never ship the card "
                "path with residue.",
            )
            found += 1
            break  # one issue per card is enough
    return found


# ── Closure workflow (REQ-105, approved 2026-07-14) ──────────────────────

def open_findings(
    db_path: Path, days: int | None = 7
) -> list[dict]:
    """Return open issues, deduped by ``(issue_type, evidence)``.

    ``days=None`` deliberately removes the reporting-window cutoff. The L3
    observer uses that mode because unresolved responsibility must not expire
    merely because no newer audit reproduced it.
    """
    conn = connect(db_path)
    query = """
        SELECT i.id, i.run_id, r.started_at, i.severity, i.issue_type,
               i.title, i.evidence, i.recommendation
          FROM audit_issues i JOIN audit_runs r ON r.id = i.run_id
         WHERE i.status='open'
    """
    params: tuple[str, ...] = ()
    if days is not None:
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=days)
        ).isoformat()
        query += " AND r.started_at >= ?"
        params = (cutoff,)
    rows = conn.execute(query + " ORDER BY i.id DESC", params).fetchall()
    conn.close()
    seen, out = set(), []
    for row in rows:  # newest first
        key = (row["issue_type"], row["evidence"])
        if key in seen:
            continue
        seen.add(key)
        out.append(dict(row))
    out.sort(key=lambda r: ({"P0": 0, "P1": 1}.get(r["severity"], 2), -r["id"]))
    return out


def resolve_findings(db_path: Path, note: str, issue_id: int | None = None,
                     issue_type: str = "", days: int = 30) -> int:
    """Mark open findings resolved. Targets one --id, or every open row of
    --type from the last `days`. The note says WHY (fixed-in-commit, false
    positive, working-as-designed) — a bare resolve is refused."""
    if not note.strip():
        raise ValueError("resolution note is required")
    conn = connect(db_path)
    if issue_id is not None:
        # Resolve every open row carrying the same (type, evidence), not just
        # this id: the daily runner's 25h window derives the same evidence in
        # two consecutive runs, and open_findings shows only the newest id —
        # resolving that one alone made the older twin resurface immediately
        # (red-team 7/20 finding #2).
        row = conn.execute(
            "SELECT issue_type, evidence FROM audit_issues WHERE id=?",
            (issue_id,)).fetchone()
        if row is None:
            conn.close()
            return 0
        cur = conn.execute(
            "UPDATE audit_issues SET status='resolved', resolution=? "
            "WHERE status='open' AND issue_type=? AND evidence=?",
            (note, row["issue_type"], row["evidence"]))
    elif issue_type:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        cur = conn.execute(
            """
            UPDATE audit_issues SET status='resolved', resolution=?
            WHERE status='open' AND issue_type=? AND run_id IN
              (SELECT id FROM audit_runs WHERE started_at >= ?)
            """,
            (note, issue_type, cutoff))
    else:
        conn.close()
        raise ValueError("need --id or --type")
    n = cur.rowcount
    conn.commit()
    conn.close()
    return n


def render_open_findings(db_path: Path, days: int = 7) -> str:
    rows = open_findings(db_path, days=days)
    if not rows:
        return f"# Open audit findings (last {days}d)\n\n- none 🎉\n"
    lines = [f"# Open audit findings (last {days}d) — {len(rows)}", ""]
    for r in rows:
        lines += [
            f"## [{r['id']}] {r['severity']} `{r['issue_type']}` — {r['title']}",
            f"- Run {r['run_id']} @ {r['started_at']}",
            f"- Evidence: {r['evidence']}",
            f"- Recommendation: {r['recommendation']}",
            "",
        ]
    lines += [
        "Resolve with: python3 -m core.conversation_audit resolve "
        "--id N --note '...' (or --type TYPE)",
    ]
    return "\n".join(lines) + "\n"


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
    issues += derive_card_leak_issues(conn, run_id, paths.jarvis_dir, since_dt)
    conn.execute(
        """
        UPDATE audit_runs
        SET log_events=?, session_messages=?, issues=?, completed_at=?
        WHERE id=?
        """,
        (
            log_events,
            session_messages,
            issues,
            datetime.now(timezone.utc).isoformat(),
            run_id,
        ),
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
        "- Repeated status/model-transparency questions must appear as structured audit issues, not only as anecdotal feedback.",
        "- Each audit run must be reproducible from `data/conversation_audit.db`.",
    ])
    conn.close()
    return "\n".join(lines).rstrip() + "\n"


def main(argv: list[str] | None = None) -> int:
    import sys as _sys
    argv = list(_sys.argv[1:] if argv is None else argv)

    # Subcommands (REQ-105). Bare/flag-only invocation stays the daily run —
    # scripts/run_conversation_audit.sh depends on that.
    if argv and argv[0] == "open-findings":
        parser = argparse.ArgumentParser(prog="conversation_audit open-findings")
        parser.add_argument("--days", type=int, default=7)
        parser.add_argument("--db", default="")
        args = parser.parse_args(argv[1:])
        db = Path(args.db) if args.db else default_paths().db_path
        print(render_open_findings(db, days=args.days), end="")
        return 0
    if argv and argv[0] == "resolve":
        parser = argparse.ArgumentParser(prog="conversation_audit resolve")
        parser.add_argument("--id", type=int, default=None)
        parser.add_argument("--type", dest="issue_type", default="")
        parser.add_argument("--days", type=int, default=30)
        parser.add_argument("--note", required=True)
        parser.add_argument("--db", default="")
        args = parser.parse_args(argv[1:])
        db = Path(args.db) if args.db else default_paths().db_path
        try:
            n = resolve_findings(db, args.note, issue_id=args.id,
                                 issue_type=args.issue_type, days=args.days)
        except ValueError as e:
            print(f"error: {e}", file=_sys.stderr)
            return 2
        print(f"resolved {n} finding(s)")
        return 0

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
