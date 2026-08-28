"""Codex app-server boundary tests use a fake executable, never real state."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.codex_app_server import CodexAppServerClient, CodexAppServerError


def _fake_codex(path: Path, body: str) -> Path:
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        + body,
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def test_client_initializes_notifies_and_returns_typed_result(tmp_path):
    binary = _fake_codex(
        tmp_path / "codex",
        """
initialized = False
for line in sys.stdin:
    request = json.loads(line)
    if request.get("method") == "initialize":
        print(json.dumps({"id": request["id"], "result": {"userAgent": "fake/1"}}), flush=True)
    elif request.get("method") == "initialized":
        initialized = True
    elif request.get("method") == "thread/list":
        print(json.dumps({"id": request["id"], "result": {"data": [{"id": "t1"}], "initialized": initialized}}), flush=True)
""",
    )

    with CodexAppServerClient(str(binary), timeout=2) as client:
        result = client.request("thread/list", {"limit": 1})

    assert client.user_agent == "fake/1"
    assert result == {"data": [{"id": "t1"}], "initialized": True}


def test_client_redacts_server_message_from_exception(tmp_path):
    binary = _fake_codex(
        tmp_path / "codex",
        """
for line in sys.stdin:
    request = json.loads(line)
    if request.get("method") == "initialize":
        print(json.dumps({"id": request["id"], "result": {"userAgent": "fake/1"}}), flush=True)
    elif request.get("id"):
        print(json.dumps({"id": request["id"], "error": {"code": 401, "message": "Bearer secret-token"}}), flush=True)
""",
    )

    with CodexAppServerClient(str(binary), timeout=2) as client:
        with pytest.raises(CodexAppServerError) as caught:
            client.request("thread/read", {"threadId": "missing"})

    assert "code=401" in str(caught.value)
    assert "secret-token" not in str(caught.value)
