import json
import subprocess

import pytest

from core.taskline_bridge import TasklineBridge, TasklineBridgeError
from core.delegations import DelegationStore


def _result(command, payload, returncode=0, stderr=""):
    return subprocess.CompletedProcess(
        command, returncode, json.dumps(payload), stderr
    )


def test_claim_checks_health_then_atomically_claims(tmp_path):
    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        if command == ["taskline", "status"]:
            return _result(command, {"healthy": True, "registered": True})
        return _result(command, {"id": "12345678-abcd", "title": "Fix bug"})

    result = TasklineBridge(root=tmp_path, runner=runner).claim_next(
        lease="3h", labels=["p0"]
    )
    assert result["available"] is True
    claim = calls[1][0]
    assert claim[:4] == ["taskline", "task", "next", "--project"]
    assert "--claim" in claim
    assert ["--label", "p0"] == claim[-2:]
    detail = DelegationStore(root=tmp_path).get(result["delegation_id"])
    assert detail["source"] == "taskline"
    assert detail["links"][0]["entity_type"] == "taskline_task"
    assert detail["expected_postcondition"] == {
        "runtime_ok": True,
        "components_ok": True,
    }
    assert detail["steps"][0]["kind"] == "runtime_deploy"


def test_claim_fails_when_workspace_is_not_registered(tmp_path):
    bridge = TasklineBridge(
        root=tmp_path,
        runner=lambda command, **kwargs: _result(
            command, {"healthy": True, "registered": False}
        ),
    )
    with pytest.raises(TasklineBridgeError, match="not registered"):
        bridge.claim_next()


def test_empty_claim_has_explicit_stop_reason(tmp_path):
    def runner(command, **kwargs):
        if command == ["taskline", "status"]:
            return _result(command, {"healthy": True, "registered": True})
        return _result(command, {})

    result = TasklineBridge(root=tmp_path, runner=runner).claim_next()
    assert result == {
        "available": False,
        "reason": "queue_empty_or_blocked",
    }


def test_worktree_uses_task_id_and_origin_main(tmp_path, monkeypatch):
    calls = []
    worktree_root = tmp_path / "worktrees"
    monkeypatch.setenv("JARVIS_WORKTREE_ROOT", str(worktree_root))

    def runner(command, **kwargs):
        calls.append(command)
        if command[:3] == ["git", "show-ref", "--verify"]:
            return subprocess.CompletedProcess(command, 1, "", "")
        if command[:3] == ["git", "worktree", "add"]:
            worktree_root.joinpath("12345678").mkdir(parents=True)
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[:3] == ["taskline", "task", "update"]:
            return _result(command, {"id": "12345678-abcd"})
        raise AssertionError(command)

    result = TasklineBridge(root=tmp_path, runner=runner).prepare_worktree(
        {"id": "12345678-abcd", "title": "Fix Delivery"},
    )
    assert result["branch"] == "agent/fix-delivery-12345678"
    add = next(command for command in calls if command[:3] == [
        "git", "worktree", "add"
    ])
    assert add[-1] == "origin/main"
    delegations = DelegationStore(root=tmp_path).list()
    links = DelegationStore(root=tmp_path).get(delegations[0]["id"])["links"]
    assert {link["entity_type"] for link in links} == {
        "taskline_task", "workspace", "git_branch",
    }


def test_execution_context_links_codex_session_and_job_without_transcript(
        tmp_path):
    bridge = TasklineBridge(root=tmp_path)
    detail = bridge.link_execution_context(
        {"id": "12345678-abcd", "title": "Fix Delivery"},
        provider="codex",
        session_id="session-1",
        job_id="job-1",
        workspace=str(tmp_path),
        branch="codex/fix",
    )
    links = {(row["entity_type"], row["entity_id"]) for row in detail["links"]}
    assert ("session", "codex:session-1") in links
    assert ("job", "job-1") in links
    assert all("transcript" not in value for _, value in links)


def test_unsafe_task_id_cannot_become_a_path(tmp_path):
    with pytest.raises(TasklineBridgeError, match="unsafe"):
        TasklineBridge(root=tmp_path).prepare_worktree(
            {"id": "../../secret", "title": "bad"}
        )


def test_existing_registered_worktree_repairs_missing_links(
    tmp_path, monkeypatch
):
    calls = []
    worktree_root = tmp_path / "worktrees"
    path = worktree_root / "12345678"
    path.mkdir(parents=True)
    monkeypatch.setenv("JARVIS_WORKTREE_ROOT", str(worktree_root))
    branch = "agent/fix-delivery-12345678"

    def runner(command, **kwargs):
        calls.append(command)
        if command[:4] == ["git", "worktree", "list", "--porcelain"]:
            return subprocess.CompletedProcess(
                command,
                0,
                f"worktree {path}\nHEAD {'a' * 40}\n"
                f"branch refs/heads/{branch}\n",
                "",
            )
        if command[:3] == ["taskline", "task", "update"]:
            return _result(command, {"id": "12345678-abcd"})
        raise AssertionError(command)

    result = TasklineBridge(root=tmp_path, runner=runner).prepare_worktree(
        {"id": "12345678-abcd", "title": "Fix Delivery"},
    )

    assert result["created"] == "false"
    detail = DelegationStore(root=tmp_path).list()[0]
    links = DelegationStore(root=tmp_path).get(detail["id"])["links"]
    assert {row["entity_type"] for row in links} == {
        "taskline_task",
        "workspace",
        "git_branch",
    }
    assert not any(command[:3] == ["git", "worktree", "add"] for command in calls)


def test_unregistered_existing_directory_is_rejected(tmp_path, monkeypatch):
    worktree_root = tmp_path / "worktrees"
    (worktree_root / "12345678").mkdir(parents=True)
    monkeypatch.setenv("JARVIS_WORKTREE_ROOT", str(worktree_root))

    def runner(command, **kwargs):
        return subprocess.CompletedProcess(
            command,
            0,
            f"worktree {tmp_path / 'other'}\nbranch refs/heads/other\n",
            "",
        )

    with pytest.raises(TasklineBridgeError, match="not the expected"):
        TasklineBridge(root=tmp_path, runner=runner).prepare_worktree(
            {"id": "12345678-abcd", "title": "Fix Delivery"},
        )
