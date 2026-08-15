from __future__ import annotations

import json
import urllib.error
from dataclasses import dataclass

import pytest

from core import lark_bot_transport as transport


@dataclass
class _Response:
    payload: dict

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit=-1):
        return json.dumps(self.payload, ensure_ascii=False).encode("utf-8")


class _Opener:
    def __init__(self, *payloads):
        self.payloads = list(payloads)
        self.calls = []

    def __call__(self, request, timeout):
        self.calls.append((request, timeout))
        payload = self.payloads.pop(0)
        if isinstance(payload, BaseException):
            raise payload
        return _Response(payload)


def _env():
    return {
        "LARK_APP_ID": "cli_test",
        "LARK_APP_SECRET": "private-secret",
        "LARK_OPENAPI_BASE_URL": "https://open.feishu.cn",
    }


def _body(call):
    return json.loads(call[0].data.decode("utf-8"))


def setup_function():
    transport.clear_token_cache()


def test_unconfigured_transport_declines_without_opening_network(tmp_path):
    opener = _Opener()

    result = transport.send(
        text="hello", user_id="ou_owner", root=tmp_path, env={}, opener=opener,
    )

    assert result.attempted is False
    assert result.ok is False
    assert result.error == "bot_credentials_unavailable"
    assert opener.calls == []


@pytest.mark.parametrize(
    "custom_base",
    ["http://attacker.invalid", "https://attacker.invalid"],
)
def test_untrusted_custom_api_base_is_never_used(custom_base):
    opener = _Opener(
        {"code": 0, "tenant_access_token": "token", "expire": 7200},
        {"code": 0, "data": {"message_id": "om_safe"}},
    )
    env = {**_env(), "LARK_OPENAPI_BASE_URL": custom_base}

    result = transport.send(
        text="hello", user_id="ou_owner", env=env, opener=opener,
    )

    assert result.ok is True
    assert all(call[0].full_url.startswith("https://open.feishu.cn/")
               for call in opener.calls)


def test_markdown_send_uses_bot_token_and_requires_message_receipt():
    opener = _Opener(
        {"code": 0, "tenant_access_token": "tenant-token", "expire": 7200},
        {"code": 0, "data": {"message_id": "om_sent"}},
    )

    result = transport.send(
        text="**重点** [链接](https://example.com)",
        user_id="ou_owner",
        env=_env(),
        opener=opener,
        now_epoch=100,
    )

    assert result.ok is True
    assert result.message_id == "om_sent"
    assert len(opener.calls) == 2
    token_call, send_call = opener.calls
    assert token_call[0].full_url.endswith(
        "/open-apis/auth/v3/tenant_access_token/internal"
    )
    assert _body(token_call) == {
        "app_id": "cli_test", "app_secret": "private-secret",
    }
    assert send_call[0].full_url.endswith(
        "/open-apis/im/v1/messages?receive_id_type=open_id"
    )
    assert send_call[0].headers["Authorization"] == "Bearer tenant-token"
    body = _body(send_call)
    assert body["receive_id"] == "ou_owner"
    assert body["msg_type"] == "post"
    content = json.loads(body["content"])
    assert content["zh_cn"]["content"][0][0] == {
        "tag": "md", "text": "**重点** [链接](https://example.com)",
    }
    assert "private-secret" not in repr(result)
    assert "tenant-token" not in repr(result)


def test_token_is_reused_in_memory_and_never_written(tmp_path):
    opener = _Opener(
        {"code": 0, "tenant_access_token": "tenant-token", "expire": 7200},
        {"code": 0, "data": {"message_id": "om_one"}},
        {"code": 0, "data": {"message_id": "om_two"}},
    )

    one = transport.send(
        text="one", user_id="ou_owner", root=tmp_path, env=_env(),
        opener=opener, now_epoch=100,
    )
    two = transport.send(
        text="two", user_id="ou_owner", root=tmp_path, env=_env(),
        opener=opener, now_epoch=101,
    )

    assert one.message_id == "om_one"
    assert two.message_id == "om_two"
    assert len(opener.calls) == 3
    assert list(tmp_path.rglob("*")) == []


