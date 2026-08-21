"""Shared SQLite database layer with the ordered base-schema migrations.

Single-file DB (data/jarvis.db) with WAL mode for concurrent readers
(bot.sh, admin, heartbeat tasks). Uses FTS5 for full-text search on
bookmarks and logs. Lived at dashboard/db.py until the :3457 NiceGUI
dashboard was retired (2026-08-21); the schema and tables are unchanged.
"""

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from core.runtime_paths import database_path
from core.timeutil import now_local_str

_DEFAULT_DB_PATH = Path(__file__).parent.parent / "data" / "jarvis.db"
# Kept as a module attribute for test monkeypatching compat (tests patch
# core.db.DB_PATH). Runtime code must go through _db_path().
DB_PATH = _DEFAULT_DB_PATH


def _db_path() -> Path:
    """Resolve the DB path at call time, honoring runtime overrides.

    Import-time constants pin the prod path even when JARVIS_DIR is set
    later (7/21 red-team family: tests polluted the production ledger).
    A monkeypatched DB_PATH still wins so existing tests keep working.
    """
    if DB_PATH != _DEFAULT_DB_PATH:
        return Path(DB_PATH)
    return database_path(default=_DEFAULT_DB_PATH)

MIGRATIONS = [
    # v1: Core tables
    """
    CREATE TABLE IF NOT EXISTS scheduled_tasks (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        category TEXT NOT NULL DEFAULT 'user',
        trigger_type TEXT NOT NULL DEFAULT 'cron',
        trigger_config TEXT NOT NULL DEFAULT '{}',
        conditions TEXT DEFAULT '[]',
        action_type TEXT NOT NULL DEFAULT 'prompt',
        action_config TEXT NOT NULL DEFAULT '{}',
        priority INTEGER DEFAULT 5,
        enabled INTEGER DEFAULT 1,
        created_at TEXT NOT NULL,
        last_run_at TEXT,
        next_run_at TEXT,
        run_count INTEGER DEFAULT 0,
        last_result TEXT
    );

    CREATE TABLE IF NOT EXISTS bookmarks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        url TEXT,
        title TEXT NOT NULL,
        content TEXT,
        summary TEXT,
        tags TEXT DEFAULT '[]',
        status TEXT DEFAULT 'inbox',
        source TEXT DEFAULT 'manual',
        energy_level TEXT,
        read_time_min INTEGER,
        surfaced_count INTEGER DEFAULT 0,
        last_surfaced_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS agent_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT NOT NULL,
        source TEXT NOT NULL,
        level TEXT DEFAULT 'info',
        message TEXT NOT NULL,
        context TEXT DEFAULT '{}'
    );

    CREATE TABLE IF NOT EXISTS engagement_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_type TEXT NOT NULL,
        source TEXT,
        timestamp TEXT NOT NULL,
        engaged INTEGER DEFAULT 0,
        gap_seconds REAL,
        metadata TEXT DEFAULT '{}'
    );

    CREATE TABLE IF NOT EXISTS kv_store (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );

    CREATE INDEX IF NOT EXISTS idx_bookmarks_status ON bookmarks(status);
    CREATE INDEX IF NOT EXISTS idx_bookmarks_created ON bookmarks(created_at);
    CREATE INDEX IF NOT EXISTS idx_agent_log_ts ON agent_log(timestamp);
    CREATE INDEX IF NOT EXISTS idx_agent_log_source ON agent_log(source);
    CREATE INDEX IF NOT EXISTS idx_engagement_ts ON engagement_events(timestamp);
    CREATE INDEX IF NOT EXISTS idx_scheduled_next ON scheduled_tasks(next_run_at);
    """,
    # v2: FTS indices
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS bookmarks_fts USING fts5(
        title, summary, tags, content='bookmarks', content_rowid='id'
    );

    CREATE TRIGGER IF NOT EXISTS bookmarks_ai AFTER INSERT ON bookmarks BEGIN
        INSERT INTO bookmarks_fts(rowid, title, summary, tags)
        VALUES (new.id, new.title, new.summary, new.tags);
    END;

    CREATE TRIGGER IF NOT EXISTS bookmarks_ad AFTER DELETE ON bookmarks BEGIN
        INSERT INTO bookmarks_fts(bookmarks_fts, rowid, title, summary, tags)
        VALUES ('delete', old.id, old.title, old.summary, old.tags);
    END;

    CREATE TRIGGER IF NOT EXISTS bookmarks_au AFTER UPDATE ON bookmarks BEGIN
        INSERT INTO bookmarks_fts(bookmarks_fts, rowid, title, summary, tags)
        VALUES ('delete', old.id, old.title, old.summary, old.tags);
        INSERT INTO bookmarks_fts(rowid, title, summary, tags)
        VALUES (new.id, new.title, new.summary, new.tags);
    END;
    """,
    # v3: Task execution history
    """
    CREATE TABLE IF NOT EXISTS task_executions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id TEXT NOT NULL,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        status TEXT DEFAULT 'running',
        result TEXT,
        duration_ms INTEGER
    );
    CREATE INDEX IF NOT EXISTS idx_task_exec_task ON task_executions(task_id);
    CREATE INDEX IF NOT EXISTS idx_task_exec_started ON task_executions(started_at);
    """,
    # v4: Matter workspace — durable identity above channels and sessions
    """
    CREATE TABLE IF NOT EXISTS matters (
        id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        summary TEXT NOT NULL DEFAULT '',
        next_action TEXT NOT NULL DEFAULT '',
        outcome TEXT NOT NULL DEFAULT '',
        kind TEXT NOT NULL DEFAULT 'project',
        status TEXT NOT NULL DEFAULT 'active',
        priority INTEGER NOT NULL DEFAULT 5,
        source TEXT NOT NULL DEFAULT 'manual',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        closed_at TEXT
    );

    CREATE TABLE IF NOT EXISTS matter_links (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        matter_id TEXT NOT NULL REFERENCES matters(id) ON DELETE CASCADE,
        entity_type TEXT NOT NULL,
        provider TEXT NOT NULL DEFAULT '',
        entity_id TEXT NOT NULL,
        title TEXT NOT NULL DEFAULT '',
        metadata TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(provider, entity_type, entity_id)
    );

    CREATE TABLE IF NOT EXISTS matter_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        matter_id TEXT NOT NULL REFERENCES matters(id) ON DELETE CASCADE,
        event_type TEXT NOT NULL,
        actor TEXT NOT NULL DEFAULT 'system',
        summary TEXT NOT NULL DEFAULT '',
        payload TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL
    );

    CREATE INDEX IF NOT EXISTS idx_matters_status_updated
        ON matters(status, updated_at DESC);
    CREATE INDEX IF NOT EXISTS idx_matter_links_matter
        ON matter_links(matter_id, updated_at DESC);
    CREATE INDEX IF NOT EXISTS idx_matter_events_matter
        ON matter_events(matter_id, id DESC);
    """,
    # v5: Cross-channel Matter bindings and authenticated mobile access
    """
    CREATE TABLE IF NOT EXISTS matter_bindings (
        conv_key TEXT PRIMARY KEY,
        matter_id TEXT NOT NULL REFERENCES matters(id) ON DELETE CASCADE,
        channel TEXT NOT NULL DEFAULT 'lark',
        destination_id TEXT NOT NULL DEFAULT '',
        chat_type TEXT NOT NULL DEFAULT 'p2p',
        thread_root_id TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS matter_channel_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        matter_id TEXT NOT NULL REFERENCES matters(id) ON DELETE CASCADE,
        channel TEXT NOT NULL,
        destination_id TEXT NOT NULL DEFAULT '',
        message_id TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'update',
        state TEXT NOT NULL DEFAULT 'active',
        metadata TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(channel, message_id)
    );

    -- Retention-only schemas from the retired phone web gateway. No active
    -- route reads or writes these tables; historical rows remain auditable.
    CREATE TABLE IF NOT EXISTS mobile_devices (
        id TEXT PRIMARY KEY,
        label TEXT NOT NULL,
        token_hash TEXT NOT NULL,
        created_at TEXT NOT NULL,
        last_seen_at TEXT,
        revoked_at TEXT
    );

    CREATE TABLE IF NOT EXISTS mobile_pair_codes (
        code_hash TEXT PRIMARY KEY,
        label TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        created_at TEXT NOT NULL,
        consumed_at TEXT
    );

    CREATE TABLE IF NOT EXISTS mobile_access_audit (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT NOT NULL,
        device_id TEXT NOT NULL DEFAULT '',
        remote_addr TEXT NOT NULL DEFAULT '',
        method TEXT NOT NULL DEFAULT '',
        path TEXT NOT NULL DEFAULT '',
        status INTEGER NOT NULL DEFAULT 0,
        metadata TEXT NOT NULL DEFAULT '{}'
    );

    CREATE TABLE IF NOT EXISTS matter_push_subscriptions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        device_id TEXT NOT NULL DEFAULT '',
        endpoint TEXT NOT NULL UNIQUE,
        p256dh TEXT NOT NULL,
        auth TEXT NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );

    CREATE INDEX IF NOT EXISTS idx_matter_bindings_matter
        ON matter_bindings(matter_id, updated_at DESC);
    CREATE INDEX IF NOT EXISTS idx_matter_channel_messages_matter
        ON matter_channel_messages(matter_id, updated_at DESC);
    CREATE INDEX IF NOT EXISTS idx_mobile_access_timestamp
        ON mobile_access_audit(timestamp DESC);
    """,
    # v6: Last actually-used provider/model per conversation
    """
    CREATE TABLE IF NOT EXISTS conversation_runtime (
        conv_key TEXT PRIMARY KEY,
        provider TEXT NOT NULL DEFAULT '',
        model TEXT NOT NULL DEFAULT '',
        session_id TEXT NOT NULL DEFAULT '',
        updated_at TEXT NOT NULL
    );
    """,
    # v7: Unified delivery/state plane.  JSONL remains an append-only audit
    # export, but cross-process decisions live in SQLite WAL.
    """
    CREATE TABLE IF NOT EXISTS delivery_envelopes (
        id TEXT PRIMARY KEY,
        source TEXT NOT NULL,
        kind TEXT NOT NULL,
        attention TEXT NOT NULL DEFAULT 'notice',
        requested_channel TEXT NOT NULL DEFAULT 'auto',
        route_channel TEXT NOT NULL DEFAULT '',
        state TEXT NOT NULL DEFAULT 'queued',
        content_hash TEXT NOT NULL,
        dedup_key TEXT NOT NULL DEFAULT '',
        throttle_key TEXT NOT NULL DEFAULT '',
        payload TEXT NOT NULL DEFAULT '{}',
        metadata TEXT NOT NULL DEFAULT '{}',
        provider TEXT NOT NULL DEFAULT '',
        model TEXT NOT NULL DEFAULT '',
        memorial_id TEXT NOT NULL DEFAULT '',
        matter_id TEXT NOT NULL DEFAULT '',
        reply_to TEXT NOT NULL DEFAULT '',
        attempts INTEGER NOT NULL DEFAULT 0,
        last_error TEXT NOT NULL DEFAULT '',
        created_epoch REAL NOT NULL,
        updated_epoch REAL NOT NULL,
        next_attempt_epoch REAL,
        delivered_epoch REAL,
        read_epoch REAL,
        acted_epoch REAL,
        message_id TEXT NOT NULL DEFAULT ''
    );

    CREATE TABLE IF NOT EXISTS delivery_attempts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        delivery_id TEXT NOT NULL REFERENCES delivery_envelopes(id)
            ON DELETE CASCADE,
        attempt INTEGER NOT NULL,
        channel TEXT NOT NULL,
        started_epoch REAL NOT NULL,
        finished_epoch REAL,
        status TEXT NOT NULL DEFAULT 'attempting',
        error TEXT NOT NULL DEFAULT '',
        message_id TEXT NOT NULL DEFAULT ''
    );

    CREATE TABLE IF NOT EXISTS delivery_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        delivery_id TEXT NOT NULL,
        state TEXT NOT NULL,
        detail TEXT NOT NULL DEFAULT '',
        created_epoch REAL NOT NULL
    );

    CREATE TABLE IF NOT EXISTS delivery_dead_letters (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        delivery_id TEXT NOT NULL,
        source TEXT NOT NULL,
        kind TEXT NOT NULL,
        detail TEXT NOT NULL DEFAULT '',
        created_epoch REAL NOT NULL,
        notified_epoch REAL
    );

    CREATE TABLE IF NOT EXISTS intent_breaches (
        id TEXT PRIMARY KEY,
        payload TEXT NOT NULL DEFAULT '{}',
        notify_attempts INTEGER NOT NULL DEFAULT 0,
        created_epoch REAL NOT NULL,
        retired_epoch REAL
    );

    CREATE TABLE IF NOT EXISTS schedule_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event TEXT NOT NULL,
        task TEXT NOT NULL DEFAULT '',
        run_id TEXT NOT NULL DEFAULT '',
        timestamp TEXT NOT NULL,
        created_epoch REAL NOT NULL,
        payload TEXT NOT NULL DEFAULT '{}'
    );

    CREATE TABLE IF NOT EXISTS runtime_versions (
        component TEXT PRIMARY KEY,
        pid INTEGER NOT NULL,
        git_head TEXT NOT NULL DEFAULT '',
        code_mtime REAL NOT NULL DEFAULT 0,
        started_epoch REAL NOT NULL,
        heartbeat_sha256 TEXT NOT NULL DEFAULT '',
        metadata TEXT NOT NULL DEFAULT '{}'
    );

    CREATE INDEX IF NOT EXISTS idx_delivery_dedup
        ON delivery_envelopes(content_hash, created_epoch DESC);
    CREATE INDEX IF NOT EXISTS idx_delivery_queue
        ON delivery_envelopes(state, next_attempt_epoch);
    CREATE INDEX IF NOT EXISTS idx_delivery_source_day
        ON delivery_envelopes(source, created_epoch DESC);
    CREATE INDEX IF NOT EXISTS idx_delivery_deadletter_pending
        ON delivery_dead_letters(notified_epoch, created_epoch);
    CREATE INDEX IF NOT EXISTS idx_schedule_events_created
        ON schedule_events(created_epoch DESC);
    """,
    # v8: Durable cross-device continuation without copying work objects.
    """
    CREATE TABLE IF NOT EXISTS surface_handoffs (
        id TEXT PRIMARY KEY,
        entity_type TEXT NOT NULL,
        entity_id TEXT NOT NULL,
        matter_id TEXT NOT NULL DEFAULT '',
        from_surface TEXT NOT NULL,
        to_surface TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'open',
        title TEXT NOT NULL DEFAULT '',
        note TEXT NOT NULL DEFAULT '',
        created_by TEXT NOT NULL DEFAULT 'local',
        created_epoch REAL NOT NULL,
        claimed_epoch REAL,
        completed_epoch REAL,
        delivery_id TEXT NOT NULL DEFAULT '',
        metadata TEXT NOT NULL DEFAULT '{}'
    );

    CREATE UNIQUE INDEX IF NOT EXISTS idx_surface_handoff_active_unique
        ON surface_handoffs(entity_type, entity_id, to_surface)
        WHERE status IN ('open', 'claimed');
    CREATE INDEX IF NOT EXISTS idx_surface_handoff_target
        ON surface_handoffs(to_surface, status, created_epoch DESC);
    CREATE INDEX IF NOT EXISTS idx_surface_handoff_entity
        ON surface_handoffs(entity_type, entity_id, created_epoch DESC);
    """,
    # v9: Atomic send-day cap reservations for concurrent delivery workers.
    """
    CREATE TABLE IF NOT EXISTS delivery_cap_reservations (
        delivery_id TEXT PRIMARY KEY,
        day_start_epoch REAL NOT NULL,
        throttle_key TEXT NOT NULL DEFAULT '',
        source TEXT NOT NULL DEFAULT '',
        reserved_epoch REAL NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_delivery_cap_reservation_day
        ON delivery_cap_reservations(day_start_epoch, source, throttle_key);
    """,
    # v10: Per-conversation provider preference and persistent Codex thread.
    """
    CREATE TABLE IF NOT EXISTS conversation_provider_preferences (
        conv_key TEXT PRIMARY KEY,
        preference TEXT NOT NULL DEFAULT 'auto'
            CHECK(preference IN ('auto', 'codex')),
        updated_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS codex_conversation_sessions (
        conv_key TEXT PRIMARY KEY,
        thread_id TEXT NOT NULL,
        model TEXT NOT NULL DEFAULT '',
        work_dir TEXT NOT NULL DEFAULT '',
        updated_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS conversation_turns (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        conv_key TEXT NOT NULL,
        role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
        text TEXT NOT NULL,
        message_id TEXT NOT NULL DEFAULT '',
        provider TEXT NOT NULL DEFAULT '',
        model TEXT NOT NULL DEFAULT '',
        session_id TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_conversation_turns_recent
        ON conversation_turns(conv_key, id DESC);
    CREATE UNIQUE INDEX IF NOT EXISTS idx_conversation_turns_message_role
        ON conversation_turns(conv_key, role, message_id)
        WHERE message_id <> '';
    """,
    # v11: reset generations for logical conversation contexts.  A reset bumps
    # the generation instead of deleting provider transcripts, so delayed
    # writers from the old generation can be rejected deterministically.
    """
    CREATE TABLE IF NOT EXISTS logical_context_states (
        context_key TEXT PRIMARY KEY,
        generation INTEGER NOT NULL DEFAULT 0,
        reset_at TEXT NOT NULL DEFAULT ''
    );
    """,
]

_connection: sqlite3.Connection | None = None


def get_db() -> sqlite3.Connection:
    """Get or create the singleton DB connection."""
    global _connection
    if _connection is None:
        path = _db_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        _connection = sqlite3.connect(str(path), check_same_thread=False)
        _connection.row_factory = sqlite3.Row
        _connection.execute("PRAGMA journal_mode=WAL")
        _connection.execute("PRAGMA synchronous=NORMAL")
        _connection.execute("PRAGMA foreign_keys=ON")
        # A transient writer-lock should wait, not blank a @ui.refreshable page:
        # the container is cleared BEFORE the data fn runs and only TypeError is
        # caught, so a bare OperationalError('database is locked') escapes after
        # the clear → blank tick. Block up to 3s for the writer instead.
        _connection.execute("PRAGMA busy_timeout=5000")
        _run_migrations(_connection)
    return _connection


@contextmanager
def transaction():
    """Context manager for atomic writes."""
    db = get_db()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise


def _run_migrations(db: sqlite3.Connection):
    """Run pending migrations."""
    db.execute("""
        CREATE TABLE IF NOT EXISTS _migrations (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL
        )
    """)
    applied = {row[0] for row in db.execute("SELECT version FROM _migrations")}

    for i, sql in enumerate(MIGRATIONS):
        if i in applied:
            continue
        db.executescript(sql)
        db.execute(
            "INSERT INTO _migrations (version, applied_at) VALUES (?, ?)",
            (i, now_local_str("%Y-%m-%dT%H:%M:%S")),
        )
    db.commit()
    # Logical-session scope is an additive domain migration rather than a raw
    # ALTER in MIGRATIONS: the named markers make a crash/restart re-entrant
    # and verify the physical column shape before accepting it.
    from core.sqlite_migrations import ensure_additive_columns
    ensure_additive_columns(
        db,
        namespace="logical_session",
        table="conversation_turns",
        columns=(
            ("context_key", "TEXT NOT NULL DEFAULT ''"),
            ("matter_id", "TEXT NOT NULL DEFAULT ''"),
        ),
    )
    try:
        db.execute("BEGIN IMMEDIATE")
        # These data repairs intentionally run on every startup.  They are
        # idempotent, which makes the column-add -> backfill boundary safe if a
        # process crashes after the additive schema transaction commits.
        db.execute(
            """UPDATE conversation_turns
                  SET context_key = 'conversation:' || conv_key
                WHERE context_key = ''"""
        )
        db.execute(
            """DELETE FROM codex_conversation_sessions AS legacy
                WHERE legacy.conv_key NOT LIKE 'conversation:%'
                  AND legacy.conv_key NOT LIKE 'matter:%'
                  AND EXISTS (
                      SELECT 1 FROM codex_conversation_sessions AS scoped
                       WHERE scoped.conv_key = 'conversation:' || legacy.conv_key
                  )"""
        )
        db.execute(
            """UPDATE codex_conversation_sessions
                  SET conv_key = 'conversation:' || conv_key
                WHERE conv_key NOT LIKE 'conversation:%'
                  AND conv_key NOT LIKE 'matter:%'"""
        )
        db.execute(
            """CREATE INDEX IF NOT EXISTS idx_conversation_turns_context_recent
               ON conversation_turns(context_key, id DESC)"""
        )
        db.commit()
    except Exception:
        db.rollback()
        raise


# ── Bookmark operations ──────────────────────────────────────────────

def bookmark_add(title: str, url: str = "", source: str = "manual",
                 summary: str = "", tags: list[str] | None = None,
                 content: str = "") -> int:
    """Add a bookmark. Returns the new ID."""
    now = now_local_str("%Y-%m-%dT%H:%M:%S")
    with transaction() as db:
        # Deduplicate by URL
        if url:
            existing = db.execute(
                "SELECT id FROM bookmarks WHERE url = ?", (url,)
            ).fetchone()
            if existing:
                return existing[0]
        cur = db.execute(
            """INSERT INTO bookmarks (title, url, source, summary, tags, content, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (title, url, source, summary, json.dumps(tags or []), content, now, now),
        )
        return cur.lastrowid


def bookmark_list(status: str | None = None, limit: int = 50,
                  offset: int = 0) -> list[dict]:
    """List bookmarks, optionally filtered by status."""
    db = get_db()
    if status:
        rows = db.execute(
            "SELECT * FROM bookmarks WHERE status = ? ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (status, limit, offset),
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT * FROM bookmarks ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
    return [dict(r) for r in rows]


def bookmark_search(query: str, limit: int = 20) -> list[dict]:
    """Full-text search bookmarks.

    FTS5 MATCH treats +, ", ( etc. as query operators and raises
    OperationalError on user input like 'c++'; fall back to a plain
    LIKE substring search instead of 500ing.
    """
    db = get_db()
    try:
        rows = db.execute(
            """SELECT b.* FROM bookmarks b
               JOIN bookmarks_fts f ON b.id = f.rowid
               WHERE bookmarks_fts MATCH ? ORDER BY rank LIMIT ?""",
            (query, limit),
        ).fetchall()
    except sqlite3.OperationalError:
        like = "%" + (query.replace("\\", r"\\")
                      .replace("%", r"\%").replace("_", r"\_")) + "%"
        rows = db.execute(
            r"""SELECT * FROM bookmarks
                WHERE title LIKE ? ESCAPE '\'
                   OR summary LIKE ? ESCAPE '\'
                   OR tags LIKE ? ESCAPE '\'
                ORDER BY created_at DESC LIMIT ?""",
            (like, like, like, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def bookmark_update(bookmark_id: int, **kwargs) -> None:
    """Update bookmark fields."""
    allowed = {"status", "summary", "tags", "title", "url", "content",
               "energy_level", "read_time_min", "surfaced_count", "last_surfaced_at"}
    updates = {k: v for k, v in kwargs.items() if k in allowed}
    if not updates:
        return
    updates["updated_at"] = now_local_str("%Y-%m-%dT%H:%M:%S")
    if "tags" in updates and isinstance(updates["tags"], list):
        updates["tags"] = json.dumps(updates["tags"])
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [bookmark_id]
    with transaction() as db:
        db.execute(f"UPDATE bookmarks SET {set_clause} WHERE id = ?", values)


def bookmark_delete(bookmark_id: int) -> None:
    """Delete a bookmark."""
    with transaction() as db:
        db.execute("DELETE FROM bookmarks WHERE id = ?", (bookmark_id,))


# ── Agent log operations ─────────────────────────────────────────────

def log_event(source: str, message: str, level: str = "info",
              context: dict | None = None) -> None:
    """Write an agent log entry."""
    with transaction() as db:
        db.execute(
            "INSERT INTO agent_log (timestamp, source, level, message, context) VALUES (?, ?, ?, ?, ?)",
            (now_local_str("%Y-%m-%dT%H:%M:%S"), source, level, message,
             json.dumps(context or {})),
        )


def log_list(source: str | None = None, limit: int = 100,
             since: str | None = None) -> list[dict]:
    """Query agent logs."""
    db = get_db()
    conditions = []
    params: list = []
    if source:
        conditions.append("source = ?")
        params.append(source)
    if since:
        conditions.append("timestamp >= ?")
        params.append(since)
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    rows = db.execute(
        f"SELECT * FROM agent_log {where} ORDER BY timestamp DESC LIMIT ?",
        params + [limit],
    ).fetchall()
    return [dict(r) for r in rows]


# ── Scheduled task operations ────────────────────────────────────────

def task_register(task_id: str, name: str, trigger_type: str,
                  trigger_config: dict, action_type: str = "prompt",
                  action_config: dict | None = None,
                  conditions: list | None = None,
                  category: str = "user", priority: int = 5) -> None:
    """Register or update a dynamic task.

    Raises ValueError on a malformed trigger — a poison row would otherwise
    be evaluated (and skipped, loudly) on every due-check forever.
    """
    from core.cron import validate_trigger
    err = validate_trigger(trigger_type, trigger_config)
    if err:
        raise ValueError(err)
    now = now_local_str("%Y-%m-%dT%H:%M:%S")
    with transaction() as db:
        db.execute(
            """INSERT OR REPLACE INTO scheduled_tasks
               (id, name, category, trigger_type, trigger_config, conditions,
                action_type, action_config, priority, enabled, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)""",
            (task_id, name, category, trigger_type,
             json.dumps(trigger_config), json.dumps(conditions or []),
             action_type, json.dumps(action_config or {}), priority, now),
        )


def task_list(category: str | None = None, enabled_only: bool = True) -> list[dict]:
    """List scheduled tasks."""
    db = get_db()
    conditions = []
    params: list = []
    if category:
        conditions.append("category = ?")
        params.append(category)
    if enabled_only:
        conditions.append("enabled = 1")
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    rows = db.execute(
        f"SELECT * FROM scheduled_tasks {where} ORDER BY priority, name",
        params,
    ).fetchall()
    return [dict(r) for r in rows]


def task_delete(task_id: str) -> None:
    """Delete a scheduled task."""
    with transaction() as db:
        db.execute("DELETE FROM scheduled_tasks WHERE id = ?", (task_id,))


def task_update(task_id: str, **kwargs) -> None:
    """Update task fields."""
    allowed = {"enabled", "trigger_config", "conditions", "action_config",
               "priority", "last_run_at", "next_run_at", "run_count", "last_result", "name"}
    updates = {k: v for k, v in kwargs.items() if k in allowed}
    if not updates:
        return
    for k in ("trigger_config", "conditions", "action_config"):
        if k in updates and not isinstance(updates[k], str):
            updates[k] = json.dumps(updates[k])
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [task_id]
    with transaction() as db:
        db.execute(f"UPDATE scheduled_tasks SET {set_clause} WHERE id = ?", values)


# ── KV store ─────────────────────────────────────────────────────────

def kv_get(key: str, default: str = "") -> str:
    """Get a value from the KV store."""
    db = get_db()
    row = db.execute("SELECT value FROM kv_store WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def kv_set(key: str, value: str) -> None:
    """Set a value in the KV store."""
    with transaction() as db:
        db.execute(
            "INSERT OR REPLACE INTO kv_store (key, value, updated_at) VALUES (?, ?, ?)",
            (key, value, now_local_str("%Y-%m-%dT%H:%M:%S")),
        )


# ── Engagement tracking ──────────────────────────────────────────────

def engagement_record(event_type: str, source: str = "",
                      engaged: bool = False, gap_seconds: float = 0,
                      metadata: dict | None = None) -> None:
    """Record an engagement event."""
    with transaction() as db:
        db.execute(
            """INSERT INTO engagement_events
               (event_type, source, timestamp, engaged, gap_seconds, metadata)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (event_type, source, now_local_str("%Y-%m-%dT%H:%M:%S"),
             int(engaged), gap_seconds, json.dumps(metadata or {})),
        )


# engagement_stats cache: callers poll frequently, the jsonl rarely changes.
# Keyed on (path, days) → (mtime_ns, size, result).
_engagement_stats_cache: dict[tuple[str, int], tuple[int, int, dict]] = {}


def engagement_stats(days: int = 7) -> dict:
    """Get engagement statistics for the last N days.

    Reads engagement_log.jsonl — the source of truth written by the bot/
    heartbeat. The engagement_events TABLE is live but one-sided: core/
    delivery.py inserts a 'sent' attribution row on every delivered envelope
    (delivery_id/channel/provider metadata), while the response half of the
    story is recorded only in the jsonl. Stats therefore read the jsonl,
    which carries both halves; the table and its API stay for delivery
    attribution.

    Per-source engaged counts are capped at the sent count (historical rows
    double-credited replies, showing >100% rates on home) — same cap as
    pages/engagement.py.
    """
    import time as _time
    from collections import defaultdict

    jarvis_dir = Path(os.environ.get("JARVIS_DIR",
                                     Path(__file__).resolve().parent.parent))
    log_path = Path(jarvis_dir) / "engagement_log.jsonl"
    cutoff = _time.time() - days * 86400

    cache_key = (str(log_path), days)
    try:
        st = log_path.stat()
        stamp = (st.st_mtime_ns, st.st_size)
    except OSError:
        stamp = None
    if stamp is not None:
        cached = _engagement_stats_cache.get(cache_key)
        if cached and cached[:2] == stamp:
            return cached[2]

    total = engaged = 0
    by_source: dict[str, dict] = defaultdict(lambda: {"total": 0, "engaged_count": 0})
    try:
        lines = log_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    for line in lines:
        try:
            e = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        epoch = e.get("epoch", 0)
        if not epoch:
            # "response" entries carry only a "ts" string, no epoch
            try:
                epoch = _time.mktime(_time.strptime(e.get("ts", ""), "%Y-%m-%d %H:%M"))
            except (ValueError, OverflowError):
                continue
        if epoch < cutoff:
            continue
        etype = e.get("type")
        source = e.get("source", "")
        if etype == "sent":
            total += 1
            by_source[source]["total"] += 1
        elif etype == "response" and e.get("reaction") in ("engaged", "late_reply"):
            by_source[source]["engaged_count"] += 1
    for v in by_source.values():
        v["engaged_count"] = min(v["engaged_count"], v["total"])
    engaged = sum(v["engaged_count"] for v in by_source.values())
    result = {
        "total": total,
        "engaged": engaged,
        "rate": round(engaged / total * 100, 1) if total else 0,
        "by_source": [{"source": s, **v} for s, v in sorted(by_source.items())],
    }
    if stamp is not None:
        _engagement_stats_cache[cache_key] = (*stamp, result)
    return result
