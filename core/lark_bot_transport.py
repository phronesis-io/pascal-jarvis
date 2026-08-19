"""Keychain-independent Lark transport for the application bot identity.

Bot delivery needs only the application's ``app_id`` and ``app_secret``.  It
must not depend on the owner's user OAuth token or lark-cli's macOS Keychain.
User-identity APIs (calendar, docs, mail, tasks) remain separate and continue
to fail closed when their OAuth token is unavailable.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence


DEFAULT_BASE_URL = "https://open.feishu.cn"
ALLOWED_BASE_URLS = frozenset({
    DEFAULT_BASE_URL,
    "https://open.larksuite.com",
})
DEFAULT_TIMEOUT_SECONDS = 15
TOKEN_REFRESH_MARGIN_SECONDS = 60
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_IDEMPOTENCY_KEY_CHARS = 50


@dataclass(frozen=True, slots=True)
class BotSendResult:
    attempted: bool
    ok: bool
    message_id: str = ""
    error: str = ""


_TOKEN_CACHE: dict[str, tuple[str, float]] = {}
_TOKEN_LOCK = threading.Lock()


def clear_token_cache() -> None:
    """Test/recovery hook; credentials and tokens are never persisted."""
    with _TOKEN_LOCK:
        _TOKEN_CACHE.clear()


def _runtime_root(root: str | Path | None = None) -> Path:
    return Path(root or os.environ.get("JARVIS_DIR") or Path.cwd()).resolve()


def _credentials(
    root: str | Path | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> tuple[str, str]:
    env = os.environ if env is None else env
    app_id = str(env.get("LARK_APP_ID") or env.get("APP_ID") or "").strip()
    app_secret = str(env.get("LARK_APP_SECRET") or "").strip()
    if app_id and app_secret:
        return app_id, app_secret
    try:
        from core.config import Config

        lark = Config(_runtime_root(root) / "jarvis.yaml").lark
        return (
            app_id or str(lark.get("app_id") or "").strip(),
            app_secret or str(lark.get("app_secret") or "").strip(),
        )
    except Exception:
        return app_id, app_secret


def configured(
    root: str | Path | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> bool:
    app_id, app_secret = _credentials(root, env=env)
    return bool(app_id and app_secret)


def _base_url(env: Mapping[str, str]) -> str:
    value = str(env.get("LARK_OPENAPI_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
    return value if value in ALLOWED_BASE_URLS else DEFAULT_BASE_URL


def _cache_key(app_id: str, app_secret: str, base_url: str) -> str:
    return hashlib.sha256(
        f"{base_url}\0{app_id}\0{app_secret}".encode("utf-8")
    ).hexdigest()


def _delivery_uuid(value: str) -> str:
    """Return a stable, API-sized key for retry-safe message delivery."""
    explicit = str(value or "").strip()
    if explicit and len(explicit) <= MAX_IDEMPOTENCY_KEY_CHARS:
        return explicit
    if explicit:
        return "jv_" + hashlib.sha256(explicit.encode("utf-8")).hexdigest()[:40]
    # No caller identity means this is a new logical send. Content-derived
    # keys would incorrectly collapse two legitimate identical messages sent
    # within Lark's one-hour UUID deduplication window.
    return "jv_" + uuid.uuid4().hex


def _read_json(response) -> dict:
    raw = response.read(MAX_RESPONSE_BYTES + 1)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ValueError("response_too_large")
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("response_not_object")
    return value


def _request_json(
    url: str,
    body: dict,
    *,
    headers: Mapping[str, str] | None = None,
    opener: Callable = urllib.request.urlopen,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json; charset=utf-8",
            **dict(headers or {}),
        },
        method="POST",
    )
    with opener(request, timeout=timeout) as response:
        return _read_json(response)


def _get_json(
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    opener: Callable = urllib.request.urlopen,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> dict:
    request = urllib.request.Request(
        url, headers=dict(headers or {}), method="GET"
    )
    with opener(request, timeout=timeout) as response:
        return _read_json(response)


def _tenant_token(
    app_id: str,
    app_secret: str,
    base_url: str,
    *,
    opener: Callable,
    timeout: int,
    now_epoch: float,
    force_refresh: bool = False,
) -> str:
    key = _cache_key(app_id, app_secret, base_url)
    with _TOKEN_LOCK:
        cached = _TOKEN_CACHE.get(key)
        if (
            not force_refresh
            and cached
            and cached[1] - TOKEN_REFRESH_MARGIN_SECONDS > now_epoch
        ):
            return cached[0]
        data = _request_json(
            f"{base_url}/open-apis/auth/v3/tenant_access_token/internal",
            {"app_id": app_id, "app_secret": app_secret},
            opener=opener,
            timeout=timeout,
        )
        if int(data.get("code") or 0) != 0:
            raise RuntimeError("tenant_token_rejected")
        token = str(data.get("tenant_access_token") or "").strip()
        if not token:
            raise RuntimeError("tenant_token_missing")
        try:
            expires_in = max(1, int(data.get("expire") or 7200))
        except (TypeError, ValueError):
            expires_in = 7200
        _TOKEN_CACHE[key] = (token, now_epoch + expires_in)
        return token


def _markdown_payload(text: str) -> tuple[str, str]:
    content = {
        "zh_cn": {
            "title": "",
            "content": [[{"tag": "md", "text": str(text or "")}]],
        }
    }
    return "post", json.dumps(content, ensure_ascii=False, separators=(",", ":"))


def _card_payload(card_json: str) -> tuple[str, str]:
    from core.card import strip_internal_fields

    return "interactive", strip_internal_fields(card_json)


def send(
    *,
    text: str = "",
    card_json: str = "",
    user_id: str = "",
    chat_id: str = "",
    reply_to: str = "",
    idempotency_key: str = "",
    root: str | Path | None = None,
    env: Mapping[str, str] | None = None,
    opener: Callable = urllib.request.urlopen,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    now_epoch: float | None = None,
) -> BotSendResult:
    """Send once as the application bot and require a real message receipt."""
    env = os.environ if env is None else env
    app_id, app_secret = _credentials(root, env=env)
    if not app_id or not app_secret:
        return BotSendResult(False, False, error="bot_credentials_unavailable")
    if bool(text) == bool(card_json):
        return BotSendResult(True, False, error="exactly_one_payload_required")
    if not reply_to and bool(user_id) == bool(chat_id):
        return BotSendResult(True, False, error="exactly_one_target_required")
    try:
        msg_type, content = (
            _card_payload(card_json) if card_json else _markdown_payload(text)
        )
    except (TypeError, ValueError, json.JSONDecodeError):
        return BotSendResult(True, False, error="invalid_payload")

    base_url = _base_url(env)
    now_epoch = float(time.time() if now_epoch is None else now_epoch)
    try:
        if reply_to:
            message_id = urllib.parse.quote(str(reply_to), safe="")
            url = f"{base_url}/open-apis/im/v1/messages/{message_id}/reply"
            body = {"msg_type": msg_type, "content": content}
        else:
            receive_type = "chat_id" if chat_id else "open_id"
            target = str(chat_id or user_id)
            url = (
                f"{base_url}/open-apis/im/v1/messages?"
                f"receive_id_type={receive_type}"
            )
            body = {
                "receive_id": target,
                "msg_type": msg_type,
                "content": content,
            }
        body["uuid"] = _delivery_uuid(idempotency_key)
        data = {}
        for auth_attempt in range(2):
            token = _tenant_token(
                app_id,
                app_secret,
                base_url,
                opener=opener,
                timeout=timeout,
                now_epoch=now_epoch,
                force_refresh=bool(auth_attempt),
            )
            try:
                data = _request_json(
                    url,
                    body,
                    headers={"Authorization": f"Bearer {token}"},
                    opener=opener,
                    timeout=timeout,
                )
                break
            except urllib.error.HTTPError as exc:
                if exc.code == 401 and auth_attempt == 0:
                    continue
                raise
        if int(data.get("code") or 0) != 0:
            return BotSendResult(True, False, error="message_rejected")
        payload = data.get("data") if isinstance(data.get("data"), dict) else {}
        message_id = str(payload.get("message_id") or "").strip()
        if not message_id:
            return BotSendResult(True, False, error="message_receipt_missing")
        return BotSendResult(True, True, message_id=message_id)
    except urllib.error.HTTPError as exc:
        return BotSendResult(True, False, error=f"http_{int(exc.code)}")
    except urllib.error.URLError:
        return BotSendResult(True, False, error="network_error")
    except TimeoutError:
        return BotSendResult(True, False, error="timeout")
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError):
        return BotSendResult(True, False, error="request_failed")


def send_from_cli_args(
    args: Sequence[str],
    *,
    root: str | Path | None = None,
    env: Mapping[str, str] | None = None,
    opener: Callable = urllib.request.urlopen,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> BotSendResult:
    """Compatibility adapter for existing bot-only ``messages-send`` callers."""
    values = [str(value) for value in args]

    def option(name: str) -> str:
        try:
            index = values.index(name)
        except ValueError:
            return ""
        return values[index + 1] if index + 1 < len(values) else ""

    msg_type = option("--msg-type")
    content = option("--content")
    markdown = option("--markdown")
    card_json = content if msg_type == "interactive" else ""
    text = markdown
    if not text and msg_type == "text" and content:
        try:
            decoded = json.loads(content)
            if isinstance(decoded, dict):
                text = str(decoded.get("text") or "")
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
    if not text and not card_json:
        if configured(root, env=env):
            return BotSendResult(True, False, error="unsupported_payload")
        return BotSendResult(False, False, error="bot_credentials_unavailable")
    return send(
        text=text,
        card_json=card_json,
        user_id=option("--user-id") or (
            option("--receive-id")
            if option("--receive-id-type") == "open_id" else ""
        ),
        chat_id=option("--chat-id"),
        idempotency_key=option("--idempotency-key"),
        root=root,
        env=env,
        opener=opener,
        timeout=timeout,
    )


def bot_open_id(
    root: str | Path | None = None,
    *,
    env: Mapping[str, str] | None = None,
    opener: Callable = urllib.request.urlopen,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    now_epoch: float | None = None,
) -> str:
    """Resolve the bot's open_id without consulting user OAuth or Keychain."""
    env = os.environ if env is None else env
    app_id, app_secret = _credentials(root, env=env)
    if not app_id or not app_secret:
        return ""
    base_url = _base_url(env)
    now_epoch = float(time.time() if now_epoch is None else now_epoch)
    try:
        token = _tenant_token(
            app_id,
            app_secret,
            base_url,
            opener=opener,
            timeout=timeout,
            now_epoch=now_epoch,
        )
        data = _get_json(
            f"{base_url}/open-apis/bot/v3/info",
            headers={"Authorization": f"Bearer {token}"},
            opener=opener,
            timeout=timeout,
        )
        if int(data.get("code") or 0) != 0:
            return ""
        bot = data.get("bot") if isinstance(data.get("bot"), dict) else {}
        return str(bot.get("open_id") or "").strip()
    except (
        OSError,
        RuntimeError,
        ValueError,
        TimeoutError,
        urllib.error.URLError,
        json.JSONDecodeError,
    ):
        return ""
