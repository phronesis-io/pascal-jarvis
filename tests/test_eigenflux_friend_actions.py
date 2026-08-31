import json
import subprocess

import pytest

from core.actions import ActionProcessor
from core import eigenflux_friends


def test_welcome_message_reads_owner_name_only_from_private_config(tmp_path):
    (tmp_path / "jarvis.yaml").write_text(
        "owner_name: Configured Owner\n", encoding="utf-8"
    )

    message = eigenflux_friends.welcome_message(tmp_path)

    assert "Configured Owner 的 Jarvis" in message
    assert "首席科学家 Configured Owner" in message
    assert eigenflux_friends.welcome_message(tmp_path / "missing") == (
        eigenflux_friends.WELCOME_MESSAGE
    )


def test_temporary_friend_policy_reads_only_owner_structured_fact(tmp_path):
    memory = tmp_path / "memory"
    facts = memory / "hot" / "structured_facts.md"
    facts.parent.mkdir(parents=True)

    assert not eigenflux_friends.temporary_friend_policy_active(memory)

    facts.write_text(
        "eigenflux.friend_policy.temporary: 2026-07-23 起，直至明确撤销\n",
        encoding="utf-8",
    )
    assert eigenflux_friends.temporary_friend_policy_active(memory)

    facts.write_text(
        "eigenflux.friend_policy.temporary: 已撤销\n",
        encoding="utf-8",
    )
    assert not eigenflux_friends.temporary_friend_policy_active(memory)


def _processor(tmp_path):
    return ActionProcessor(
        jarvis_dir=tmp_path,
        memory_dir=tmp_path / "memory",
        jobs_dir=tmp_path / "jobs",
    )


def test_friend_readback_paginates_until_target():
    calls = []

    def runner(command):
        calls.append(command)
        cursor = (
            command[command.index("--cursor") + 1]
            if "--cursor" in command
            else ""
        )
        payload = (
            {"friends": [], "next_cursor": "page-2"}
            if not cursor
            else {
                "friends": [
                    {"agent_id": "target", "agent_name": "Target Agent"}
                ]
            }
        )
        return subprocess.CompletedProcess(
            command, 0, stdout=json.dumps(payload), stderr=""
        )

    friend = eigenflux_friends._friend_by_id("target", runner)

    assert friend["agent_name"] == "Target Agent"
    assert calls[1][calls[1].index("--cursor") + 1] == "page-2"


def test_friend_readback_treats_zero_cursor_as_terminal():
    calls = []

    def runner(command):
        calls.append(command)
        payload = (
            {"friends": [], "next_cursor": "28", "total": 18}
            if "--cursor" not in command
            else {"friends": [], "next_cursor": "0", "total": 18}
        )
        return subprocess.CompletedProcess(
            command, 0, stdout=json.dumps(payload), stderr=""
        )

    assert eigenflux_friends._friend_by_id("new-agent", runner) is None
    assert len(calls) == 2
    assert calls[1][calls[1].index("--cursor") + 1] == "28"


def test_card_action_accepts_and_sends_fixed_welcome(monkeypatch, tmp_path):
    calls = []
    api_calls = []
    friend = {"agent_id": "456", "agent_name": "金融 Agent"}
    friend_reads = 0

    def fake_run(cmd):
        nonlocal friend_reads
        calls.append(cmd)
        if cmd[1:3] == ["relation", "friends"]:
            friend_reads += 1
            rows = [] if friend_reads == 1 else [friend]
            return subprocess.CompletedProcess(
                cmd, 0, stdout=json.dumps({"friends": rows}), stderr="")
        if cmd[1:3] == ["relation", "handle"]:
            return subprocess.CompletedProcess(
                cmd, 0, stdout=json.dumps({"code": 0}), stderr="")
        if cmd[1:3] == ["msg", "history"]:
            return subprocess.CompletedProcess(
                cmd, 0,
                stdout=json.dumps({"messages": [{
                    "msg_id": "m1",
                    "receiver_id": "456",
                    "content": eigenflux_friends.WELCOME_MESSAGE,
                    "created_at": 0,
                }]}),
                stderr="")
        raise AssertionError(cmd)

    monkeypatch.setattr(eigenflux_friends, "run_cli", fake_run)
    monkeypatch.setattr(
        "core.eigenflux_messages.EigenFluxApiClient.send",
        lambda _self, target, content: (
            api_calls.append((target, content))
            or {"code": 0, "data": {"msg_id": "m1", "conv_id": "c1"}}
        ),
    )
    result = _processor(tmp_path)._do_eigenflux_friend(
        "request_id=123|decision=accept|from_uid=456|"
        "from_name=金融 Agent|remark=金融研究")

    assert "已通过" in result
    assert calls[1][:4] == [
        "eigenflux", "relation", "handle", "--request-id"]
    assert api_calls == [("456", eigenflux_friends.WELCOME_MESSAGE)]
    assert not any(call[1:3] == ["msg", "send"] for call in calls)


