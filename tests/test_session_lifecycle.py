"""Logical-session lifecycle, context isolation, and reset regressions."""

from __future__ import annotations

import json
import sqlite3

import pytest

import core.db as db_module
from core import codex_fallback
from core.compact import get_compact_path
from core.conversation_context import (
    apply_runtime_transition,
    claim_pending_context,
    context_snapshot,
    current_context_generation,
    logical_context_key,
    queue_pending_context,
)
from core.matter_bridge import (
    bind_conversation,
    command_would_handle,
    context_for_matter,
    get_binding,
    handle_lark_command,
    recent_provider_context,
    record_turn,
)
from core.matters import create_matter, get_matter
from core.prompt import build_system_prompt
from core.session import SessionManager


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "jarvis.db")
    if db_module._connection is not None:
        db_module._connection.close()
    db_module._connection = None
    yield
    if db_module._connection is not None:
        db_module._connection.close()
    db_module._connection = None


def test_private_session_commands_create_switch_list_and_hide_provider_ids():
    created = handle_lark_command(
        "开个新会话 白皮书", "owner", "owner", chat_type="p2p"
    )
    first_id = get_binding("owner")["matter_id"]
    assert created["transition"]["context_key"] == f"matter:{first_id}"
    assert first_id not in created["reply"]

    second = create_matter("移动端")
    switched = handle_lark_command(
        "切换会话 移动端", "owner", "owner", chat_type="p2p"
    )
    assert get_binding("owner")["matter_id"] == second["id"]
    assert switched["transition"]["context_key"] == f"matter:{second['id']}"
    assert second["id"] not in switched["reply"]
    listing = handle_lark_command("会话列表", "owner")["reply"]
    assert "白皮书" in listing and "移动端" in listing
    assert "mat_" not in listing


@pytest.mark.parametrize("text", [
    "新开会话 研究", "/session new 研究", "会话 新建 研究",
])
def test_session_new_aliases(text):
    result = handle_lark_command(text, "owner", "owner", chat_type="p2p")
    assert result["handled"] is True
    assert "transition" in result


def test_group_session_and_matter_commands_fail_closed():
    for command in ("新开会话 私人计划", "/matter new 私人计划"):
        result = handle_lark_command(
            command, "group", "group", chat_type="group"
        )
        assert result == {"handled": True, "reply": "会话和事项管理只在你的私聊中开放。"}
    assert get_binding("group") is None


def test_close_archives_and_unbinds_with_fresh_unbound_context():
    matter = create_matter("已完成研究")
    bind_conversation("owner", matter["id"])

    result = handle_lark_command("结束会话 结论已交付", "owner")

    assert get_binding("owner") is None
    assert get_matter(matter["id"])["outcome"] == "结论已交付"
    assert result["transition"] == {"context_key": "conversation:owner"}
    assert matter["id"] not in result["reply"]


def test_legacy_matter_done_also_leaves_closed_context():
    matter = create_matter("旧命令完成")
    bind_conversation("owner", matter["id"])

    result = handle_lark_command("/matter done 已完成", "owner")

    assert get_binding("owner") is None
    assert result["transition"] == {"context_key": "conversation:owner"}


def test_close_rolls_back_matter_when_unbind_fails():
    matter = create_matter("原子关闭")
    bind_conversation("owner", matter["id"])
    db = db_module.get_db()
    db.execute(
        """CREATE TRIGGER reject_binding_delete
           BEFORE DELETE ON matter_bindings
           BEGIN SELECT RAISE(ABORT, 'synthetic unbind failure'); END"""
    )
    db.commit()

    with pytest.raises(sqlite3.IntegrityError, match="synthetic unbind"):
        handle_lark_command("结束会话 不应半提交", "owner")

    unchanged = get_matter(matter["id"])
    assert unchanged["status"] == "active"
    assert unchanged["outcome"] == ""
    assert get_binding("owner")["matter_id"] == matter["id"]
    assert not any(
        event["event_type"] == "conversation_unbound"
        for event in unchanged["events"]
    )


@pytest.mark.parametrize("text", [
    "新开会话 研究", "会话列表", "当前模型", "上一下备用吧",
    "/matter done 完成",
])
def test_deterministic_command_classifier_fails_closed_before_execution(text):
    assert command_would_handle(text) is True


