import json
import subprocess

import pytest

from core.actions import ActionProcessor
from core import eigenflux_friends


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
