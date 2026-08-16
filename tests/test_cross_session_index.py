import json
import os
import sqlite3
import stat
from pathlib import Path

import pytest

from core import cross_session_index
from core.prompt import _external_work_context


def _write(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def _claude(path: Path, session_id: str, topic: str) -> None:
    _write(path, [
        {
            "type": "user", "sessionId": session_id, "cwd": "/work/alpha",
            "timestamp": "2026-06-01T09:00:00Z",
            "message": {"content": f"决定采用{topic}，api_key=hidden-secret"},
        },
        {
            "type": "assistant", "sessionId": session_id, "cwd": "/work/alpha",
            "timestamp": "2026-06-01T09:01:00Z",
            "message": {"content": f"已记录{topic}的下一步"},
        },
    ])


def _codex(path: Path, session_id: str, topic: str) -> None:
    _write(path, [
        {"type": "session_meta", "payload": {
            "id": session_id, "cwd": "/work/beta", "source": "vscode",
        }},
        {"type": "event_msg", "timestamp": "2026-05-01T10:00:00Z",
         "payload": {"type": "user_message", "message": f"研究{topic}的方案"}},
        {"type": "event_msg", "timestamp": "2026-05-01T10:01:00Z",
         "payload": {"type": "agent_message", "message": f"{topic}采用数据库索引"}},
    ])


def test_index_backfills_both_products_and_retrieves_relevant_history(tmp_path):
    claude = tmp_path / "claude"
    codex = tmp_path / "codex"
    _claude(claude / "project" / "one.jsonl", "claude-one", "模型控制平面")
    _codex(codex / "2026" / "two.jsonl", "codex-two", "跨产品记忆")
    db = tmp_path / "history.db"

    report = cross_session_index.index_sessions(
        db_path=db, claude_root=claude, codex_root=codex,
        tracker_path=tmp_path / "missing.json", batch_size=10,
    )
    model_history = cross_session_index.search_history(
        "之前模型控制平面是怎么决定的", db_path=db,
    )
    memory_history = cross_session_index.search_history(
        "跨产品记忆数据库", db_path=db,
    )

    assert report["indexed_sources"] == 2
    assert report["indexed_turns"] == 4
    assert "Claude Code" in model_history
    assert "模型控制平面" in model_history
    assert "Codex" in memory_history
    assert "跨产品记忆" in memory_history
    assert "hidden-secret" not in model_history
    assert str(claude / "project" / "one.jsonl") not in db.read_text(
        encoding="utf-8", errors="ignore"
    )
    assert "/work/alpha" not in db.read_text(encoding="utf-8", errors="ignore")
    assert db.stat().st_mode & 0o777 == 0o600


def test_relevance_ranking_prevents_recent_broad_matches_from_starving_decision(
    tmp_path,
):
    db_path = tmp_path / "history.db"
    db = cross_session_index._connect(db_path)
    try:
        db.execute(
            """INSERT INTO session_sources
               (source_key,provider,session_id,workspace,mtime_ns,size,status,
                indexed_at,turn_count,policy_version)
               VALUES ('source','codex','decision','jarvis',1,1,'indexed',
                       1,301,?)""",
            (cross_session_index.INDEX_POLICY_VERSION,),
        )
        rows = [
            (
                "decision", "source", "codex", "decision", "jarvis",
                "user", "2026-01-01T00:00:00Z",
                "好友申请通过了100次仍然反复弹窗，这个功能必须修复",
            )
        ]
        rows.extend(
            (
                f"noise-{index}", "source", "codex", "decision", "jarvis",
                "assistant", f"2026-08-15T12:{index % 60:02d}:00Z",
                f"普通功能状态更新 {index}",
            )
            for index in range(300)
        )
        db.executemany(
            """INSERT INTO session_turns
               (identity,source_key,provider,session_id,workspace,role,
                occurred_at,text) VALUES (?,?,?,?,?,?,?,?)""",
            rows,
        )
        db.commit()
    finally:
        db.close()

    history = cross_session_index.search_history(
        "好友申请 通过 100 次 功能", db_path=db_path, max_results=1,
    )

    assert "反复弹窗" in history
    assert "普通功能状态更新" not in history


def test_compact_chinese_phrase_outranks_scattered_term_matches(tmp_path):
    db_path = tmp_path / "history.db"
    db = cross_session_index._connect(db_path)
    try:
        db.execute(
            """INSERT INTO session_sources
               (source_key,provider,session_id,workspace,mtime_ns,size,status,
                indexed_at,turn_count,policy_version)
               VALUES ('source','codex','decision','jarvis',1,1,'indexed',
                       1,2,?)""",
            (cross_session_index.INDEX_POLICY_VERSION,),
        )
        db.executemany(
            """INSERT INTO session_turns
               (identity,source_key,provider,session_id,workspace,role,
                occurred_at,text) VALUES (?,?,?,?,?,?,?,?)""",
            [
                (
                    "decision", "source", "codex", "decision", "jarvis",
                    "user", "2026-01-01T00:00:00Z",
                    "好友申请已经通过但仍反复弹窗",
                ),
                (
                    "distractor", "source", "codex", "decision", "jarvis",
                    "assistant", "2026-08-15T00:00:00Z",
                    "好友关系需要重新提交申请，检查已经通过",
                ),
            ],
        )
        db.commit()
    finally:
        db.close()

    history = cross_session_index.search_history(
        "好友申请 通过", db_path=db_path, max_results=1,
    )

    assert "反复弹窗" in history
    assert "重新提交申请" not in history


def test_multi_concept_query_returns_empty_for_weak_single_topic_noise(
    tmp_path,
):
    db_path = tmp_path / "history.db"
    db = cross_session_index._connect(db_path)
    try:
        db.execute(
            """INSERT INTO session_sources
               (source_key,provider,session_id,workspace,mtime_ns,size,status,
                indexed_at,turn_count,policy_version)
               VALUES ('source','codex','noise','jarvis',1,1,'indexed',
                       1,1,?)""",
            (cross_session_index.INDEX_POLICY_VERSION,),
        )
        db.execute(
            """INSERT INTO session_turns
               (identity,source_key,provider,session_id,workspace,role,
                occurred_at,text) VALUES
               ('noise','source','codex','noise','jarvis','assistant',
                '2026-08-15T00:00:00Z','普通功能价格是100')"""
        )
        db.commit()
    finally:
        db.close()

    history = cross_session_index.search_history(
        "好友申请 通过 100 次 功能", db_path=db_path,
    )

    assert history == ""


def test_index_is_private_before_sqlite_opens_it(tmp_path, monkeypatch):
    db = tmp_path / "history.db"
    real_connect = sqlite3.connect
    observed_modes = []

    def connect_after_permission_check(path, *args, **kwargs):
        observed_modes.append(Path(path).stat().st_mode & 0o777)
        return real_connect(path, *args, **kwargs)

    monkeypatch.setattr(cross_session_index.sqlite3, "connect", connect_after_permission_check)

    connection = cross_session_index._connect(db)
    connection.close()

    assert observed_modes == [0o600]
    assert stat.S_ISREG(db.stat().st_mode)


def test_existing_index_is_made_private_before_sqlite_opens_it(
    tmp_path, monkeypatch,
):
    db = tmp_path / "history.db"
    db.write_bytes(b"")
    db.chmod(0o644)
    real_connect = sqlite3.connect
    observed_modes = []

    def connect_after_permission_check(path, *args, **kwargs):
        observed_modes.append(Path(path).stat().st_mode & 0o777)
        return real_connect(path, *args, **kwargs)

    monkeypatch.setattr(cross_session_index.sqlite3, "connect", connect_after_permission_check)

    connection = cross_session_index._connect(db)
    connection.close()

    assert observed_modes == [0o600]


def test_index_refuses_a_symlink_database_path(tmp_path):
    target = tmp_path / "unrelated.db"
    target.write_text("must remain untouched", encoding="utf-8")
    db = tmp_path / "history.db"
    db.symlink_to(target)

    with pytest.raises(OSError):
        cross_session_index._connect(db)

    assert target.read_text(encoding="utf-8") == "must remain untouched"


def test_index_is_incremental_and_replaces_changed_source_without_duplicates(tmp_path):
    codex = tmp_path / "codex"
    path = codex / "one.jsonl"
    _codex(path, "codex-one", "最初决策")
    db = tmp_path / "history.db"
    kwargs = {
        "db_path": db,
        "claude_root": tmp_path / "claude",
        "codex_root": codex,
        "tracker_path": tmp_path / "missing.json",
        "batch_size": 10,
    }

    first = cross_session_index.index_sessions(**kwargs)
    second = cross_session_index.index_sessions(**kwargs)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "type": "event_msg", "timestamp": "2026-05-01T10:02:00Z",
            "payload": {"type": "user_message", "message": "追加最终决策"},
        }, ensure_ascii=False) + "\n")
    os.utime(path, None)
    third = cross_session_index.index_sessions(**kwargs)

    assert first["indexed_sources"] == 1
    assert second["indexed_sources"] == 0
    assert third["indexed_sources"] == 1
    stats = cross_session_index.index_stats(db_path=db)
    assert stats["turns"] == 3
    assert "追加最终决策" in cross_session_index.search_history(
        "最终决策", db_path=db,
    )


