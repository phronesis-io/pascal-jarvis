"""Tests for admin.py — route coverage + cache + auth.

Admin reads its config at import time from ./jarvis.yaml. For tests that
need isolation from the live project, we monkeypatch module-level globals.
"""

import http.server
import json
import socket
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import pytest

import admin as admin_mod
from core.session import NAMESPACE


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def isolated_paths(tmp_path, monkeypatch):
    """Redirect admin's project/memory/tracker paths to tmp_path."""
    pdir = tmp_path / "project"
    pdir.mkdir()
    mdir = tmp_path / "memory"
    mdir.mkdir()
    tracker = tmp_path / "active_sessions.json"

    monkeypatch.setattr(admin_mod, "PROJECT_DIR", pdir)
    monkeypatch.setattr(admin_mod, "MEMORY_DIR", mdir)
    monkeypatch.setattr(admin_mod, "SESSION_TRACKER", tracker)
    monkeypatch.setattr(admin_mod, "SESSION_SEARCH_PATHS", [pdir])
    # Reset caches so previous test state doesn't bleed in
    admin_mod._sessions_meta_cache.update(key=None, data=[], time=0.0)
    admin_mod._lark_chats_cache.update(key=None, data=[], time=0.0)
    return {"project": pdir, "memory": mdir, "tracker": tracker}


def test_empty_paths_no_crash(isolated_paths):
    assert admin_mod.sessions_meta() == []
    assert admin_mod.lark_chats() == []


def test_sessions_meta_returns_data(isolated_paths):
    pdir = isolated_paths["project"]
    (pdir / "abc.jsonl").write_text(json.dumps({
        "type": "user",
        "timestamp": "2026-04-16T10:00:00",
        "message": {"role": "user", "content": "hello world"}
    }) + "\n")

    # Invalidate cache (directory fingerprint changes)
    admin_mod._sessions_meta_cache.update(key=None, data=[], time=0.0)
    metas = admin_mod.sessions_meta()
    assert len(metas) == 1
    assert metas[0]["id"] == "abc"
    assert "hello world" in metas[0]["first_prompt"]


def test_lark_chats_walks_all_counters(isolated_paths):
    tracker = isolated_paths["tracker"]
    pdir = isolated_paths["project"]

    tracker_data = {
        "ou_test": {
            "session_id": str(uuid.uuid5(NAMESPACE, "ou_test-3")),
            "counter": 3,
        }
    }
    tracker.write_text(json.dumps(tracker_data))

    # Create all three session files
    for i in range(1, 4):
        sid = str(uuid.uuid5(NAMESPACE, f"ou_test-{i}"))
        (pdir / f"{sid}.jsonl").write_text(json.dumps({
            "type": "user",
            "timestamp": f"2026-04-1{i}T10:00:00",
            "message": {"role": "user", "content": f"msg in session {i}"}
        }) + "\n")

    admin_mod._lark_chats_cache.update(key=None, data=[], time=0.0)
    chats = admin_mod.lark_chats()
    assert len(chats) == 1
    assert chats[0]["conv_key"] == "ou_test"
    assert chats[0]["kind"] == "p2p"
    assert chats[0]["current_counter"] == 3
    assert len(chats[0]["sessions"]) == 3
    assert chats[0]["sessions"][-1]["is_active"] is True


def test_classifies_group_vs_p2p(isolated_paths):
    tracker = isolated_paths["tracker"]
    tracker.write_text(json.dumps({
        "oc_group": {"session_id": str(uuid.uuid5(NAMESPACE, "oc_group-1")), "counter": 1},
        "ou_dm":    {"session_id": str(uuid.uuid5(NAMESPACE, "ou_dm-1")),    "counter": 1},
    }))
    admin_mod._lark_chats_cache.update(key=None, data=[], time=0.0)

    chats = admin_mod.lark_chats()
    kinds = [c["kind"] for c in chats]
    assert "p2p" in kinds and "group" in kinds
    assert chats[0]["kind"] == "p2p"  # sort puts p2p first


def test_lark_chats_missing_session_files_still_returns(isolated_paths):
    """When no session files exist on disk, the chat entry is returned but
    sessions list is empty (leading non-existing sessions are skipped)."""
    tracker = isolated_paths["tracker"]
    tracker.write_text(json.dumps({
        "ou_test": {"session_id": str(uuid.uuid5(NAMESPACE, "ou_test-2")), "counter": 2}
    }))
    # No session files written to project dir
    admin_mod._lark_chats_cache.update(key=None, data=[], time=0.0)
    chats = admin_mod.lark_chats()
    # Chat entry exists but sessions list is empty (no files on disk)
    assert len(chats) == 1
    assert chats[0]["sessions"] == []


def test_auth_blocks_without_token(isolated_paths, monkeypatch):
    monkeypatch.setattr(admin_mod, "ADMIN_TOKEN", "secret123")
    port = _free_port()
    server = http.server.HTTPServer(("127.0.0.1", port), admin_mod.Handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        time.sleep(0.1)
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/memories", timeout=2)
        assert exc.value.code == 401

        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/memories",
                                     headers={"X-Admin-Token": "secret123"})
        resp = urllib.request.urlopen(req, timeout=2)
        assert resp.status == 200

        resp = urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/memories?token=secret123", timeout=2)
        assert resp.status == 200
    finally:
        server.shutdown()
