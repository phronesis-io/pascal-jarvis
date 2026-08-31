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


def test_unconfigured_card_update_declines_without_opening_network(tmp_path):
    opener = _Opener()

    result = transport.update_card(
        "om_existing", '{"schema":"2.0"}', root=tmp_path,
        env={}, opener=opener,
    )

    assert result.attempted is False
    assert result.error == "bot_credentials_unavailable"
    assert opener.calls == []


def test_card_update_uses_bot_token_and_patch_receipt():
    opener = _Opener(
        {"code": 0, "tenant_access_token": "tenant-token", "expire": 7200},
        {"code": 0, "data": {}},
    )
    card = json.dumps({
        "schema": "2.0",
        "body": {"elements": [{"tag": "markdown", "content": "新正文"}]},
    }, ensure_ascii=False)

    result = transport.update_card(
        "om/a b", card, env=_env(), opener=opener, now_epoch=100,
    )

    assert result.ok is True
    assert result.message_id == "om/a b"
    request = opener.calls[-1][0]
    assert request.method == "PATCH"
    assert request.full_url.endswith("/open-apis/im/v1/messages/om%2Fa%20b")
    assert request.headers["Authorization"] == "Bearer tenant-token"
    body = _body(opener.calls[-1])
    assert "新正文" in body["content"]


def test_card_update_validates_identity_payload_and_api_receipt():
    missing_id = transport.update_card(
        "", '{"schema":"2.0"}', env=_env(), opener=_Opener(),
    )
    invalid = transport.update_card(
        "om_existing", "not-json", env=_env(), opener=_Opener(),
    )
    opener = _Opener(
        {"code": 0, "tenant_access_token": "token", "expire": 7200},
        {"code": 19001, "msg": "message cannot be updated"},
    )
    rejected = transport.update_card(
        "om_existing", '{"schema":"2.0"}', env=_env(), opener=opener,
    )

    assert missing_id.error == "message_id_required"
    assert invalid.error == "invalid_payload"
    assert rejected.error == "message_update_rejected"


def test_card_update_refreshes_expired_bot_token_once():
    opener = _Opener(
        {"code": 0, "tenant_access_token": "stale", "expire": 7200},
        urllib.error.HTTPError("url", 401, "unauthorized", {}, None),
        {"code": 0, "tenant_access_token": "fresh", "expire": 7200},
        {"code": 0, "data": {}},
    )

    result = transport.update_card(
        "om_existing", '{"schema":"2.0"}', env=_env(), opener=opener,
        now_epoch=100,
    )

    assert result.ok is True
    assert len(opener.calls) == 4
    assert opener.calls[1][0].headers["Authorization"] == "Bearer stale"
    assert opener.calls[3][0].headers["Authorization"] == "Bearer fresh"


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (urllib.error.HTTPError("url", 429, "limited", {}, None), "http_429"),
        (urllib.error.URLError("offline"), "network_error"),
        (TimeoutError("slow"), "timeout"),
        (OSError("socket closed"), "request_failed"),
    ],
)
def test_card_update_maps_transport_failures_without_leaking_details(
    failure, expected,
):
    opener = _Opener(
        {"code": 0, "tenant_access_token": "token", "expire": 7200},
        failure,
    )

    result = transport.update_card(
        "om_existing", '{"schema":"2.0"}', env=_env(), opener=opener,
    )

    assert result.attempted is True
    assert result.ok is False
    assert result.error == expected
    assert "socket closed" not in repr(result)


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
    assert body["uuid"].startswith("jv_")
    assert len(body["uuid"]) <= 50
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
    assert _body(opener.calls[1])["uuid"] != _body(opener.calls[2])["uuid"]
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
    reply_body = _body(opener.calls[-1])
    assert "receive_id" not in reply_body
    assert reply_body["uuid"].startswith("jv_")


def test_explicit_idempotency_key_is_sent_and_bounded():
    opener = _Opener(
        {"code": 0, "tenant_access_token": "token", "expire": 7200},
        {"code": 0, "data": {"message_id": "om_sent"}},
    )

    result = transport.send(
        text="hello",
        user_id="ou_owner",
        idempotency_key="dlv_stable",
        env=_env(),
        opener=opener,
    )

    assert result.ok is True
    assert _body(opener.calls[1])["uuid"] == "dlv_stable"
    assert len(transport._delivery_uuid("x" * 80)) <= 50


def test_cli_argument_adapter_supports_card_and_emergency_text():
    cards = _Opener(
        {"code": 0, "tenant_access_token": "token", "expire": 7200},
        {"code": 0, "data": {"message_id": "om_card"}},
    )
    card = transport.send_from_cli_args(
        ["--chat-id", "oc_chat", "--msg-type", "interactive",
         "--content", '{"elements":[]}',
         "--idempotency-key", "memorial_stable"],
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
    assert _body(cards.calls[-1])["uuid"] == "memorial_stable"
    assert text.message_id == "om_text"
    assert "warning" in _body(texts.calls[-1])["content"]


def test_bot_api_strips_internal_card_envelope_fields():
    opener = _Opener(
        {"code": 0, "tenant_access_token": "token", "expire": 7200},
        {"code": 0, "data": {"message_id": "om_card"}},
    )

    result = transport.send(
        card_json=json.dumps({
            "elements": [],
            "__jarvis_full_body": "private full body",
            "__jarvis_context": "private context",
        }),
        user_id="ou_owner",
        env=_env(),
        opener=opener,
    )

    assert result.ok is True
    sent_card = json.loads(_body(opener.calls[-1])["content"])
    assert sent_card == {"elements": []}


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