def test_removed_session_text_is_not_left_in_database_or_wal(tmp_path):
    codex = tmp_path / "codex"
    path = codex / "one.jsonl"
    phrase = "SHOULD_NOT_SURVIVE_DELETION_8f31"
    _codex(path, "codex-one", phrase)
    db = tmp_path / "history.db"
    kwargs = {
        "db_path": db,
        "claude_root": tmp_path / "claude",
        "codex_root": codex,
        "tracker_path": tmp_path / "missing.json",
        "batch_size": 10,
    }
    cross_session_index.index_sessions(**kwargs)

    path.unlink()
    cross_session_index.index_sessions(**kwargs)

    assert cross_session_index.index_stats(db_path=db)["turns"] == 0
    raw = b"".join(
        candidate.read_bytes()
        for candidate in (db, Path(f"{db}-wal"), Path(f"{db}-shm"))
        if candidate.exists()
    )
    assert phrase.encode("utf-8") not in raw


def test_index_excludes_managed_provider_calls_and_removes_deleted_sources(tmp_path):
    claude = tmp_path / "claude"
    managed = claude / "project" / "managed.jsonl"
    human = claude / "project" / "human.jsonl"
    _claude(managed, "managed", "不应进入")
    _claude(human, "human", "应该进入")
    tracker = tmp_path / "active_sessions.json"
    tracker.write_text(json.dumps({
        "owner": {"session_id": "managed", "counter": 0},
    }), encoding="utf-8")
    db = tmp_path / "history.db"
    kwargs = {
        "db_path": db, "claude_root": claude,
        "codex_root": tmp_path / "codex", "tracker_path": tracker,
        "batch_size": 10,
    }

    cross_session_index.index_sessions(**kwargs)
    assert "不应进入" not in cross_session_index.search_history(
        "不应进入", db_path=db,
    )
    assert "应该进入" in cross_session_index.search_history(
        "应该进入", db_path=db,
    )

    human.unlink()
    cross_session_index.index_sessions(**kwargs)
    assert cross_session_index.index_stats(db_path=db)["turns"] == 0


