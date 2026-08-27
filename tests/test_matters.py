"""Matter kernel tests against an isolated shared SQLite database."""

from __future__ import annotations

import pytest

import core.db as db_module
from core.matter_context import build_context_bundle
from core.matters import (
    add_event,
    create_matter,
    find_by_entity,
    get_matter,
    link_entity,
    list_matters,
    unlink_entity,
    update_matter,
)


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "matters.db")
    if db_module._connection is not None:
        db_module._connection.close()
    db_module._connection = None
    yield
    if db_module._connection is not None:
        db_module._connection.close()
    db_module._connection = None


def test_create_list_and_get_matter():
    created = create_matter(
        "统一 Jarvis 入口",
        summary="把多个入口收束到同一个事项",
        next_action="完成第一阶段",
        kind="project",
        priority=9,
    )

    assert created["id"].startswith("mat_")
    assert created["status"] == "active"
    assert created["links"] == []
    assert created["events"][0]["event_type"] == "matter_created"
    assert list_matters(status="active")[0]["id"] == created["id"]
    assert get_matter(created["id"])["title"] == "统一 Jarvis 入口"


def test_close_and_reopen_manage_closed_at_and_audit_trail():
    matter = create_matter("完成闭环")
    closed = update_matter(
        matter["id"], status="done", outcome="第一阶段上线", actor="test")

    assert closed["closed_at"]
    assert closed["outcome"] == "第一阶段上线"
    assert closed["events"][0]["event_type"] == "matter_updated"
    assert closed["events"][0]["summary"] == "更新了状态、完成结果、完成时间"
    assert closed["events"][0]["payload"]["status"]["to"] == "done"

    reopened = update_matter(matter["id"], status="active")
    assert reopened["closed_at"] is None
    assert reopened["status"] == "active"


def test_add_event_updates_timeline():
    matter = create_matter("保留时间线")
    event = add_event(
        matter["id"], "handoff_created", "从飞书交给 Codex",
        actor="lark", payload={"provider": "codex"},
    )

    assert event["payload"] == {"provider": "codex"}
    loaded = get_matter(matter["id"])
    assert loaded["events"][0]["summary"] == "从飞书交给 Codex"


def test_link_is_idempotent_and_preserves_metadata():
    matter = create_matter("绑定会话")
    first = link_entity(
        matter["id"], "session", "session-1", provider="codex",
        title="初始标题", metadata={"workspace": "/tmp/project"},
    )
    second = link_entity(
        matter["id"], "session", "session-1", provider="codex",
        title="更新标题",
    )

    assert first["id"] == second["id"]
    assert second["title"] == "更新标题"
    assert second["metadata"] == {"workspace": "/tmp/project"}
    assert len(get_matter(matter["id"])["links"]) == 1
    assert find_by_entity("session", "session-1", "codex")["id"] == matter["id"]


def test_context_bundle_carries_bounded_git_and_github_evidence():
    matter = create_matter("发布代码改动")
    link_entity(
        matter["id"], "artifact", "commit:abc123", provider="git",
        title="实现提交", metadata={"sha256": "a" * 64, "private_note": "drop"},
    )
    link_entity(
        matter["id"], "artifact", "pull:129", provider="github",
        title="发布 PR", metadata={"status": "merged", "review_body": "drop"},
    )

    artifacts = build_context_bundle(matter["id"])["artifacts"]
    by_provider = {item["provider"]: item for item in artifacts}

    assert set(by_provider) == {"git", "github"}
    assert by_provider["git"]["id"] == "commit:abc123"
    assert by_provider["github"]["id"] == "pull:129"
    assert by_provider["git"]["metadata"] == {"sha256": "a" * 64}
    assert by_provider["github"]["metadata"] == {"status": "merged"}


def test_link_requires_explicit_move_between_matters():
    first = create_matter("第一件事")
    second = create_matter("第二件事")
    link = link_entity(
        first["id"], "session", "shared-session", provider="claude",
        metadata={"model": "claude-test"},
    )

    with pytest.raises(ValueError, match="already linked"):
        link_entity(second["id"], "session", "shared-session", provider="claude")

    moved = link_entity(
        second["id"], "session", "shared-session", provider="claude", move=True)
    assert moved["id"] == link["id"]
    assert moved["matter_id"] == second["id"]
    assert moved["metadata"] == {"model": "claude-test"}
    assert get_matter(first["id"])["links"] == []


def test_unlink_and_input_validation():
    matter = create_matter("校验")
    link = link_entity(matter["id"], "artifact", "/tmp/result.md", provider="file")

    assert unlink_entity(matter["id"], link["id"])
    assert not unlink_entity(matter["id"], link["id"])
    assert get_matter(matter["id"])["links"] == []

    with pytest.raises(ValueError, match="title"):
        create_matter(" ")
    with pytest.raises(ValueError, match="priority"):
        create_matter("bad priority", priority=11)
    with pytest.raises(ValueError, match="entity type"):
        link_entity(matter["id"], "unknown", "x")