def test_friend_accept_succeeds_but_private_welcome_is_not_sent(
    monkeypatch, tmp_path,
):
    friend = {"agent_id": "456", "agent_name": "金融 Agent"}

    def runner(command):
        assert command[1:3] == ["relation", "friends"]
        return subprocess.CompletedProcess(
            command, 0, json.dumps({"friends": [friend]}), ""
        )

    monkeypatch.setattr(
        eigenflux_friends,
        "welcome_message",
        lambda _root=None: "欢迎，当前用户数 12000",
    )
    monkeypatch.setattr(
        "core.eigenflux_messages.EigenFluxMessenger",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("blocked welcome must not create a transport")
        ),
    )

    result, failed = eigenflux_friends.execute_friend_action(
        {
            "request_id": "existing-123",
            "decision": "accept",
            "from_uid": "456",
            "from_name": "金融 Agent",
        },
        runner=runner,
        root=tmp_path,
    )

    assert failed is False
    assert "已通过并核验" in result
    assert "business-metric" in result
    assert "没有发送" in result


def test_card_action_raises_when_server_does_not_confirm(monkeypatch, tmp_path):
    calls = []

    def failed(cmd):
        calls.append(cmd)
        if cmd[1:3] == ["relation", "friends"]:
            return subprocess.CompletedProcess(
                cmd, 0, stdout=json.dumps({"friends": []}), stderr="")
        return subprocess.CompletedProcess(
            cmd, 1, stdout="", stderr="request is not pending")

    monkeypatch.setattr(
        eigenflux_friends,
        "run_cli",
        failed,
    )

    with pytest.raises(RuntimeError, match="服务端没有确认成功"):
        _processor(tmp_path)._do_eigenflux_friend(
            "request_id=123|decision=accept|from_uid=456")


def test_repeated_callback_reads_friend_and_does_not_accept_again(
        monkeypatch, tmp_path):
    calls = []
    api_calls = []
    friend = {"agent_id": "456", "agent_name": "金融 Agent"}

    def fake_run(cmd):
        calls.append(cmd)
        if cmd[1:3] == ["relation", "friends"]:
            return subprocess.CompletedProcess(
                cmd, 0, stdout=json.dumps({"friends": [friend]}), stderr="")
        if cmd[1:3] == ["msg", "history"]:
            return subprocess.CompletedProcess(
                cmd, 0,
                stdout=json.dumps({"messages": [{
                    "msg_id": "m1",
                    "receiver_id": "456",
                    "content": eigenflux_friends.WELCOME_MESSAGE,
                    "created_at": 0,
                }]}),
                stderr="")
        raise AssertionError(cmd)

    monkeypatch.setattr(eigenflux_friends, "run_cli", fake_run)
    monkeypatch.setattr(
        "core.eigenflux_messages.EigenFluxApiClient.send",
        lambda _self, target, content: (
            api_calls.append((target, content))
            or {"code": 0, "data": {"msg_id": "m1", "conv_id": "c1"}}
        ),
    )
    result = _processor(tmp_path)._do_eigenflux_friend(
        "request_id=123|decision=accept|from_uid=456|from_name=金融 Agent")

    assert "没有重复执行好友操作" in result
    assert not any(call[1:3] == ["relation", "handle"] for call in calls)
    assert api_calls == [("456", eigenflux_friends.WELCOME_MESSAGE)]