def test_owner_external_context_retrieves_old_focus_without_exposing_generic_archive(
    tmp_path, monkeypatch,
):
    jarvis = tmp_path / "jarvis"
    codex = tmp_path / "codex"
    jarvis.mkdir()
    path = codex / "old.jsonl"
    _codex(path, "old-codex", "董责险跨 Agent 转发")
    monkeypatch.setenv("JARVIS_DIR", str(jarvis))
    monkeypatch.setenv("CROSS_SESSION_CLAUDE_ROOT", str(tmp_path / "claude"))
    monkeypatch.setenv("CROSS_SESSION_CODEX_ROOT", str(codex))
    cross_session_index.index_sessions(root=jarvis, batch_size=10)

    focused = _external_work_context(jarvis, "之前董责险转发是怎么设计的")
    generic = _external_work_context(jarvis, "继续")

    assert "Relevant Historical Work Sessions" in focused
    assert "董责险跨 Agent 转发" in focused
    assert "Relevant Historical Work Sessions" not in generic


def test_identical_turns_in_different_sessions_do_not_collide(tmp_path):
    codex = tmp_path / "codex"
    _codex(codex / "one.jsonl", "one", "相同主题")
    _codex(codex / "two.jsonl", "two", "相同主题")
    db = tmp_path / "history.db"

    cross_session_index.index_sessions(
        db_path=db, claude_root=tmp_path / "claude", codex_root=codex,
        tracker_path=tmp_path / "missing.json", batch_size=10,
    )

    assert cross_session_index.index_stats(db_path=db) == {
        "version": 1, "sources": 2, "turns": 4,
        "ignored_sources": 0, "parse_failed_sources": 0,
    }


