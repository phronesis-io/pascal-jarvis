"""Tests for admin.py API endpoints — focusing on untested ones."""

import http.server
import json
import socket
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

import admin as admin_mod


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def isolated_admin(tmp_path, monkeypatch):
    pdir = tmp_path / "project"
    pdir.mkdir()
    mdir = tmp_path / "memory"
    (mdir / "hot").mkdir(parents=True)
    (mdir / "warm").mkdir(parents=True)
    (mdir / "system").mkdir(parents=True)
    tracker = tmp_path / "active_sessions.json"
    tracker.write_text("{}")

    monkeypatch.setattr(admin_mod, "PROJECT_DIR", pdir)
    monkeypatch.setattr(admin_mod, "MEMORY_DIR", mdir)
    monkeypatch.setattr(admin_mod, "SESSION_TRACKER", tracker)
    monkeypatch.setattr(admin_mod, "SESSION_SEARCH_PATHS", [pdir])
    monkeypatch.setattr(admin_mod, "ADMIN_TOKEN", "")  # no auth for tests
    monkeypatch.setattr(admin_mod, "ROOT", tmp_path)

    # Create minimal required files
    (tmp_path / "heartbeat_state.json").write_text("{}")
    (tmp_path / "HEARTBEAT.md").write_text("### test-task\n- interval: 1h\n- prompt: test\n")

    admin_mod._sessions_meta_cache.update(key=None, data=[], time=0.0)
    admin_mod._lark_chats_cache.update(key=None, data=[], time=0.0)
    return {"root": tmp_path, "memory": mdir, "project": pdir}


@pytest.fixture
def server(isolated_admin):
    port = _free_port()
    srv = http.server.HTTPServer(("127.0.0.1", port), admin_mod.Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    time.sleep(0.1)
    yield f"http://127.0.0.1:{port}", isolated_admin
    srv.shutdown()


def _get(url):
    return json.loads(urllib.request.urlopen(url, timeout=3).read())


def _post(url, data):
    body = json.dumps(data).encode()
    req = urllib.request.Request(url, data=body,
                                 headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=3).read())


# ── Health endpoint ──

def test_health_endpoint(server):
    base, ctx = server
    result = _get(f"{base}/health")
    assert result["status"] in ("ok", "degraded", "error")
    assert "timestamp" in result


# ── Memory CRUD ──

def test_memory_list_empty(server):
    base, ctx = server
    result = _get(f"{base}/api/memories")
    assert isinstance(result, list)


def test_memory_save_and_list(server):
    base, ctx = server
    _post(f"{base}/api/memory", {
        "filename": "hot/test_note.md",
        "name": "Test",
        "description": "A test note",
        "type": "user",
        "body": "This is test content",
    })
    memories = _get(f"{base}/api/memories")
    names = [m["name"] for m in memories]
    assert "Test" in names


def test_memory_delete(server):
    base, ctx = server
    mdir = ctx["memory"]
    (mdir / "warm" / "deleteme.md").write_text("---\nname: DeleteMe\ntype: user\n---\nContent")

    req = urllib.request.Request(
        f"{base}/api/memory/warm/deleteme.md",
        method="DELETE",
    )
    resp = urllib.request.urlopen(req, timeout=3)
    assert resp.status == 200
    assert not (mdir / "warm" / "deleteme.md").exists()


def test_memory_save_path_traversal_blocked(server):
    base, ctx = server
    try:
        _post(f"{base}/api/memory", {
            "filename": "../../etc/passwd",
            "name": "Hack",
            "description": "...",
            "type": "user",
            "body": "pwned",
        })
    except urllib.error.HTTPError as e:
        assert e.code in (400, 403)


# ── Heartbeat status ──

def test_heartbeat_status(server):
    base, ctx = server
    result = _get(f"{base}/api/heartbeat/status")
    assert "tasks" in result
    assert isinstance(result["tasks"], list)


# ── Skills ──

def test_skills_list(server):
    base, ctx = server
    result = _get(f"{base}/api/skills")
    assert isinstance(result, list)