def test_reject_does_not_require_friend_identity_or_readback(tmp_path):
    calls = []

    def runner(command):
        calls.append(command)
        assert command[1:3] == ["relation", "handle"]
        return subprocess.CompletedProcess(command, 0, "{}", "")

    result, failed = eigenflux_friends.execute_friend_action(
        {
            "request_id": "reject-123",
            "decision": "reject",
            "from_name": "Unknown Agent",
        },
        runner=runner,
        root=tmp_path,
    )

    assert failed is False
    assert "已拒绝" in result
    assert len(calls) == 1


def test_interrupted_accept_is_resumed_from_durable_pending_step(
    monkeypatch, tmp_path,
):
    from core.delegation_reconcile import DelegationReconciler
    from core.delegations import DelegationStore

    def interrupted(command):
        if command[1:3] == ["relation", "friends"]:
            return subprocess.CompletedProcess(
                command, 0, json.dumps({"friends": []}), ""
            )
        if command[1:3] == ["relation", "handle"]:
            raise KeyboardInterrupt
        raise AssertionError(command)

    with pytest.raises(KeyboardInterrupt):
        eigenflux_friends.execute_friend_action(
            {
                "request_id": "resume-123",
                "decision": "accept",
                "from_uid": "456",
                "from_name": "金融 Agent",
            },
            runner=interrupted,
            root=tmp_path,
        )

    store = DelegationStore(root=tmp_path)
    detail = store.get(store.list()[0]["id"])
    assert detail["status"] == "executing"
    assert detail["steps"][0]["attempt_count"] == 0
    with store._tx() as db:
        db.execute(
            "UPDATE delegation_steps SET lease_expires_at=0 WHERE id=?",
            (detail["steps"][0]["id"],),
        )

    friend_reads = 0

    def recovered(command):
        nonlocal friend_reads
        if command[1:3] == ["relation", "friends"]:
            friend_reads += 1
            friends = (
                []
                if friend_reads == 1
                else [{"agent_id": "456", "agent_name": "金融 Agent"}]
            )
            return subprocess.CompletedProcess(
                command, 0, json.dumps({"friends": friends}), ""
            )
        if command[1:3] == ["relation", "handle"]:
            return subprocess.CompletedProcess(command, 0, "{}", "")
        if command[1:3] == ["msg", "history"]:
            return subprocess.CompletedProcess(
                command,
                0,
                json.dumps({"messages": [{
                    "msg_id": "welcome-1",
                    "receiver_id": "456",
                    "content": eigenflux_friends.WELCOME_MESSAGE,
                    "created_at": 0,
                }]}),
                "",
            )
        raise AssertionError(command)

    monkeypatch.setattr(eigenflux_friends, "run_cli", recovered)
    monkeypatch.setattr(
        "core.eigenflux_messages.EigenFluxApiClient.send",
        lambda _self, _target, _content: {
            "code": 0,
            "data": {"msg_id": "welcome-1", "conv_id": "conv-1"},
        },
    )

    result = DelegationReconciler(store=store).run(send_items=False)

    assert result["released_leases"] == [detail["steps"][0]["id"]]
    assert result["verified"] == 1
    assert store.get(detail["id"])["status"] == "completed"