def test_compact_chinese_topic_is_searchable_but_generic_continue_is_not(tmp_path):
    codex = tmp_path / "codex"
    _codex(codex / "one.jsonl", "one", "董责险")
    db = tmp_path / "history.db"
    cross_session_index.index_sessions(
        db_path=db, claude_root=tmp_path / "claude", codex_root=codex,
        tracker_path=tmp_path / "missing.json", batch_size=10,
    )

    assert "董责险" in cross_session_index.search_history("董责险", db_path=db)
    assert cross_session_index.search_history("继续", db_path=db) == ""


def test_parse_failures_are_counted_without_storing_transcript_text(
    tmp_path, monkeypatch,
):
    codex = tmp_path / "codex"
    path = codex / "one.jsonl"
    _codex(path, "one", "不可持久化的内容")
    db = tmp_path / "history.db"
    monkeypatch.setattr(
        cross_session_index, "_parse",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("bad row")),
    )

    report = cross_session_index.index_sessions(
        db_path=db, claude_root=tmp_path / "claude", codex_root=codex,
        tracker_path=tmp_path / "missing.json", batch_size=10,
    )
    stats = cross_session_index.index_stats(db_path=db)

    assert report["parse_failed_sources"] == 1
    assert stats["parse_failed_sources"] == 1
    assert stats["turns"] == 0
    assert "不可持久化的内容" not in db.read_text(
        encoding="utf-8", errors="ignore",
    )


def test_pasted_provider_failures_do_not_become_historical_memory(tmp_path):
    codex = tmp_path / "codex"
    _write(codex / "one.jsonl", [
        {"type": "session_meta", "payload": {
            "id": "provider-failure", "cwd": "/work/beta", "source": "vscode",
        }},
        {"type": "event_msg", "timestamp": "2026-05-01T10:00:00Z",
         "payload": {"type": "user_message",
                     "message": "You've hit your weekly limit · resets tomorrow"}},
        {"type": "event_msg", "timestamp": "2026-05-01T10:01:00Z",
         "payload": {"type": "user_message", "message": "改用模型控制平面"}},
    ])
    db = tmp_path / "history.db"

    cross_session_index.index_sessions(
        db_path=db, claude_root=tmp_path / "claude", codex_root=codex,
        tracker_path=tmp_path / "missing.json", batch_size=10,
    )
    raw = db.read_text(encoding="utf-8", errors="ignore")

    assert "weekly limit" not in raw
    assert "模型控制平面" in cross_session_index.search_history(
        "模型控制平面", db_path=db,
    )