def test_missing_message_id_is_a_failed_receipt():
    opener = _Opener(
        {"code": 0, "tenant_access_token": "token", "expire": 7200},
        {"code": 0, "data": {}},
    )

    result = transport.send(
        text="hello", chat_id="oc_chat", env=_env(), opener=opener,
    )

    assert result.attempted is True
    assert result.ok is False
    assert result.error == "message_receipt_missing"


def test_http_401_refreshes_cached_token_once_before_sending():
    opener = _Opener(
        {"code": 0, "tenant_access_token": "stale", "expire": 7200},
        urllib.error.HTTPError("url", 401, "unauthorized", {}, None),
        {"code": 0, "tenant_access_token": "fresh", "expire": 7200},
        {"code": 0, "data": {"message_id": "om_refreshed"}},
    )

    result = transport.send(
        text="hello", user_id="ou_owner", env=_env(), opener=opener,
        now_epoch=100,
    )

    assert result.message_id == "om_refreshed"
    assert len(opener.calls) == 4
    assert opener.calls[1][0].headers["Authorization"] == "Bearer stale"
    assert opener.calls[3][0].headers["Authorization"] == "Bearer fresh"


def test_reply_quotes_message_id_and_card_rejects_invalid_json():
    invalid = transport.send(
        card_json="not-json", user_id="ou_owner", env=_env(), opener=_Opener(),
    )
    opener = _Opener(
        {"code": 0, "tenant_access_token": "token", "expire": 7200},
        {"code": 0, "data": {"message_id": "om_reply"}},
    )
    reply = transport.send(
        text="reply", reply_to="om/a b", env=_env(), opener=opener,
    )

    assert invalid.error == "invalid_payload"
    assert reply.ok is True
    assert opener.calls[-1][0].full_url.endswith(
        "/open-apis/im/v1/messages/om%2Fa%20b/reply"
    )
    assert "receive_id" not in _body(opener.calls[-1])


def test_cli_argument_adapter_supports_card_and_emergency_text():
    cards = _Opener(
        {"code": 0, "tenant_access_token": "token", "expire": 7200},
        {"code": 0, "data": {"message_id": "om_card"}},
    )
    card = transport.send_from_cli_args(
        ["--chat-id", "oc_chat", "--msg-type", "interactive",
         "--content", '{"elements":[]}'],
        env=_env(), opener=cards,
    )
    transport.clear_token_cache()
    texts = _Opener(
        {"code": 0, "tenant_access_token": "token", "expire": 7200},
        {"code": 0, "data": {"message_id": "om_text"}},
    )
    text = transport.send_from_cli_args(
        ["--receive-id", "ou_owner", "--receive-id-type", "open_id",
         "--msg-type", "text", "--content", '{"text":"warning"}'],
        env=_env(), opener=texts,
    )

    assert card.message_id == "om_card"
    assert _body(cards.calls[-1])["msg_type"] == "interactive"
    assert text.message_id == "om_text"
    assert "warning" in _body(texts.calls[-1])["content"]


def test_bot_open_id_uses_same_cached_tenant_token():
    opener = _Opener(
        {"code": 0, "tenant_access_token": "token", "expire": 7200},
        {"code": 0, "bot": {"open_id": "ou_bot"}},
    )

    assert transport.bot_open_id(
        env=_env(), opener=opener, now_epoch=100,
    ) == "ou_bot"
    assert opener.calls[-1][0].method == "GET"
    assert opener.calls[-1][0].full_url.endswith("/open-apis/bot/v3/info")


def test_self_diagnostic_emergency_send_prefers_bot_api(monkeypatch):
    from core import memorial
    from tasks import self_diagnostic_post

    calls = []
    monkeypatch.setattr(
        memorial,
        "create",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("ledger down")),
    )
    monkeypatch.setattr(
        transport,
        "send",
        lambda **kwargs: (
            calls.append(kwargs)
            or transport.BotSendResult(True, True, "om_diagnostic")
        ),
    )
    monkeypatch.setattr(
        self_diagnostic_post.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("CLI should not run")
        ),
    )

    assert self_diagnostic_post._send("warning", "ou_owner") is True
    assert calls == [{"text": "warning", "user_id": "ou_owner"}]