def test_ordinary_conversation_is_not_a_deterministic_command():
    assert command_would_handle("我今天想继续聊白皮书") is False


def test_matter_turns_compacts_and_prompts_are_isolated(tmp_path):
    memory = tmp_path / "memory"
    sessions = tmp_path / "sessions"
    memory.mkdir()
    sessions.mkdir()
    tracker = tmp_path / "active_sessions.json"
    tracker.write_text("{}", encoding="utf-8")
    first = create_matter("白皮书", summary="只属于白皮书")
    second = create_matter("移动端", summary="只属于移动端")
    first_key = logical_context_key("owner", first["id"])
    second_key = logical_context_key("owner", second["id"])
    record_turn("owner", "user", "WHITEPAPER_PRIVATE", context_key=first_key)
    record_turn("owner", "user", "MOBILE_PRIVATE", context_key=second_key)
    for key, text in ((first_key, "WHITEPAPER_COMPACT"),
                      (second_key, "MOBILE_COMPACT")):
        path = get_compact_path(tmp_path, key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    prompt = build_system_prompt(
        str(tmp_path), str(memory), str(sessions), "sid-mobile", "owner",
        "2026-08-13 20:00", str(tracker), context_key=second_key,
        matter_id=second["id"],
    )

    assert "MOBILE_PRIVATE" in prompt and "MOBILE_COMPACT" in prompt
    assert "只属于移动端" in prompt
    assert "WHITEPAPER_PRIVATE" not in prompt
    assert "WHITEPAPER_COMPACT" not in prompt
    assert "只属于白皮书" not in prompt
    assert "WHITEPAPER_PRIVATE" in recent_provider_context(
        "owner", context_key=first_key)
    assert "MOBILE_PRIVATE" not in recent_provider_context(
        "owner", context_key=first_key)


def test_delayed_reply_is_recorded_against_dispatch_snapshot():
    first = create_matter("先发出的工作")
    second = create_matter("后来切换的工作")
    bind_conversation("owner", first["id"])
    snapshot = context_snapshot("owner")
    bind_conversation("owner", second["id"])

    record_turn(
        "owner", "assistant", "LATE_REPLY", message_id="late",
        context_key=snapshot["context_key"], matter_id=snapshot["matter_id"],
    )

    first_events = get_matter(first["id"])["events"]
    second_events = get_matter(second["id"])["events"]
    assert any(event["summary"] == "LATE_REPLY" for event in first_events)
    assert not any(event["summary"] == "LATE_REPLY" for event in second_events)


def test_turn_rejects_mismatched_context_and_matter():
    first = create_matter("正确上下文")
    second = create_matter("错误事件目标")

    with pytest.raises(ValueError, match="does not match"):
        record_turn(
            "owner", "assistant", "must not land",
            context_key=f"matter:{first['id']}", matter_id=second["id"],
        )


def test_pending_merge_claims_only_exact_logical_context(tmp_path):
    queue = tmp_path / "pending_merge.jsonl"
    queue_pending_context(
        queue, conv_key="owner", context_key="matter:first",
        job_id="first", timestamp="now", summary="FIRST_RESULT",
    )
    queue_pending_context(
        queue, conv_key="owner", context_key="matter:second",
        job_id="second", timestamp="now", summary="SECOND_RESULT",
    )
    # A legacy row is deliberately unscoped and therefore belongs only to the
    # unbound transport conversation.
    from core.jsonl import append_jsonl_locked
    append_jsonl_locked(queue, {
        "conv_key": "owner", "job_id": "legacy", "summary": "LEGACY",
    })

    first = claim_pending_context(
        queue, conv_key="owner", context_key="matter:first")
    assert [row["job_id"] for row in first] == ["first"]
    unbound = claim_pending_context(
        queue, conv_key="owner", context_key="conversation:owner")
    assert [row["job_id"] for row in unbound] == ["legacy"]
    second = claim_pending_context(
        queue, conv_key="owner", context_key="matter:second")
    assert [row["job_id"] for row in second] == ["second"]


def test_pending_merge_discards_prior_reset_generation(tmp_path):
    queue = tmp_path / "pending_merge.jsonl"
    queue_pending_context(
        queue, conv_key="owner", context_key="matter:first",
        job_id="old", timestamp="now", summary="OLD_RESULT",
    )
    queue_pending_context(
        queue, conv_key="owner", context_key="matter:first@g1",
        job_id="current", timestamp="now", summary="CURRENT_RESULT",
    )

    claimed = claim_pending_context(
        queue, conv_key="owner", context_key="matter:first@g1")

    assert [row["job_id"] for row in claimed] == ["current"]
    assert queue.read_text(encoding="utf-8") == ""


def test_reset_clears_only_derived_context_and_preserves_raw_session(tmp_path):
    tracker = tmp_path / "active_sessions.json"
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    tracker.write_text("{}", encoding="utf-8")
    matter = create_matter("保留长期目标", summary="长期共识不能删除")
    bind_conversation("owner", matter["id"])
    key = logical_context_key("owner", matter["id"])
    manager = SessionManager(tracker, session_dir)
    old_sid, _ = manager.get_session("owner", key)
    raw = session_dir / f"{old_sid}.jsonl"
    raw.write_text('{"type":"user"}\n', encoding="utf-8")
    record_turn("owner", "user", "EPHEMERAL", context_key=key)
    codex_fallback.save_session(key, "codex-thread", "gpt-test", str(tmp_path))
    compact = get_compact_path(tmp_path, key)
    compact.parent.mkdir(parents=True, exist_ok=True)
    compact.write_text("EPHEMERAL_COMPACT", encoding="utf-8")

    result = apply_runtime_transition(
        conv_key="owner", context_key=key, tracker_path=tracker,
        session_dir=session_dir, jarvis_dir=tmp_path, reset=True,
    )

    assert result["session_id"] != old_sid
    assert result["context_key"] == f"{key}@g1"
    assert current_context_generation(key) == 1
    assert raw.exists()
    assert get_matter(matter["id"])["summary"] == "长期共识不能删除"
    assert recent_provider_context("owner", context_key=key) == ""
    assert codex_fallback.load_session(key) is None
    assert not compact.exists()
    assert "EPHEMERAL" not in context_for_matter(matter["id"])


def test_late_old_generation_writer_cannot_repopulate_reset_prompt(tmp_path):
    matter = create_matter("代际隔离", summary="长期目标保留")
    bind_conversation("owner", matter["id"])
    old_scope = context_snapshot("owner")["context_key"]
    tracker = tmp_path / "tracker.json"
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    SessionManager(tracker, sessions).get_session("owner", old_scope)

    apply_runtime_transition(
        conv_key="owner", context_key=old_scope, tracker_path=tracker,
        session_dir=sessions, jarvis_dir=tmp_path, reset=True,
    )
    new_scope = context_snapshot("owner")["context_key"]
    assert new_scope.endswith("@g1")

    # Simulate an assistant receipt that started before reset and committed
    # afterwards.  It remains auditable under generation 0 but is not current.
    record_turn(
        "owner", "assistant", "LATE_SECRET_FROM_OLD_GENERATION",
        context_key=old_scope, matter_id=matter["id"], message_id="late-old",
    )
    record_turn(
        "owner", "assistant", "CURRENT_GENERATION",
        context_key=new_scope, matter_id=matter["id"], message_id="current",
    )

    assert "LATE_SECRET" not in recent_provider_context(
        "owner", context_key=new_scope)
    assert "CURRENT_GENERATION" in recent_provider_context(
        "owner", context_key=new_scope)
    assert "LATE_SECRET" not in context_for_matter(matter["id"])
    assert "CURRENT_GENERATION" in context_for_matter(matter["id"])


def test_late_old_context_reply_does_not_replace_current_provider_status():
    first = create_matter("旧上下文")
    second = create_matter("当前上下文")
    first_scope = f"matter:{first['id']}"
    second_scope = f"matter:{second['id']}"
    bind_conversation("owner", second["id"])
    record_turn(
        "owner", "assistant", "CURRENT", provider="Codex", model="gpt-current",
        context_key=second_scope, matter_id=second["id"], message_id="current",
    )
    record_turn(
        "owner", "assistant", "LATE", provider="Claude", model="old-model",
        context_key=first_scope, matter_id=first["id"], message_id="late",
    )

    runtime = db_module.get_db().execute(
        "SELECT provider, model FROM conversation_runtime WHERE conv_key='owner'"
    ).fetchone()
    assert tuple(runtime) == ("Codex", "gpt-current")


def test_reset_tracker_failure_defers_but_next_dispatch_rotates(tmp_path, monkeypatch):
    matter = create_matter("可恢复重置")
    bind_conversation("owner", matter["id"])
    tracker = tmp_path / "tracker.json"
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    manager = SessionManager(tracker, sessions)
    old_scope = context_snapshot("owner")["context_key"]
    old_sid, _ = manager.get_session("owner", old_scope)
    real_rotate = SessionManager.force_rotate

    def fail_rotate(self, *args, **kwargs):
        raise OSError("synthetic tracker outage")

    monkeypatch.setattr(SessionManager, "force_rotate", fail_rotate)
    result = apply_runtime_transition(
        conv_key="owner", context_key=old_scope, tracker_path=tracker,
        session_dir=sessions, jarvis_dir=tmp_path, reset=True,
    )
    assert result["deferred_rotation"] is True
    new_scope = result["context_key"]
    assert new_scope.endswith("@g1")

    monkeypatch.setattr(SessionManager, "force_rotate", real_rotate)
    new_sid, rotated = manager.get_session("owner", new_scope)
    assert rotated is True and new_sid != old_sid
    assert manager.get_state("owner")["context_key"] == new_scope


def test_codex_threads_are_scoped_per_logical_session(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(codex_fallback, "resolve_codex_bin", lambda *_: "/codex")
    monkeypatch.setattr(codex_fallback, "ensure_codex_authenticated", lambda *_: None)

    def invoke(**kwargs):
        calls.append(kwargs["thread_id"])
        return codex_fallback.CliResult(
            text="ok", thread_id=kwargs["thread_id"] or f"thread-{len(calls)}"
        )

    monkeypatch.setattr(codex_fallback, "invoke_codex", invoke)
    common = dict(
        content="continue", conv_key="owner", system_prompt="context",
        model="gpt-test", timeout=10, work_dir=tmp_path, binary="/codex",
    )
    codex_fallback.run_fallback(**common, context_key="matter:first")
    codex_fallback.run_fallback(**common, context_key="matter:second")
    codex_fallback.run_fallback(**common, context_key="matter:first")

    assert calls == ["", "", "thread-1"]
    assert codex_fallback.load_session("matter:first")["thread_id"] == "thread-1"
    assert codex_fallback.load_session("matter:second")["thread_id"] == "thread-2"


def test_session_manager_context_transition_prevents_previous_backfill(tmp_path):
    tracker = tmp_path / "active_sessions.json"
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    manager = SessionManager(tracker, sessions)
    first, _ = manager.get_session("owner", "matter:first")
    second, rotated = manager.get_session("owner", "matter:second")
    state = manager.get_state("owner")

    assert second != first and rotated is True
    assert state["previous_context_key"] == "matter:first"
    assert state["context_key"] == "matter:second"
    assert state["rotation_reason"] == "context"


def test_context_transition_outranks_oversized_previous_transcript(tmp_path):
    tracker = tmp_path / "active_sessions.json"
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    manager = SessionManager(tracker, sessions, max_size=4)
    old_sid, _ = manager.get_session("owner", "matter:first")
    (sessions / f"{old_sid}.jsonl").write_text("oversized", encoding="utf-8")

    _, rotated = manager.get_session("owner", "matter:second")
    state = manager.get_state("owner")

    assert rotated is True
    assert state["rotation_reason"] == "context"
    assert state["previous_context_key"] == "matter:first"
    assert state["context_key"] == "matter:second"


def test_shared_or_external_transport_ignores_stale_private_binding():
    matter = create_matter("私人事项", summary="PRIVATE_MATTER_SECRET")
    db = db_module.get_db()
    db.execute(
        """INSERT INTO matter_bindings
           (conv_key,matter_id,channel,destination_id,chat_type,thread_root_id,
            created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)""",
        ("oc_old_group", matter["id"], "lark", "oc_old_group", "group", "",
         "old", "old"),
    )
    db.commit()

    trusted = context_snapshot("oc_old_group")
    untrusted = context_snapshot("oc_old_group", allow_binding=False)

    assert trusted["matter_id"] == matter["id"]
    assert untrusted["matter_id"] == ""
    assert untrusted["context_key"] == "conversation:oc_old_group"
    with pytest.raises(ValueError, match="shared Lark"):
        bind_conversation(
            "oc_new_group", matter["id"], destination_id="oc_new_group",
            chat_type="group",
        )


def test_binding_transaction_rolls_back_when_link_write_fails(monkeypatch):
    matter = create_matter("原子绑定")
    real_db = db_module.get_db()

    class FailingLinkDB:
        def execute(self, sql, params=()):
            if "INSERT INTO matter_links" in sql:
                raise sqlite3.DatabaseError("synthetic link failure")
            return real_db.execute(sql, params)

        def commit(self):
            return real_db.commit()

        def rollback(self):
            return real_db.rollback()

    monkeypatch.setattr("core.matter_bridge._db", lambda: FailingLinkDB())
    with pytest.raises(sqlite3.DatabaseError, match="synthetic link"):
        bind_conversation("owner", matter["id"])
    row = real_db.execute(
        "SELECT 1 FROM matter_bindings WHERE conv_key='owner'").fetchone()
    assert row is None


def test_schema_migrates_legacy_provider_state_to_unbound_context(tmp_path):
    path = tmp_path / "jarvis.db"
    with sqlite3.connect(path) as db:
        db.execute("""CREATE TABLE _migrations (
            version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)""")
        for version, sql in enumerate(db_module.MIGRATIONS):
            db.executescript(sql)
            db.execute(
                "INSERT INTO _migrations(version, applied_at) VALUES (?, 'old')",
                (version,),
            )
        db.execute(
        """INSERT INTO conversation_turns
           (conv_key, role, text, message_id, provider, model, session_id, created_at)
           VALUES ('legacy', 'user', 'old', '', '', '', '', '2026-08-13')"""
        )
        db.execute(
            """INSERT INTO codex_conversation_sessions
               (conv_key, thread_id, model, work_dir, updated_at)
               VALUES ('legacy', 'thread-old', 'gpt', '/tmp', '2026-08-13')"""
        )
        db.commit()

    db = db_module.get_db()
    row = db.execute(
        "SELECT context_key, matter_id FROM conversation_turns WHERE conv_key='legacy'"
    ).fetchone()
    assert tuple(row) == ("conversation:legacy", "")
    assert codex_fallback.load_session("conversation:legacy")["thread_id"] == "thread-old"
    assert codex_fallback.load_session("legacy")["thread_id"] == "thread-old"


def test_schema_backfill_retries_after_columns_already_exist(tmp_path):
    path = tmp_path / "jarvis.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE _migrations (version INTEGER PRIMARY KEY, applied_at TEXT)")
        for version, sql in enumerate(db_module.MIGRATIONS):
            db.executescript(sql)
            db.execute("INSERT INTO _migrations VALUES (?, 'old')", (version,))
        # Simulate a crash after additive columns committed but before data
        # repair.  Restart must not treat column presence as backfill evidence.
        db.execute("ALTER TABLE conversation_turns ADD COLUMN context_key TEXT NOT NULL DEFAULT ''")
        db.execute("ALTER TABLE conversation_turns ADD COLUMN matter_id TEXT NOT NULL DEFAULT ''")
        db.execute(
            """INSERT INTO conversation_turns
               (conv_key,role,text,created_at) VALUES ('crashed','user','old','old')""")
        db.execute(
            """INSERT INTO codex_conversation_sessions
               VALUES ('crashed','thread-old','gpt','/tmp','old')""")
        db.commit()

    db = db_module.get_db()
    assert db.execute(
        "SELECT context_key FROM conversation_turns WHERE conv_key='crashed'"
    ).fetchone()[0] == "conversation:crashed"
    assert db.execute(
        "SELECT thread_id FROM codex_conversation_sessions "
        "WHERE conv_key='conversation:crashed'"
    ).fetchone()[0] == "thread-old"
