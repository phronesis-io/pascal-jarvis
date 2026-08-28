"""Owner-triggered Codex wake preparation and fail-closed fallback tests."""

from __future__ import annotations

import copy

import pytest

import core.db as db_module
from core.codex_app_server import CodexAppServerError
from core.codex_wake import (
    audit_codex_wakes,
    create_codex_wake_task,
    prepare_codex_wake,
)
from core.matter_runs import list_runs
from core.matters import (
    add_event,
    create_matter,
    get_matter,
    link_entity,
    update_matter,
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


class FakeClient:
    threads: dict[str, dict] = {}
    calls: list[tuple[str, dict]] = []
    next_thread = 1
    fail_methods: set[str] = set()
    user_agent = "Codex Desktop/fake"

    @classmethod
    def reset(cls):
        cls.threads = {}
        cls.calls = []
        cls.next_thread = 1
        cls.fail_methods = set()

    def __init__(self, **_kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def request(self, method: str, params: dict):
        type(self).calls.append((method, copy.deepcopy(params)))
        if method in type(self).fail_methods:
            raise CodexAppServerError(f"failed {method}")
        if method == "project/list":
            return {"data": []}
        if method == "thread/start":
            thread_id = f"thread-{type(self).next_thread}"
            type(self).next_thread += 1
            thread = {
                "id": thread_id,
                "name": None,
                "cwd": params["cwd"],
                "turns": [],
            }
            type(self).threads[thread_id] = thread
            return {"thread": copy.deepcopy(thread)}
        if method == "thread/name/set":
            type(self).threads[params["threadId"]]["name"] = params["name"]
            return None
        if method == "thread/read":
            thread = type(self).threads.get(params["threadId"])
            if thread is None:
                raise CodexAppServerError("missing")
            return {"thread": copy.deepcopy(thread)}
        if method == "thread/delete":
            type(self).threads.pop(params["threadId"], None)
            return {}
        raise AssertionError(f"unexpected method: {method}")


@pytest.fixture(autouse=True)
def reset_fake_client():
    FakeClient.reset()


def test_wake_creates_verified_empty_task_without_acquiring_run(tmp_path):
    matter = create_matter("继续白皮书", next_action="整理三条主张")

    result = create_codex_wake_task(
        matter["id"],
        workspace=tmp_path,
        source_ref="om_message_1",
        client_factory=FakeClient,
        now_epoch=lambda: 123.0,
    )

    assert result["status"] == "prepared"
    assert result["executed"] is False
    assert result["thread_id"] == "thread-1"
    assert result["task_name"] == "继续：继续白皮书"
    assert result["wake_receipt"]["matter_lease_started"] is False
    assert list_runs(matter_id=matter["id"]) == []
    stored = get_matter(matter["id"])
    link = next(item for item in stored["links"] if item["entity_id"] == "thread-1")
    assert link["metadata"]["source_ref"] == "om_message_1"
    assert link["metadata"]["turn_count"] == 0
    start = next(params for method, params in FakeClient.calls if method == "thread/start")
    assert matter["id"] in start["developerInstructions"]
    assert "继续白皮书" not in start["developerInstructions"]
    assert audit_codex_wakes(now_epoch=500)["healthy"] is True


def test_repeated_wake_reuses_same_unused_task(tmp_path):
    matter = create_matter("重复点击不重复开任务")

    first = create_codex_wake_task(
        matter["id"], workspace=tmp_path, client_factory=FakeClient,
    )
    second = create_codex_wake_task(
        matter["id"], workspace=tmp_path, client_factory=FakeClient,
    )

    assert first["thread_id"] == second["thread_id"]
    assert second["status"] == "reused"
    assert [method for method, _ in FakeClient.calls].count("thread/start") == 1


def test_used_task_gets_a_new_handoff_instead_of_reuse(tmp_path):
    matter = create_matter("下一轮要新任务")
    first = create_codex_wake_task(
        matter["id"], workspace=tmp_path, client_factory=FakeClient,
    )
    FakeClient.threads[first["thread_id"]]["turns"] = [{"id": "turn-1"}]

    second = create_codex_wake_task(
        matter["id"], workspace=tmp_path, client_factory=FakeClient,
    )

    assert second["status"] == "prepared"
    assert second["thread_id"] == "thread-2"


def test_existing_task_read_failure_does_not_create_a_duplicate(tmp_path):
    matter = create_matter("读不到时不重复创建")
    first = create_codex_wake_task(
        matter["id"], workspace=tmp_path, client_factory=FakeClient,
    )
    FakeClient.fail_methods = {"thread/read"}

    second = prepare_codex_wake(
        matter["id"], workspace=tmp_path, client_factory=FakeClient,
    )

    assert first["thread_id"] == "thread-1"
    assert second["status"] == "manual_fallback"
    assert [method for method, _ in FakeClient.calls].count("thread/start") == 1


def test_failed_verification_deletes_orphan_and_returns_manual_phrase(tmp_path):
    matter = create_matter("失败时不假装成功")
    FakeClient.fail_methods = {"thread/name/set"}

    result = prepare_codex_wake(
        matter["id"], workspace=tmp_path, client_factory=FakeClient,
    )

    assert result["status"] == "manual_fallback"
    assert result["thread_id"] == ""
    assert result["wake_receipt"]["thread_verified"] is False
    assert FakeClient.threads == {}
    assert ("thread/delete", {"threadId": "thread-1"}) in FakeClient.calls
    events = get_matter(matter["id"])["events"]
    failed = next(item for item in events if item["event_type"] == "codex_wake_failed")
    assert failed["payload"]["error_type"] == "CodexWakeError"


def test_name_readback_mismatch_deletes_unverified_task(tmp_path, monkeypatch):
    matter = create_matter("名称必须读回核验")

    original_request = FakeClient.request

    def ignore_name_set(self, method, params):
        if method == "thread/name/set":
            type(self).calls.append((method, copy.deepcopy(params)))
            return None
        return original_request(self, method, params)

    monkeypatch.setattr(FakeClient, "request", ignore_name_set)

    result = prepare_codex_wake(
        matter["id"], workspace=tmp_path, client_factory=FakeClient,
    )

    assert result["status"] == "manual_fallback"
    assert FakeClient.threads == {}
    assert ("thread/delete", {"threadId": "thread-1"}) in FakeClient.calls


def test_failed_orphan_cleanup_is_explicit_in_receipt_and_audit(tmp_path):
    matter = create_matter("孤儿清理必须可见")
    FakeClient.fail_methods = {"thread/name/set", "thread/delete"}

    result = prepare_codex_wake(
        matter["id"], workspace=tmp_path, client_factory=FakeClient,
    )

    assert result["status"] == "manual_fallback"
    assert result["wake_receipt"]["orphan_cleanup_required"] is True
    failed = next(
        item for item in get_matter(matter["id"])["events"]
        if item["event_type"] == "codex_wake_failed"
    )
    assert failed["payload"]["orphan_cleanup_required"] is True
    assert failed["payload"]["orphan_thread_id"] == "thread-1"
    issue = audit_codex_wakes(now_epoch=500)["issues"][0]
    assert issue["code"] == "orphan_cleanup_required"
    assert issue["thread_id"] == "thread-1"
    link_entity(
        matter["id"], "session", "thread-1", provider="codex",
        metadata={
            "source": "codex_wake",
            "wake_id": failed["payload"]["wake_id"],
        },
    )
    assert audit_codex_wakes(now_epoch=500)["healthy"] is True


def test_explicit_workspace_change_does_not_reuse_wrong_task(tmp_path):
    first_workspace = tmp_path / "first"
    second_workspace = tmp_path / "second"
    first_workspace.mkdir()
    second_workspace.mkdir()
    matter = create_matter("切换工作区")

    first = create_codex_wake_task(
        matter["id"], workspace=first_workspace, client_factory=FakeClient,
    )
    second = create_codex_wake_task(
        matter["id"], workspace=second_workspace, client_factory=FakeClient,
    )

    assert first["thread_id"] == "thread-1"
    assert second["thread_id"] == "thread-2"
    assert second["workspace"] == str(second_workspace)


def test_matter_closing_while_waiting_for_lock_blocks_creation(
    tmp_path, monkeypatch,
):
    from core import codex_wake

    matter = create_matter("锁内重新核验状态")
    active = get_matter(matter["id"])
    closed = {**active, "status": "done"}
    states = iter((active, closed))
    monkeypatch.setattr(codex_wake, "get_matter", lambda _matter_id: next(states))

    with pytest.raises(codex_wake.CodexWakeError):
        create_codex_wake_task(
            matter["id"], workspace=tmp_path, client_factory=FakeClient,
        )

    assert not any(method == "thread/start" for method, _ in FakeClient.calls)


def test_workspace_prefers_explicit_then_latest_session_metadata(tmp_path):
    prior = tmp_path / "prior"
    explicit = tmp_path / "explicit"
    prior.mkdir()
    explicit.mkdir()
    matter = create_matter("选择工作区")
    link_entity(
        matter["id"], "session", "old", provider="codex",
        metadata={"workspace": str(prior)},
    )

    result = create_codex_wake_task(
        matter["id"], workspace=explicit, client_factory=FakeClient,
    )

    assert result["workspace"] == str(explicit)


def test_closed_matter_never_creates_task(tmp_path):
    matter = create_matter("已经结束")
    update_matter(matter["id"], status="done", outcome="完成", force=True)

    result = prepare_codex_wake(
        matter["id"], workspace=tmp_path, client_factory=FakeClient,
    )

    assert result["status"] == "manual_fallback"
    assert not any(method == "thread/start" for method, _ in FakeClient.calls)


def test_audit_finds_stale_requested_and_unlinked_external_task():
    matter = create_matter("审计半成品")
    add_event(
        matter["id"], "codex_wake_requested", actor="owner",
        payload={"wake_id": "wake_pending", "requested_epoch": 100.0},
    )
    add_event(
        matter["id"], "codex_wake_requested", actor="owner",
        payload={"wake_id": "wake_external", "requested_epoch": 100.0},
    )
    add_event(
        matter["id"], "codex_wake_task_created", actor="owner",
        payload={
            "wake_id": "wake_external", "thread_id": "thread-orphan",
            "created_epoch": 101.0,
        },
    )

    report = audit_codex_wakes(now_epoch=500.0, stale_seconds=300)

    assert report["healthy"] is False
    by_code = {item["code"]: item for item in report["issues"]}
    assert by_code["wake_request_unfinished"]["wake_id"] == "wake_pending"
    assert by_code["external_task_unlinked"]["thread_id"] == "thread-orphan"


def test_audit_does_not_flag_a_recent_inflight_request():
    matter = create_matter("刚开始的交接")
    add_event(
        matter["id"], "codex_wake_requested", actor="owner",
        payload={"wake_id": "wake_recent", "requested_epoch": 490.0},
    )

    report = audit_codex_wakes(now_epoch=500.0, stale_seconds=300)

    assert report["healthy"] is True
    assert report["issues"] == []
