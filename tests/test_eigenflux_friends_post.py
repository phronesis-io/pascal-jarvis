import io
import json
import subprocess

from tasks import eigenflux_friends_post as post


def _run_main(monkeypatch, capsys, payload, results):
    calls = []

    def fake_run(cmd):
        calls.append(cmd)
        return results.pop(0)

    monkeypatch.setattr(post, "_run", fake_run)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    assert post.main() == 0
    return calls, capsys.readouterr()


def test_accept_is_verified_then_welcome_is_sent(monkeypatch, capsys):
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
    ok = subprocess.CompletedProcess([], 0, stdout="ok", stderr="")

    calls, output = _run_main(monkeypatch, capsys, payload, [ok, ok])

    assert calls[0][:4] == [
        "eigenflux", "relation", "handle", "--request-id"]
    assert calls[1][:4] == [
        "eigenflux", "msg", "send", "--receiver-id"]
    assert post.WELCOME_MESSAGE in calls[1]
    assert "已通过" in output.out
    assert "发了欢迎" in output.out
    assert "123" not in output.out


def test_accept_failure_never_sends_welcome_or_claims_success(
        monkeypatch, capsys):
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

    calls, output = _run_main(monkeypatch, capsys, payload, [failed])

    assert len(calls) == 1
    assert "处理未完成" in output.out
    assert "已通过" not in output.out
    assert "模型说已处理" not in output.out
    assert "server rejected" in output.err


def test_review_only_message_still_emits_card(monkeypatch, capsys):
    payload = {
        "actions": [],
        "user_message": "这个申请疑似冒充官方账号，需要你判断。",
    }

    calls, output = _run_main(monkeypatch, capsys, payload, [])

    assert calls == []
    assert "疑似冒充" in output.out
