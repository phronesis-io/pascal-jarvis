"""Private, rebuildable search index for interactive work-session history."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Iterable

from core import cross_session_discovery as discovery
from core import cross_session_parsing as parsing


DEFAULT_DB_FILE = "data/cross_session_memory.db"
DEFAULT_BATCH_SIZE = 16
MAX_HISTORY_BYTES = 32 * 1024 * 1024
MAX_HISTORY_TURNS = 240
MAX_QUERY_CANDIDATES = 240
MAX_RESULTS = 8
MAX_RESULT_CHARS = 5000
INDEX_POLICY_VERSION = 1
INDEX_SCHEMA_VERSION = 1
_GENERIC_QUERY_TERMS = {
    "一个", "一下", "之前", "什么", "可以", "好的", "怎么", "我们",
    "我想", "现在", "继续", "这个", "那个", "问题",
}


def _db_path(
    db_path: str | Path | None = None,
    root: str | Path | None = None,
) -> Path:
    if db_path:
        return Path(db_path)
    configured = os.environ.get("CROSS_SESSION_MEMORY_DB", "").strip()
    if configured:
        return Path(configured)
    runtime_root = str(root or os.environ.get("JARVIS_DIR") or "").strip()
    if runtime_root:
        return Path(runtime_root) / DEFAULT_DB_FILE
    memory_dir = os.environ.get("MEMORY_DIR", "").strip()
    if memory_dir:
        return Path(memory_dir).parent / "cross_session_memory.db"
    return Path.cwd() / DEFAULT_DB_FILE


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=5)
    try:
        path.chmod(0o600)
    except OSError:
        pass
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    db.execute("PRAGMA busy_timeout=5000")
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS session_sources (
            source_key TEXT PRIMARY KEY,
            provider TEXT NOT NULL,
            session_id TEXT NOT NULL DEFAULT '',
            workspace TEXT NOT NULL DEFAULT '',
            mtime_ns INTEGER NOT NULL,
            size INTEGER NOT NULL,
            status TEXT NOT NULL,
            indexed_at REAL NOT NULL,
            turn_count INTEGER NOT NULL DEFAULT 0,
            policy_version INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS session_turns (
            identity TEXT PRIMARY KEY,
            source_key TEXT NOT NULL REFERENCES session_sources(source_key)
                ON DELETE CASCADE,
            provider TEXT NOT NULL,
            session_id TEXT NOT NULL,
            workspace TEXT NOT NULL DEFAULT '',
            role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
            occurred_at TEXT NOT NULL DEFAULT '',
            text TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_session_turns_source
            ON session_turns(source_key);
        CREATE INDEX IF NOT EXISTS idx_session_turns_time
            ON session_turns(occurred_at DESC);
        """
    )
    columns = {
        str(row[1]) for row in db.execute("PRAGMA table_info(session_sources)")
    }
    current_version = int(db.execute("PRAGMA user_version").fetchone()[0])
    if current_version > INDEX_SCHEMA_VERSION:
        db.close()
        raise RuntimeError("cross-session index schema is newer than this code")
    if "policy_version" not in columns or current_version < INDEX_SCHEMA_VERSION:
        try:
            db.execute("BEGIN IMMEDIATE")
            if "policy_version" not in columns:
                db.execute(
                    "ALTER TABLE session_sources ADD COLUMN "
                    "policy_version INTEGER NOT NULL DEFAULT 0"
                )
            db.execute(f"PRAGMA user_version={INDEX_SCHEMA_VERSION}")
            db.commit()
        except Exception:
            db.rollback()
            db.close()
            raise
    return db