def test_session_becoming_managed_is_purged_without_transcript_change(tmp_path):
    claude = tmp_path / "claude"
    session = claude / "project" / "later-managed.jsonl"
    _claude(session, "later-managed", "先前人工会话")
    tracker = tmp_path / "active_sessions.json"
    tracker.write_text("{}", encoding="utf-8")
    db = tmp_path / "history.db"
    kwargs = {
        "db_path": db, "claude_root": claude,
        "codex_root": tmp_path / "codex", "tracker_path": tracker,
        "batch_size": 10,
    }
    cross_session_index.index_sessions(**kwargs)
    assert cross_session_index.index_stats(db_path=db)["turns"] == 2

    tracker.write_text(json.dumps({
        "owner": {"session_id": "later-managed", "counter": 0},
    }), encoding="utf-8")
    report = cross_session_index.index_sessions(**kwargs)

    assert report["removed_sources"] == 1
    assert cross_session_index.index_stats(db_path=db)["turns"] == 0


def test_missing_provider_root_does_not_delete_rebuildable_history(tmp_path):
    codex = tmp_path / "codex"
    _codex(codex / "one.jsonl", "one", "离线根目录保留")
    db = tmp_path / "history.db"
    cross_session_index.index_sessions(
        db_path=db, claude_root=tmp_path / "claude", codex_root=codex,
        tracker_path=tmp_path / "missing.json", batch_size=10,
    )
    renamed = tmp_path / "codex-offline"
    codex.rename(renamed)

    report = cross_session_index.index_sessions(
        db_path=db, claude_root=tmp_path / "claude", codex_root=codex,
        tracker_path=tmp_path / "missing.json", batch_size=10,
    )

    assert report["removed_sources"] == 0
    assert cross_session_index.index_stats(db_path=db)["turns"] == 2


def test_search_path_is_sqlite_read_only(tmp_path):
    codex = tmp_path / "codex"
    _codex(codex / "one.jsonl", "one", "只读检索")
    db = tmp_path / "history.db"
    cross_session_index.index_sessions(
        db_path=db, claude_root=tmp_path / "claude", codex_root=codex,
        tracker_path=tmp_path / "missing.json", batch_size=10,
    )
    before = db.stat().st_mtime_ns

    assert "只读检索" in cross_session_index.search_history(
        "只读检索", db_path=db,
    )
    assert db.stat().st_mtime_ns == before
    with sqlite3.connect(db) as check:
        assert check.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_legacy_index_schema_migrates_transactionally_on_next_index(tmp_path):
    db = tmp_path / "legacy.db"
    with sqlite3.connect(db) as legacy:
        legacy.executescript("""
            CREATE TABLE session_sources (
                source_key TEXT PRIMARY KEY, provider TEXT NOT NULL,
                session_id TEXT NOT NULL DEFAULT '', workspace TEXT NOT NULL DEFAULT '',
                mtime_ns INTEGER NOT NULL, size INTEGER NOT NULL,
                status TEXT NOT NULL, indexed_at REAL NOT NULL,
                turn_count INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE session_turns (
                identity TEXT PRIMARY KEY, source_key TEXT NOT NULL,
                provider TEXT NOT NULL, session_id TEXT NOT NULL,
                workspace TEXT NOT NULL DEFAULT '', role TEXT NOT NULL,
                occurred_at TEXT NOT NULL DEFAULT '', text TEXT NOT NULL
            );
        """)

    cross_session_index.index_sessions(
        db_path=db, claude_root=tmp_path / "claude",
        codex_root=tmp_path / "codex",
        tracker_path=tmp_path / "missing.json", batch_size=10,
    )

    with sqlite3.connect(db) as migrated:
        columns = {
            row[1] for row in migrated.execute("PRAGMA table_info(session_sources)")
        }
        assert "policy_version" in columns
        assert migrated.execute("PRAGMA user_version").fetchone()[0] == 1