def test_accept_recovers_after_remote_commit_before_attempt_record(
    monkeypatch, tmp_path,
):
    from core.delegations import DelegationStore

    friend = {"agent_id": "456", "agent_name": "金融 Agent"}
    friend_reads = 0

    def runner(command):
        nonlocal friend_reads
        if command[1:3] == ["relation", "friends"]:
            friend_reads += 1
            rows = [] if friend_reads == 1 else [friend]
            return subprocess.CompletedProcess(
                command, 0, json.dumps({"friends": rows}), ""
            )
        if command[1:3] == ["relation", "handle"]:
            return subprocess.CompletedProcess(command, 0, "{}", "")
        if command[1:3] == ["msg", "history"]:
            return subprocess.CompletedProcess(
                command,
                0,
                json.dumps({"messages": [{
                    "msg_id": "welcome-2",
                    "receiver_id": "456",
                    "content": eigenflux_friends.WELCOME_MESSAGE,
                    "created_at": 0,
                }]}),
                "",
            )
        raise AssertionError(command)

    original = DelegationStore.record_attempt
    interrupted = [False]

    def crash_once(self, *args, **kwargs):
        if not interrupted[0]:
            interrupted[0] = True
            raise KeyboardInterrupt
        return original(self, *args, **kwargs)

    monkeypatch.setattr(DelegationStore, "record_attempt", crash_once)
    action = {
        "request_id": "commit-gap-123",
        "decision": "accept",
        "from_uid": "456",
        "from_name": "金融 Agent",
    }
    with pytest.raises(KeyboardInterrupt):
        eigenflux_friends.execute_friend_action(
            action, runner=runner, root=tmp_path
        )

    store = DelegationStore(root=tmp_path)
    detail = store.get(store.list()[0]["id"])
    assert detail["status"] == "executing"
    monkeypatch.setattr(
        "core.eigenflux_messages.EigenFluxApiClient.send",
        lambda _self, _target, _content: {
            "code": 0,
            "data": {"msg_id": "welcome-2", "conv_id": "conv-2"},
        },
    )

    result, failed = eigenflux_friends.execute_friend_action(
        action, runner=runner, root=tmp_path
    )

    assert failed is False
    assert "没有重复执行好友操作" in result
    assert store.get(detail["id"])["status"] == "completed"


def test_accept_readback_timeout_leaves_recoverable_delegation(tmp_path):
    from core.delegations import DelegationStore

    friend_reads = 0

    def runner(command):
        nonlocal friend_reads
        if command[1:3] == ["relation", "friends"]:
            friend_reads += 1
            if friend_reads == 1:
                return subprocess.CompletedProcess(
                    command, 0, json.dumps({"friends": []}), ""
                )
            raise subprocess.TimeoutExpired(command, 30)
        if command[1:3] == ["relation", "handle"]:
            return subprocess.CompletedProcess(
                command, 0, json.dumps({"code": 0}), ""
            )
        raise AssertionError(command)

    result, failed = eigenflux_friends.execute_friend_action(
        {
            "request_id": "123",
            "decision": "accept",
            "from_uid": "456",
            "from_name": "金融 Agent",
        },
        runner=runner,
        root=tmp_path,
    )

    assert failed is True
    assert "仍在核验" in result
    store = DelegationStore(root=tmp_path)
    detail = store.get(store.list()[0]["id"])
    assert detail["source"] == "eigenflux-friend"
    assert detail["status"] == "verifying"


def test_uncertain_welcome_is_projected_for_scheduled_reconciliation(
    monkeypatch, tmp_path,
):
    from core.delegations import DelegationStore
    from core.eigenflux_messages import MessageReceipt

    friend = {"agent_id": "456", "agent_name": "金融 Agent"}

    def runner(command):
        if command[1:3] == ["relation", "friends"]:
            return subprocess.CompletedProcess(
                command, 0, json.dumps({"friends": [friend]}), ""
            )
        raise AssertionError(command)

    class Messenger:
        def __init__(self, **_kwargs):
            pass

        def send_to_friend_id(self, _agent_id, _content):
            return MessageReceipt(
                state="verifying",
                recipient_name="金融 Agent",
                recipient_id="456",
                idempotency_key="welcome-action-key",
                conv_id="conv-1",
            )

    monkeypatch.setattr(
        "core.eigenflux_messages.EigenFluxMessenger", Messenger
    )

    result, failed = eigenflux_friends.execute_friend_action(
        {
            "request_id": "123",
            "decision": "accept",
            "from_uid": "456",
            "from_name": "金融 Agent",
        },
        runner=runner,
        root=tmp_path,
    )

    assert failed is True
    assert "欢迎消息已执行" in result
    store = DelegationStore(root=tmp_path)
    message = next(
        row for row in store.list()
        if row["source"] == "eigenflux-message"
    )
    detail = store.get(message["id"])
    assert detail["status"] == "verifying"
    assert (
        detail["verification_policy"]["idempotency_key"]
        == "welcome-action-key"
    )
