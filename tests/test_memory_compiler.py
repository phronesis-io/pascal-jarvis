"""Cross-product Memory Compiler lifecycle, privacy, and replay tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import core.db as db_module
from core.cross_session_index import index_sessions
from core.matter_bridge import record_turn
from core.matter_context import build_context_bundle, render_context_markdown
from core.matters import create_matter, link_entity
from core.memory_compiler import (
    MemoryCompilerError,
    apply_compile_result,
    compiled_context,
    compiler_status,
    open_conflicts,
    prepare_batch,
    resolve_claim,
    search_compiled_memory,
)


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


def _write(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def _sources(tmp_path: Path, *, linked: bool = True):
    matter = create_matter("跨产品记忆", summary="同一个决定跨产品继续")
    claude = tmp_path / "claude" / "project" / "human.jsonl"
    codex = tmp_path / "codex" / "2026" / "08" / "human.jsonl"
    _write(claude, [
        {
            "type": "user", "sessionId": "claude-human", "cwd": "/work/alpha",
            "timestamp": "2026-08-13T10:00:00Z",
            "message": {"content": "我决定默认在 Codex 开始长任务"},
        },
        {
            "type": "assistant", "sessionId": "claude-human", "cwd": "/work/alpha",
            "timestamp": "2026-08-13T10:01:00Z",
            "message": {"content": "所有上线工作已经完成"},
        },
    ])
    _write(codex, [
        {"type": "session_meta", "payload": {
            "id": "codex-human", "cwd": "/work/beta", "source": "vscode",
            "thread_source": "user", "originator": "Codex Desktop",
        }},
        {"type": "event_msg", "timestamp": "2026-08-13T11:00:00Z",
         "payload": {"type": "user_message", "message": "记忆必须保留原文来源"}},
    ])
    index_db = tmp_path / "cross-session.db"
    index_sessions(
        db_path=index_db,
        claude_root=claude.parent.parent,
        codex_root=codex.parents[2],
        tracker_path=tmp_path / "missing.json",
        batch_size=20,
    )
    if linked:
        link_entity(
            matter["id"], "session", "codex-human", provider="codex",
            title="Codex task",
        )
    record_turn(
        "owner", "user", "飞书只负责紧急唤醒", message_id="om_owner",
        matter_id=matter["id"], memory_eligible=True,
    )
    record_turn(
        "group", "user", "群聊里的私人推断绝不能进入记忆", message_id="om_group",
        memory_eligible=False,
    )
    return matter, index_db


def _claim(source: dict, *, kind: str, key: str, content: str,
           quote: str | None = None) -> dict:
    return {
        "source_ref": source["source_ref"],
        "quote": quote or source["text"],
        "kind": kind,
        "claim_key": key,
        "content": content,
        "matter_id": source.get("matter_id", ""),
    }


def _apply_all(batch: dict, claims: list[dict]) -> dict:
    claimed = {item["source_ref"] for item in claims}
    ignored = [
        item["source_ref"] for item in batch["sources"]
        if item["source_ref"] not in claimed
    ]
    return apply_compile_result({
        "schema": "jarvis.memory-candidates.v1",
        "batch_id": batch["batch_id"],
        "claims": claims,
        "ignored_source_refs": ignored,
    }, now=100.0)


def test_codex_claude_and_owner_lark_compile_without_group_leak(tmp_path):
    matter, index_db = _sources(tmp_path)
    batch = prepare_batch(index_db=index_db, batch_size=20, now=10.0)
    assert batch is not None
    texts = {item["text"] for item in batch["sources"]}
    assert "记忆必须保留原文来源" in texts
    assert "我决定默认在 Codex 开始长任务" in texts
    assert "飞书只负责紧急唤醒" in texts
    assert all("群聊里的私人推断" not in text for text in texts)

    by_text = {item["text"]: item for item in batch["sources"]}
    receipt = _apply_all(batch, [
        _claim(
            by_text["记忆必须保留原文来源"], kind="constraint",
            key="memory.source_traceability", content="记忆必须保留原文来源",
        ),
        _claim(
            by_text["我决定默认在 Codex 开始长任务"], kind="decision",
            key="work.long_task_frontstage", content="长任务默认从 Codex 开始",
        ),
        _claim(
            by_text["所有上线工作已经完成"], kind="fact",
            key="release.all_work_complete", content="所有上线工作已经完成",
        ),
        _claim(
            by_text["飞书只负责紧急唤醒"], kind="decision",
            key="lark.primary_role", content="飞书只负责紧急唤醒",
        ),
    ])

    assert receipt["source_count"] == 4
    status = compiler_status()
    assert status["claims"]["active"] == 3
    assert status["claims"]["candidate"] == 1
    matter_text = compiled_context(matter_id=matter["id"])
    assert "记忆必须保留原文来源" in matter_text
    assert "飞书只负责紧急唤醒" in matter_text
    assert "所有上线工作已经完成" not in matter_text
    assert "source `session_turn:" in matter_text

    packet = build_context_bundle(matter["id"])
    rendered = render_context_markdown(packet)
    assert packet["compiled_memory"]
    assert "## Compiled memory" in rendered
    assert "所有上线工作已经完成" not in rendered


def test_owner_lark_assistant_turn_is_candidate_not_active():
    record_turn(
        "owner", "assistant", "日历已经全部更新完成",
        message_id="om_lark_assistant", provider="Claude primary",
        memory_eligible=True,
    )
    batch = prepare_batch(batch_size=20)
    source = next(
        item for item in batch["sources"]
        if item["text"] == "日历已经全部更新完成"
    )
    _apply_all(batch, [_claim(
        source, kind="fact", key="calendar.all_updated",
        content="日历已经全部更新完成",
    )])

    result = search_compiled_memory(include_candidates=True)
    claim = next(
        item for item in result["claims"]
        if item["content"] == "日历已经全部更新完成"
    )
    assert claim["status"] == "candidate"
    assert "日历已经全部更新完成" not in compiled_context()


@pytest.mark.parametrize(
    "text",
    ["搞吧", "写进 blog 吧", "把这个做完吧", "同步到飞书吧", "go ahead"],
)
def test_context_dependent_owner_acknowledgement_is_candidate_only(text):
    batch, source = _one_lark_batch(text)

    assert source["activation_policy"] == "owner_context_candidate"
    _apply_all(batch, [_claim(
        source, kind="decision", key="blog06.contextual_approval",
        content="Pascal approved a specific Blog 06 rewrite",
    )])

    claim = search_compiled_memory(include_candidates=True)["claims"][0]
    assert claim["status"] == "candidate"
    assert claim["authority"] == "assistant_candidate"
    assert compiled_context() == ""


@pytest.mark.parametrize(
    ("text", "kind", "key"),
    [
        ("不要催我", "constraint", "attention.no_nudging"),
        ("发布 PR #130 吧", "decision", "release.pr130"),
        ("修复登录超时吧", "todo", "auth.fix_login_timeout"),
    ],
)
def test_short_self_contained_owner_statement_still_activates(
    text, kind, key,
):
    batch, source = _one_lark_batch(text)

    assert source["activation_policy"] == "owner_asserted"
    _apply_all(batch, [_claim(
        source, kind=kind, key=key, content=text,
    )])

    claim = search_compiled_memory()["claims"][0]
    assert claim["status"] == "active"
    assert claim["authority"] == "owner_asserted"
    assert text in compiled_context()


def test_contextual_subquote_cannot_borrow_authority_from_full_owner_turn():
    batch, source = _one_lark_batch("好的，主入口改为 Codex")

    assert source["activation_policy"] == "owner_asserted"
    _apply_all(batch, [
        _claim(
            source, kind="decision", key="product.ambiguous_ack",
            quote="好的", content="Pascal approved the proposed frontstage",
        ),
        _claim(
            source, kind="decision", key="product.primary_frontstage",
            quote="主入口改为 Codex", content="主入口改为 Codex",
        ),
    ])

    claims = {
        item["claim_key"]: item
        for item in search_compiled_memory(include_candidates=True)["claims"]
    }
    assert claims["product.ambiguous_ack"]["status"] == "candidate"
    assert claims["product.primary_frontstage"]["status"] == "active"
    assert "Pascal approved the proposed frontstage" not in compiled_context()
    assert "主入口改为 Codex" in compiled_context()


def test_owner_question_and_its_assertive_subquote_remain_candidates():
    batch, source = _one_lark_batch("版本已经上线了吗？")

    assert source["activation_policy"] == "owner_context_candidate"
    _apply_all(batch, [
        _claim(
            source, kind="fact", key="release.question_whole",
            content="版本已经上线", quote="版本已经上线了吗？",
        ),
        _claim(
            source, kind="fact", key="release.question_subquote",
            content="版本已经上线", quote="版本已经上线",
        ),
    ])

    claims = search_compiled_memory(include_candidates=True)["claims"]
    assert len(claims) == 2
    assert {item["status"] for item in claims} == {"candidate"}
    assert compiled_context() == ""


def test_explicit_owner_statement_promotes_matching_contextual_candidate():
    prior, prior_source = _one_lark_batch("主入口先用飞书")
    _apply_all(prior, [_claim(
        prior_source, kind="decision", key="product.primary_frontstage",
        content="主入口先用飞书",
    )])
    contextual, contextual_source = _one_lark_batch("搞吧")
    _apply_all(contextual, [_claim(
        contextual_source, kind="decision", key="product.primary_frontstage",
        content="主入口改为 Codex",
    )])

    explicit, explicit_source = _one_lark_batch("主入口改为 Codex")
    receipt = _apply_all(explicit, [_claim(
        explicit_source, kind="decision", key="product.primary_frontstage",
        content="主入口改为 Codex",
    )])

    assert receipt["outcomes"][0]["outcome"] == "promoted_superseded_previous"
    claims = search_compiled_memory(include_candidates=True)["claims"]
    current = next(item for item in claims if item["content"] == "主入口改为 Codex")
    assert current["status"] == "active"
    assert current["authority"] == "owner_asserted"
    assert len(current["source_refs"]) == 2
    assert "主入口改为 Codex" in compiled_context()
    assert "主入口先用飞书" not in compiled_context()


def test_prepare_repairs_legacy_ack_claim_and_restores_displaced_decision():
    first, first_source = _one_lark_batch("主入口先用飞书")
    _apply_all(first, [_claim(
        first_source, kind="decision", key="product.primary_frontstage",
        content="主入口先用飞书",
    )])
    prior = _db_row(
        "SELECT id FROM memory_claims WHERE content='主入口先用飞书'"
    )

    ack_batch, ack_source = _one_lark_batch("搞吧")
    _apply_all(ack_batch, [])
    connection = db_module.get_db()
    connection.execute(
        """INSERT INTO memory_claims
           (id,kind,claim_key,content,normalized_content,status,authority,
            created_epoch,updated_epoch)
           VALUES ('legacy_ack','decision','product.primary_frontstage',
                   '主入口改为 Codex','主入口改为 codex','active',
                   'owner_asserted',100,100)"""
    )
    connection.execute(
        """INSERT INTO memory_claim_sources(claim_id,source_ref,source_quote)
           VALUES ('legacy_ack',?,'搞吧')""",
        (ack_source["source_ref"],),
    )
    connection.execute(
        """UPDATE memory_claims
              SET status='superseded',superseded_by='legacy_ack'
            WHERE id=?""",
        (prior["id"],),
    )
    connection.commit()

    assert prepare_batch(batch_size=20, now=200.0) is None

    repaired = _db_row(
        "SELECT status,authority FROM memory_claims WHERE id='legacy_ack'"
    )
    assert repaired["status"] == "candidate"
    assert repaired["authority"] == "assistant_candidate"
    restored = connection.execute(
        "SELECT status,superseded_by FROM memory_claims WHERE id=?",
        (prior["id"],),
    ).fetchone()
    assert restored["status"] == "active"
    assert restored["superseded_by"] is None


def test_prepare_repairs_legacy_ack_conflict_and_restores_grounded_fact():
    first, first_source = _one_lark_batch("版本还没有上线")
    _apply_all(first, [_claim(
        first_source, kind="fact", key="release.current_status",
        content="版本还没有上线",
    )])
    connection = db_module.get_db()
    prior = connection.execute(
        "SELECT id FROM memory_claims WHERE content='版本还没有上线'"
    ).fetchone()
    ack_batch, ack_source = _one_lark_batch("搞吧")
    _apply_all(ack_batch, [])
    connection.execute(
        """INSERT INTO memory_claims
           (id,kind,claim_key,content,normalized_content,status,authority,
            created_epoch,updated_epoch)
           VALUES ('legacy_conflict','fact','release.current_status',
                   '版本已经上线','版本已经上线','conflicted',
                   'owner_asserted',100,100)"""
    )
    connection.execute(
        """INSERT INTO memory_claim_sources(claim_id,source_ref,source_quote)
           VALUES ('legacy_conflict',?,'搞吧')""",
        (ack_source["source_ref"],),
    )
    connection.execute(
        "UPDATE memory_claims SET status='conflicted' WHERE id=?",
        (prior["id"],),
    )
    connection.execute(
        """INSERT INTO memory_conflicts
           (id,matter_scope,claim_key,prior_claim_id,incoming_claim_id,
            status,created_epoch)
           VALUES ('legacy_conflict_row','','release.current_status',?,
                   'legacy_conflict','open',100)""",
        (prior["id"],),
    )
    connection.commit()

    assert prepare_batch(batch_size=20, now=200.0) is None

    assert connection.execute(
        "SELECT status FROM memory_claims WHERE id='legacy_conflict'"
    ).fetchone()["status"] == "candidate"
    assert connection.execute(
        "SELECT status FROM memory_claims WHERE id=?", (prior["id"],),
    ).fetchone()["status"] == "active"
    conflict = connection.execute(
        "SELECT status,resolution FROM memory_conflicts WHERE id='legacy_conflict_row'"
    ).fetchone()
    assert conflict["status"] == "resolved"
    assert conflict["resolution"] == "context_dependent_owner_quote"


def test_model_cannot_fabricate_a_quote_or_omit_a_source(tmp_path):
    _, index_db = _sources(tmp_path)
    batch = prepare_batch(index_db=index_db, batch_size=20)
    source = batch["sources"][0]
    with pytest.raises(MemoryCompilerError, match="not grounded"):
        apply_compile_result({
            "schema": "jarvis.memory-candidates.v1",
            "batch_id": batch["batch_id"],
            "claims": [_claim(
                source, kind="fact", key="fabricated", content="假的",
                quote="原文中不存在的句子",
            )],
            "ignored_source_refs": [
                item["source_ref"] for item in batch["sources"][1:]
            ],
        })
    assert prepare_batch(index_db=index_db)["batch_id"] == batch["batch_id"]

    with pytest.raises(MemoryCompilerError, match="omitted sources"):
        apply_compile_result({
            "schema": "jarvis.memory-candidates.v1",
            "batch_id": batch["batch_id"],
            "claims": [],
            "ignored_source_refs": [],
        })
    assert compiler_status()["pending_batches"] == 1


def _one_lark_batch(text: str, *, matter_id: str = "") -> tuple[dict, dict]:
    record_turn(
        "owner", "user", text, message_id=f"om_{abs(hash(text))}",
        matter_id=matter_id, memory_eligible=True,
    )
    batch = prepare_batch(batch_size=20)
    source = next(item for item in batch["sources"] if item["text"] == text)
    return batch, source


def test_new_owner_decision_supersedes_old_decision_without_stale_injection():
    matter = create_matter("入口决定")
    first, source = _one_lark_batch("主入口先用飞书", matter_id=matter["id"])
    _apply_all(first, [_claim(
        source, kind="decision", key="product.primary_frontstage",
        content="主入口先用飞书",
    )])
    second, source = _one_lark_batch("主入口改为 Codex", matter_id=matter["id"])
    _apply_all(second, [_claim(
        source, kind="decision", key="product.primary_frontstage",
        content="主入口改为 Codex",
    )])

    search = search_compiled_memory(matter_id=matter["id"])
    assert [item["content"] for item in search["claims"]] == ["主入口改为 Codex"]
    assert "主入口先用飞书" not in compiled_context(matter_id=matter["id"])
    row = _db_row(
        "SELECT status FROM memory_claims WHERE content='主入口先用飞书'"
    )
    assert row["status"] == "superseded"


def _db_row(sql: str):
    return db_module.get_db().execute(sql).fetchone()


def test_conflicting_facts_are_blocked_until_human_chooses():
    matter = create_matter("发布状态")
    first, source = _one_lark_batch("版本已经上线", matter_id=matter["id"])
    _apply_all(first, [_claim(
        source, kind="fact", key="release.current_status", content="版本已经上线",
    )])
    second, source = _one_lark_batch("版本还没有上线", matter_id=matter["id"])
    receipt = _apply_all(second, [_claim(
        source, kind="fact", key="release.current_status", content="版本还没有上线",
    )])

    assert receipt["needs_review"] is True
    assert len(open_conflicts(matter_id=matter["id"])) == 1
    assert "版本已经上线" not in compiled_context(matter_id=matter["id"])
    assert "版本还没有上线" not in compiled_context(matter_id=matter["id"])
    claims = search_compiled_memory(matter_id=matter["id"])["claims"]
    chosen = next(item for item in claims if item["content"] == "版本还没有上线")
    resolved = resolve_claim(
        chosen["id"], action="choose", reviewer="Pascal", now=300.0,
    )
    assert resolved["claim"]["status"] == "active"
    assert resolved["open_conflicts"] == []
    assert "版本还没有上线" in compiled_context(matter_id=matter["id"])


def test_compiled_search_returns_source_refs_but_not_raw_quotes():
    batch, source = _one_lark_batch("我偏好先给结论")
    _apply_all(batch, [_claim(
        source, kind="preference", key="communication.answer_order",
        content="回答先给结论",
    )])
    result = search_compiled_memory("回答")
    assert result["raw_transcripts_included"] is False
    assert result["claims"][0]["source_refs"] == [source["source_ref"]]
    assert "sources" not in result["claims"][0]
    assert "source_quote" not in json.dumps(result, ensure_ascii=False)


def test_batch_quota_keeps_session_and_lark_sources_moving(tmp_path):
    _, index_db = _sources(tmp_path)
    batch = prepare_batch(index_db=index_db, batch_size=2)
    kinds = [item["source_kind"] for item in batch["sources"]]
    assert kinds.count("session_turn") == 1
    assert kinds.count("lark_turn") == 1


def test_source_scan_converges_past_an_already_compiled_page(
    tmp_path, monkeypatch,
):
    import core.memory_compiler_sources as source_module

    _, index_db = _sources(tmp_path)
    monkeypatch.setattr(source_module, "SOURCE_SCAN_PAGE_SIZE", 2)
    first = prepare_batch(index_db=index_db, batch_size=2)
    _apply_all(first, [])

    second = prepare_batch(index_db=index_db, batch_size=2)
    assert second is not None
    first_refs = {item["source_ref"] for item in first["sources"]}
    assert all(item["source_ref"] not in first_refs for item in second["sources"])


def test_confirming_assistant_candidate_does_not_silently_override_fact():
    matter = create_matter("运行状态")
    owner_batch, owner_source = _one_lark_batch(
        "服务目前在线", matter_id=matter["id"],
    )
    _apply_all(owner_batch, [_claim(
        owner_source, kind="fact", key="runtime.online",
        content="服务目前在线",
    )])
    record_turn(
        "owner", "assistant", "服务目前离线", message_id="om_assistant_fact",
        matter_id=matter["id"], memory_eligible=True,
    )
    candidate_batch = prepare_batch(batch_size=20)
    candidate_source = next(
        item for item in candidate_batch["sources"]
        if item["text"] == "服务目前离线"
    )
    _apply_all(candidate_batch, [_claim(
        candidate_source, kind="fact", key="runtime.online",
        content="服务目前离线",
    )])
    candidate = next(
        item for item in search_compiled_memory(
            matter_id=matter["id"], include_candidates=True,
        )["claims"] if item["content"] == "服务目前离线"
    )

    reviewed = resolve_claim(
        candidate["id"], action="confirm", reviewer="Pascal", now=400.0,
    )
    assert reviewed["claim"]["status"] == "conflicted"
    assert len(reviewed["open_conflicts"]) == 1
    assert "服务目前在线" not in compiled_context(matter_id=matter["id"])
    assert "服务目前离线" not in compiled_context(matter_id=matter["id"])


def test_rejecting_one_conflicting_fact_restores_the_other():
    matter = create_matter("部署状态")
    first, first_source = _one_lark_batch(
        "部署还没完成", matter_id=matter["id"],
    )
    _apply_all(first, [_claim(
        first_source, kind="fact", key="deploy.complete",
        content="部署还没完成",
    )])
    second, second_source = _one_lark_batch(
        "部署已经完成", matter_id=matter["id"],
    )
    _apply_all(second, [_claim(
        second_source, kind="fact", key="deploy.complete",
        content="部署已经完成",
    )])
    rejected = next(
        item for item in search_compiled_memory(matter_id=matter["id"])["claims"]
        if item["content"] == "部署已经完成"
    )

    resolve_claim(rejected["id"], action="reject", reviewer="Pascal", now=500.0)
    assert "部署还没完成" in compiled_context(matter_id=matter["id"])
    assert "部署已经完成" not in compiled_context(matter_id=matter["id"])
    assert open_conflicts(matter_id=matter["id"]) == []


def test_context_packet_hard_limit_includes_memory_conflicts():
    matter = create_matter(
        "上下文边界",
        summary="很长的摘要" * 300,
        next_action="很长的下一步" * 300,
    )
    for index in range(8):
        first, source = _one_lark_batch(
            f"状态 {index} 是甲" + ("内容" * 80), matter_id=matter["id"],
        )
        _apply_all(first, [_claim(
            source, kind="fact", key=f"status.{index}", content=f"状态 {index} 是甲",
        )])
        second, source = _one_lark_batch(
            f"状态 {index} 是乙" + ("内容" * 80), matter_id=matter["id"],
        )
        _apply_all(second, [_claim(
            source, kind="fact", key=f"status.{index}", content=f"状态 {index} 是乙",
        )])

    packet = build_context_bundle(matter["id"], char_limit=1000)
    assert len(render_context_markdown(packet)) <= 1000