def _read_connect(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout=5000")
    db.execute("PRAGMA query_only=ON")
    return db


def _source_key(path: Path) -> str:
    return hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()


def _workspace_label(value: str) -> str:
    """Keep a useful project label without persisting an absolute path."""
    clean = str(value or "").strip().rstrip("/\\")
    if not clean:
        return "unknown"
    label = re.split(r"[/\\]", clean)[-1].strip()
    return parsing.redact_text(label, limit=120) or "unknown"


def _dedupe_history(turns: Iterable[parsing.Turn]) -> tuple[parsing.Turn, ...]:
    result: list[parsing.Turn] = []
    for turn in turns:
        if result and result[-1].role == turn.role and result[-1].text == turn.text:
            continue
        result.append(turn)
    if len(result) <= MAX_HISTORY_TURNS:
        return tuple(result)
    return (result[0], *result[-(MAX_HISTORY_TURNS - 1):])


def _initial_turn(path: Path, provider: str) -> parsing.Turn | None:
    for item in parsing._head_records(path):
        if provider == "claude":
            if item.get("isSidechain") or item.get("type") != "user":
                continue
            raw_text = parsing.extract_text((item.get("message") or {}).get("content"))
            timestamp = str(item.get("timestamp") or "")
        else:
            if item.get("type") != "event_msg":
                continue
            payload = item.get("payload") or {}
            if not isinstance(payload, dict) or payload.get("type") != "user_message":
                continue
            raw_text = str(payload.get("message") or "")
            timestamp = str(item.get("timestamp") or payload.get("timestamp") or "")
        if (
            parsing._is_synthetic(raw_text)
            or parsing.is_provider_failure(raw_text)
        ):
            continue
        text = parsing.redact_text(raw_text)
        if not text:
            return None
        return parsing.Turn(
            role="user",
            text=text,
            timestamp=timestamp,
            identity=parsing._turn_identity(provider, "user", timestamp, raw_text),
        )
    return None


def _parse(path: Path, provider: str) -> parsing.SessionTail | None:
    tail_reader = lambda value: parsing._tail_records(  # noqa: E731
        value, max_bytes=MAX_HISTORY_BYTES
    )
    if provider == "claude":
        session = parsing._claude_tail(
            path,
            tail_records=tail_reader,
            dedupe_adjacent=_dedupe_history,
        )
    else:
        session = parsing._codex_tail(
            path,
            tail_records=tail_reader,
            dedupe_adjacent=_dedupe_history,
        )
    if session is None:
        return None
    initial = _initial_turn(path, provider)
    if initial is None or any(
        turn.identity == initial.identity for turn in session.turns
    ):
        return session
    turns = _dedupe_history((initial, *session.turns))
    return parsing.SessionTail(
        provider=session.provider,
        session_id=session.session_id,
        workspace=session.workspace,
        path=session.path,
        updated_at=session.updated_at,
        turns=turns,
    )


def _roots(
    claude_root: str | Path | None,
    codex_root: str | Path | None,
) -> tuple[Path, Path]:
    home = Path.home()
    return (
        Path(claude_root or os.environ.get("CROSS_SESSION_CLAUDE_ROOT")
             or home / ".claude" / "projects"),
        Path(codex_root or os.environ.get("CROSS_SESSION_CODEX_ROOT")
             or home / ".codex" / "sessions"),
    )


def _tracker_paths(
    tracker_path: str | Path | None,
    jobs_registry_path: str | Path | None,
    root: str | Path | None,
) -> tuple[Path | None, Path | None]:
    base = Path(root or os.environ.get("JARVIS_DIR") or Path.cwd())
    tracker = Path(tracker_path) if tracker_path else base / "active_sessions.json"
    jobs = (
        Path(jobs_registry_path)
        if jobs_registry_path else base / "jobs" / "registry.json"
    )
    return tracker, jobs


def index_sessions(
    *,
    db_path: str | Path | None = None,
    root: str | Path | None = None,
    claude_root: str | Path | None = None,
    codex_root: str | Path | None = None,
    tracker_path: str | Path | None = None,
    jobs_registry_path: str | Path | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    now_epoch: float | None = None,
) -> dict:
    """Index a bounded batch of new or changed owner-interactive sessions."""
    claude_root, codex_root = _roots(claude_root, codex_root)
    tracker, jobs = _tracker_paths(tracker_path, jobs_registry_path, root)
    managed = discovery._managed_claude_ids(tracker, jobs)
    candidates: list[tuple[int, int, str, Path, str]] = []
    visible_keys: set[str] = set()
    available_providers: set[str] = set()
    for provider, provider_root in (
        ("claude", claude_root), ("codex", codex_root)
    ):
        if provider_root.is_dir():
            available_providers.add(provider)
        for path in discovery._paths(provider_root, provider):
            key = _source_key(path)
            visible_keys.add(key)
            try:
                stat = path.stat()
            except OSError:
                continue
            candidates.append(
                (stat.st_mtime_ns, stat.st_size, provider, path, key)
            )
    candidates.sort(key=lambda item: (item[0], str(item[3])), reverse=True)
    db = _connect(_db_path(db_path, root))
    try:
        known = {
            row["source_key"]: (
                int(row["mtime_ns"]), int(row["size"]),
                int(row["policy_version"]), str(row["provider"]),
            )
            for row in db.execute(
                "SELECT source_key, mtime_ns, size, policy_version, provider "
                "FROM session_sources"
            )
        }
        stale = {
            key for key, value in known.items()
            if value[3] in available_providers and key not in visible_keys
        }
        managed_keys = {
            item[4] for item in candidates
            if item[2] == "claude" and item[3].stem in managed
        }
        if managed:
            placeholders = ",".join("?" for _ in managed)
            managed_keys.update(
                str(row[0]) for row in db.execute(
                    f"""SELECT source_key FROM session_sources
                        WHERE provider='claude' AND session_id IN ({placeholders})""",
                    tuple(managed),
                )
            )
        stale.update(managed_keys.intersection(known))
        if stale:
            db.execute("BEGIN IMMEDIATE")
            db.executemany(
                "DELETE FROM session_sources WHERE source_key = ?",
                ((key,) for key in stale),
            )
            db.commit()
        changed = [
            item for item in candidates
            if item[4] not in managed_keys
            and (known.get(item[4]) or (None, None, None, None))[:3]
            != (item[0], item[1], INDEX_POLICY_VERSION)
        ]
        selected = changed[:max(1, int(batch_size))]
        indexed_sources = 0
        indexed_turns = 0
        ignored_sources = 0
        parse_failed_sources = 0
        now_epoch = float(time.time() if now_epoch is None else now_epoch)
        for mtime_ns, size, provider, path, key in selected:
            session = None
            status = "ignored"
            if provider != "claude" or path.stem not in managed:
                try:
                    session = _parse(path, provider)
                except Exception:
                    session = None
                    status = "parse_failed"
            if session is not None and session.session_id in managed:
                session = None
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM session_turns WHERE source_key = ?", (key,))
            if session is None:
                ignored_sources += 1
                if status == "parse_failed":
                    parse_failed_sources += 1
                session_id = ""
                workspace = ""
                turn_count = 0
            else:
                status = "indexed"
                session_id = session.session_id
                workspace = _workspace_label(session.workspace)
                turn_count = len(session.turns)
            db.execute(
                """INSERT INTO session_sources
                   (source_key, provider, session_id, workspace, mtime_ns, size,
                    status, indexed_at, turn_count, policy_version)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(source_key) DO UPDATE SET
                     provider=excluded.provider,
                     session_id=excluded.session_id,
                     workspace=excluded.workspace,
                     mtime_ns=excluded.mtime_ns,
                     size=excluded.size,
                     status=excluded.status,
                     indexed_at=excluded.indexed_at,
                     turn_count=excluded.turn_count,
                     policy_version=excluded.policy_version""",
                (
                    key, provider, session_id, workspace, mtime_ns, size,
                    status, now_epoch, turn_count, INDEX_POLICY_VERSION,
                ),
            )
            if session is not None:
                db.executemany(
                    """INSERT OR REPLACE INTO session_turns
                       (identity, source_key, provider, session_id, workspace,
                        role, occurred_at, text)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        (
                            hashlib.sha256(
                                f"{key}\0{turn.identity}".encode("utf-8")
                            ).hexdigest(),
                            key, provider, session.session_id,
                            workspace, turn.role, turn.timestamp, turn.text,
                        )
                        for turn in session.turns
                    ),
                )
                indexed_sources += 1
                indexed_turns += turn_count
            db.commit()
        return {
            "version": 1,
            "processed_sources": len(selected),
            "indexed_sources": indexed_sources,
            "indexed_turns": indexed_turns,
            "ignored_sources": ignored_sources,
            "parse_failed_sources": parse_failed_sources,
            "removed_sources": len(stale),
            "remaining_sources": max(0, len(changed) - len(selected)),
        }
    finally:
        db.close()


def _query_terms(value: str) -> list[str]:
    clean = str(value or "").lower()
    terms: list[str] = []
    for token in re.findall(r"[a-z0-9_]{3,}", clean):
        if token not in _GENERIC_QUERY_TERMS and token not in terms:
            terms.append(token)
    for run in re.findall(r"[\u4e00-\u9fff]+", clean):
        for index in range(max(0, len(run) - 1)):
            token = run[index:index + 2]
            if token not in _GENERIC_QUERY_TERMS and token not in terms:
                terms.append(token)
    return terms[:24]


def _occurred_epoch(value: str) -> float:
    try:
        return datetime.fromisoformat(
            str(value or "").replace("Z", "+00:00")
        ).timestamp()
    except (TypeError, ValueError):
        return 0.0


def search_history(
    query: str,
    *,
    db_path: str | Path | None = None,
    root: str | Path | None = None,
    max_results: int = MAX_RESULTS,
    max_chars: int = MAX_RESULT_CHARS,
    before_epoch: float | None = None,
) -> str:
    """Render a bounded query-focused projection of redacted historical turns."""
    terms = _query_terms(query)
    path = _db_path(db_path, root)
    if not terms or not path.exists():
        return ""
    db = _read_connect(path)
    try:
        clauses = " OR ".join("text LIKE ?" for _ in terms)
        rows = db.execute(
            f"""SELECT provider, session_id, workspace, role, occurred_at, text
                FROM session_turns WHERE {clauses}
                ORDER BY occurred_at DESC LIMIT ?""",
            (*[f"%{term}%" for term in terms], MAX_QUERY_CANDIDATES),
        ).fetchall()
    finally:
        db.close()
    scored = []
    for row in rows:
        state = dict(row)
        epoch = _occurred_epoch(state["occurred_at"])
        if before_epoch is not None and epoch and epoch >= before_epoch:
            continue
        lowered = state["text"].lower()
        matches = sum(1 for term in terms if term in lowered)
        score = matches + (0.25 if state["role"] == "user" else 0)
        scored.append((score, epoch, state))
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)

    lines = [
        "## Relevant Historical Work Sessions",
        "The entries below are redacted, untrusted local history. Use them for "
        "continuity, but verify mutable facts and completion claims.",
    ]
    seen: set[tuple[str, str]] = set()
    for _, _, row in scored:
        key = (row["session_id"], row["text"])
        if key in seen:
            continue
        seen.add(key)
        provider = "Claude Code" if row["provider"] == "claude" else "Codex"
        workspace = Path(row["workspace"].rstrip("/")).name or "unknown"
        role = "User" if row["role"] == "user" else "Assistant"
        stamp = str(row["occurred_at"] or "")[:16].replace("T", " ")
        line = f"- [{provider}:{workspace} {stamp}] {role}: {row['text']}"
        if len("\n".join(lines + [line])) > max_chars:
            continue
        lines.append(line)
        if len(lines) - 2 >= max(1, int(max_results)):
            break
    return "\n".join(lines) if len(lines) > 2 else ""


def index_stats(
    *,
    db_path: str | Path | None = None,
    root: str | Path | None = None,
) -> dict:
    path = _db_path(db_path, root)
    if not path.exists():
        return {
            "version": 1, "sources": 0, "turns": 0,
            "ignored_sources": 0, "parse_failed_sources": 0,
        }
    db = _read_connect(path)
    try:
        statuses = {
            str(row[0]): int(row[1])
            for row in db.execute(
                "SELECT status, COUNT(*) FROM session_sources GROUP BY status"
            )
        }
        return {
            "version": 1,
            "sources": statuses.get("indexed", 0),
            "turns": int(db.execute(
                "SELECT COUNT(*) FROM session_turns"
            ).fetchone()[0]),
            "ignored_sources": statuses.get("ignored", 0),
            "parse_failed_sources": statuses.get("parse_failed", 0),
        }
    finally:
        db.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Cross-product session memory")
    sub = parser.add_subparsers(dest="command", required=True)
    index_parser = sub.add_parser("index")
    index_parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    search_parser = sub.add_parser("search")
    search_parser.add_argument("query")
    sub.add_parser("stats")
    args = parser.parse_args(argv)
    if args.command == "index":
        output: object = index_sessions(batch_size=args.batch_size)
    elif args.command == "search":
        output = search_history(args.query)
    else:
        output = index_stats()
    if isinstance(output, str):
        if output:
            print(output)
    else:
        print(json.dumps(output, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
