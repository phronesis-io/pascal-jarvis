import io
import json
import subprocess

from tasks import eigenflux_friends_post as post


def _pending(*requests):
    return subprocess.CompletedProcess(
        [], 0, stdout=json.dumps({"requests": list(requests)}), stderr="")


def _run_main(monkeypatch, capsys, payload, results):
    calls = []

    def fake_run(cmd):
        calls.append(cmd)
        return results.pop(0)

    monkeypatch.setattr(post, "_run", fake_run)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    assert post.main() == 0
    return calls, capsys.readouterr()


def test_accept_is_verified_then_welcome_is_sent(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("JARVIS_DIR", str(tmp_path))
    api_calls = []
    monkeypatch.setattr(
        "core.eigenflux_messages.EigenFluxApiClient.send",
        lambda _self, target, content: (
            api_calls.append((target, content))
            or {"code": 0, "data": {"msg_id": "m1", "conv_id": "c1"}}
        ),
    )
    payload = {
        "actions": [{
            "request_id": "123",
            "decision": "accept",
            "from_uid": "456",
            "from_name": "金融 Agent",
            "remark": "金融 Agent",
        }],
        "user_message": "",
    }
    pending = _pending({
        "request_id": "123",
        "from_uid": "456",
        "from_name": "金融 Agent",
        "greeting": "hello",
    })
    friends_empty = subprocess.CompletedProcess(
        [], 0, stdout=json.dumps({"friends": []}), stderr="")
    friends_present = subprocess.CompletedProcess(
        [], 0, stdout=json.dumps({"friends": [{
            "agent_id": "456", "agent_name": "金融 Agent",
        }]}), stderr="")
    ok = subprocess.CompletedProcess(
        [], 0, stdout=json.dumps({"code": 0}), stderr="")
    history = subprocess.CompletedProcess(
        [], 0, stdout=json.dumps({"messages": [{
            "msg_id": "m1",
            "receiver_id": "456",
            "content": post.WELCOME_MESSAGE,
            "created_at": 0,
        }]}), stderr="")

    calls, output = _run_main(
        monkeypatch,
        capsys,
        payload,
        [
            pending,
            friends_empty,
            ok,
            friends_present,
            friends_present,
            history,
        ],
    )

    assert calls[0][:4] == [
        "eigenflux", "relation", "list", "--direction"]
    assert calls[2][:4] == [
        "eigenflux", "relation", "handle", "--request-id"]
    assert api_calls == [("456", post.WELCOME_MESSAGE)]
    assert not any(call[1:3] == ["msg", "send"] for call in calls)
    assert "已通过" in output.out
    assert "发了欢迎" in output.out
    assert "123" not in output.out


def test_accept_failure_never_sends_welcome_or_claims_success(
        monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("JARVIS_DIR", str(tmp_path))
    payload = {
        "actions": [{
            "request_id": "123",
            "decision": "accept",
            "from_uid": "456",
            "from_name": "金融 Agent",
        }],
        "user_message": "模型说已处理",
    }
    failed = subprocess.CompletedProcess(
        [], 1, stdout="", stderr="server rejected")
    pending = _pending({
        "request_id": "123",
        "from_uid": "456",
        "from_name": "金融 Agent",
    })

    friends_empty = subprocess.CompletedProcess(
        [], 0, stdout=json.dumps({"friends": []}), stderr="")
    calls, output = _run_main(
        monkeypatch,
        capsys,
        payload,
        [pending, friends_empty, failed, friends_empty],
    )

    assert len(calls) == 4
    assert "没有确认成功" in output.out
    assert "已通过" not in output.out
    assert "模型说已处理" not in output.out
    assert "server rejected" in output.err


def test_review_only_message_still_emits_card(monkeypatch, capsys):
    created = []
    monkeypatch.setattr(
        post.memorial, "create",
        lambda **kwargs: (created.append(kwargs) or ("mem_review", True)))
    payload = {
        "actions": [],
        "user_message": "这个申请疑似冒充官方账号，需要你判断。",
    }

    pending = _pending({
        "request_id": "3385",
        "from_uid": "3378",
        "from_name": "金融 Agent",
        "greeting": "想交换可验证输出",
    })
    calls, output = _run_main(monkeypatch, capsys, payload, [pending])

    assert len(calls) == 1
    assert output.out == ""
    assert len(created) == 1
    assert "疑似冒充" in created[0]["body"]


def test_structured_review_creates_request_bound_action_card(
        monkeypatch, capsys):
    created = []

    def fake_create(**kwargs):
        created.append(kwargs)
        return "mem_review", True

    monkeypatch.setattr(post.memorial, "create", fake_create)
    payload = {
        "actions": [],
        "reviews": [{
            "request_id": "3385",
            "from_uid": "model-invented-uid",
            "from_name": "模型编造的名字",
            "greeting": "模型改写的招呼语",
            "remark": "金融研究",
            "risk_reason": "身份信息不足",
        }],
        "user_message": "这句不应再生成第二张普通卡",
    }

    pending = _pending({
        "request_id": "3385",
        "from_uid": "3378",
        "from_name": "金融 Agent",
        "greeting": "想交换可验证输出",
    })
    calls, output = _run_main(monkeypatch, capsys, payload, [pending])

    assert calls[0][:4] == [
        "eigenflux", "relation", "list", "--direction"]
    assert output.out == ""
    assert len(created) == 1
    card = created[0]
    assert '"request_id": "3385"' in card["context"]
    assert card["dedup_key"] == "eigenflux-friend:3385"
    assert "金融 Agent" in card["body"]
    assert "模型编造的名字" not in card["body"]
    assert [o["label"] for o in card["options"]] == ["通过", "拒绝"]
    assert card["options"][0]["action"] == {
        "type": "eigenflux_friend",
        "params": {
            "request_id": "3385",
            "from_uid": "3378",
            "from_name": "金融 Agent",
            "remark": "金融研究",
            "decision": "accept",
        },
    }


def test_stale_model_output_is_dropped_when_request_is_no_longer_pending(
        monkeypatch, capsys):
    payload = {
        "actions": [{
            "request_id": "already-handled",
            "decision": "accept",
            "from_uid": "model-supplied-id",
        }],
        "reviews": [{
            "request_id": "already-handled",
            "risk_reason": "old output",
        }],
        "user_message": "不要发送这条旧消息",
    }

    calls, output = _run_main(monkeypatch, capsys, payload, [_pending()])

    assert len(calls) == 1
    assert output.out == ""


def test_model_cannot_auto_reject_a_request(monkeypatch, capsys):
    created = []
    monkeypatch.setattr(
        post.memorial, "create",
        lambda **kwargs: (created.append(kwargs) or ("mem_review", True)))
    payload = {
        "actions": [{
            "request_id": "123",
            "decision": "reject",
            "from_uid": "456",
        }],
        "reviews": [],
        "user_message": "",
    }
    pending = _pending({
        "request_id": "123",
        "from_uid": "456",
        "from_name": "金融 Agent",
        "greeting": "hello",
    })

    calls, output = _run_main(monkeypatch, capsys, payload, [pending])

    assert len(calls) == 1
    assert output.out == ""
    assert len(created) == 1
    assert "自动拒绝不被允许" in created[0]["body"]
