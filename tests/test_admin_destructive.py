"""Destructive-endpoint tests for admin.py (REQ-47) — the five actuators.

Live-verified failure modes these tests pin down:
- stop_task int()'d the two-field lock ('<pid> <token>') → ValueError swallowed
  → kill NEVER fired, yet the live lock was unconditionally deleted (enabling
  concurrent --resume transcript corruption) while reporting {ok: true}.
- EF settings save replaced curated free-text feed_delivery_preference with a
  radio template value, silently destroying hand-tuned routing rules.
- Heartbeat editor accepted nonsense intervals and blind-wrote over concurrent
  edits to HEARTBEAT.md.

Everything runs against an isolated tmp ROOT — never the live repo state.
"""

import http.server
import json
import os
import signal
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request

import pytest

import admin as admin_mod

TEST_TOKEN = "destructive-test-token-0123456789"


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
    mdir.mkdir()
    efdir = tmp_path / "eigenflux"
    efdir.mkdir()
    (tmp_path / "active_sessions.json").write_text("{}")
    (tmp_path / "heartbeat_state.json").write_text("{}")
    (tmp_path / "HEARTBEAT.md").write_text(
        "### test-task\n- interval: 1h\n- prompt: test prompt\n\n"
        "### other-task\n- interval: 30m\n- prompt: other prompt\n")
    (tmp_path / ".admin_token").write_text(TEST_TOKEN + "\n")

    monkeypatch.setattr(admin_mod, "PROJECT_DIR", pdir)
    monkeypatch.setattr(admin_mod, "MEMORY_DIR", mdir)
    monkeypatch.setattr(admin_mod, "SESSION_TRACKER", tmp_path / "active_sessions.json")
    monkeypatch.setattr(admin_mod, "SESSION_SEARCH_PATHS", [pdir])
    monkeypatch.setattr(admin_mod, "ADMIN_TOKEN", "")
    monkeypatch.setattr(admin_mod, "ROOT", tmp_path)
    monkeypatch.setattr(admin_mod, "EIGENFLUX_DIR", efdir)
    monkeypatch.setattr(admin_mod, "HEARTBEAT_TRIGGER_PATH", tmp_path / "hb-trigger")
    admin_mod._post_token_cache.update(path=None, token="")
    admin_mod._ef_status_cache.update(data=None, time=0.0)
    return {"root": tmp_path, "eigenflux": efdir}


@pytest.fixture
def server(isolated_admin):
    port = _free_port()
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), admin_mod.Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    time.sleep(0.1)
    yield f"http://127.0.0.1:{port}", isolated_admin
    srv.shutdown()


def _post(url, data=None, token=TEST_TOKEN, ctype="application/json"):
    body = json.dumps(data).encode() if data is not None else b""
    headers = {}
    if ctype:
        headers["Content-Type"] = ctype
    if token is not None:
        headers["X-Admin-Token"] = token
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except (json.JSONDecodeError, ValueError):
            return e.code, {}


# ── REQ-47① stop_task ────────────────────────────────────────────────

def test_stop_task_kills_two_field_lock_holder_and_unlinks(server):
    """The real lock format is '<pid> <token>' — the old int() parse ALWAYS
    raised and never killed anything. A live dummy holder must actually die,
    and its lock must be removed only after death is confirmed."""
    base, ctx = server
    root = ctx["root"]
    child = subprocess.Popen(["sleep", "300"])
    lock = root / ".session_lock_sess-a"
    lock.write_text(f"{child.pid} {os.getpid()}.1765593600.12345")
    try:
        status, body = _post(f"{base}/api/bot/stop_task")
        assert status == 200
        assert body["ok"] is True
        assert body["killed"] == 1
        assert body["skipped"] == 0
        # The holder is actually dead (SIGKILL), and only then unlinked
        assert child.wait(timeout=5) is not None
        assert not lock.exists()
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()


def test_stop_task_skips_acquiring_lock(server):
    """'acquiring <token>' means the handler has no pid yet — there is nothing
    to kill, and deleting the lock would hand the session to a second writer
    (concurrent claude --resume = transcript corruption)."""
    base, ctx = server
    root = ctx["root"]
    lock = root / ".session_lock_sess-b"
    lock.write_text("acquiring 999.1765593600.42")

    status, body = _post(f"{base}/api/bot/stop_task")
    assert status == 200
    assert body["killed"] == 0
    assert body["skipped"] == 1
    assert lock.exists()                      # NOT deleted
    assert lock.read_text().startswith("acquiring")


def test_stop_task_does_not_unlink_while_holder_alive(server):
    """A holder we cannot kill (pid 1, root-owned → PermissionError) stays
    alive — its lock must survive. The old code deleted it anyway."""
    base, ctx = server
    root = ctx["root"]
    lock = root / ".session_lock_sess-c"
    lock.write_text("1 sometoken.123")        # pid 1: alive, not ours to kill

    status, body = _post(f"{base}/api/bot/stop_task")
    assert status == 200
    assert body["killed"] == 0
    assert body["skipped"] == 1
    assert lock.exists()                      # holder alive ⇒ lock untouched


def test_stop_task_cleans_stale_lock_of_dead_holder(server):
    base, ctx = server
    root = ctx["root"]
    child = subprocess.Popen(["true"])        # exits immediately
    child.wait()
    lock = root / ".session_lock_sess-d"
    lock.write_text(f"{child.pid} tok.456")

    status, body = _post(f"{base}/api/bot/stop_task")
    assert status == 200
    assert body["killed"] == 0
    assert body["cleaned"] == 1               # stale lock removed, no kill claimed
    assert not lock.exists()


def test_stop_task_skips_malformed_lock(server):
    base, ctx = server
    root = ctx["root"]
    lock = root / ".session_lock_sess-e"
    lock.write_text("garbage-not-a-pid token")

    status, body = _post(f"{base}/api/bot/stop_task")
    assert status == 200
    assert body["killed"] == 0
    assert body["skipped"] == 1
    assert lock.exists()


def test_stop_task_requires_token_and_kills_nothing_without_it(server):
    base, ctx = server
    root = ctx["root"]
    child = subprocess.Popen(["sleep", "300"])
    lock = root / ".session_lock_sess-f"
    lock.write_text(f"{child.pid} tok")
    try:
        status, _ = _post(f"{base}/api/bot/stop_task", token=None)
        assert status == 403
        assert child.poll() is None           # still alive
        assert lock.exists()
    finally:
        child.kill()
        child.wait()


# ── REQ-47③ restart ─────────────────────────────────────────────────

def test_restart_writes_trigger_file(server):
    """Admin only writes .restart_trigger; heartbeat_loop is the single
    consumer that spawns restart.sh (REQ-42)."""
    base, ctx = server
    trigger = ctx["root"] / ".restart_trigger"
    assert not trigger.exists()
    status, body = _post(f"{base}/api/bot/restart")
    assert status == 200 and body["ok"] is True
    assert trigger.exists()
    int(trigger.read_text().strip())          # epoch payload parses


# ── REQ-47④ EigenFlux settings ──────────────────────────────────────

FREE_TEXT_PREF = ("Push agent coordination and PE/VC deal signals immediately. "
                  "Batch general AI news. Discard crypto price alerts.")


def _seed_ef(ctx):
    (ctx["eigenflux"] / "user_settings.json").write_text(json.dumps({
        "feed_delivery_preference": FREE_TEXT_PREF,
        "publish_cooldown_minutes": 60,
    }, ensure_ascii=False))


def test_ef_template_overwrite_rejected_without_confirm(server):
    base, ctx = server
    _seed_ef(ctx)
    status, body = _post(f"{base}/api/eigenflux/settings",
                         {"feed_delivery_preference": "Push everything"})
    assert status == 409
    assert body.get("needs_confirm") is True
    assert body.get("current_preference") == FREE_TEXT_PREF
    # the hand-tuned value survived
    saved = json.loads((ctx["eigenflux"] / "user_settings.json").read_text())
    assert saved["feed_delivery_preference"] == FREE_TEXT_PREF


def test_ef_template_overwrite_allowed_with_confirm(server):
    base, ctx = server
    _seed_ef(ctx)
    status, body = _post(f"{base}/api/eigenflux/settings", {
        "feed_delivery_preference": "Digest", "confirm_overwrite": True})
    assert status == 200 and body["ok"] is True
    saved = json.loads((ctx["eigenflux"] / "user_settings.json").read_text())
    assert saved["feed_delivery_preference"] == "Digest"
    assert "confirm_overwrite" not in saved   # flag is not persisted


def test_ef_dirty_field_save_preserves_free_text_pref(server):
    """Submitting ONLY the changed field (the new frontend contract) must
    leave the untouched free-text preference exactly as it was."""
    base, ctx = server
    _seed_ef(ctx)
    status, body = _post(f"{base}/api/eigenflux/settings",
                         {"publish_cooldown_minutes": 30})
    assert status == 200 and body["ok"] is True
    saved = json.loads((ctx["eigenflux"] / "user_settings.json").read_text())
    assert saved["feed_delivery_preference"] == FREE_TEXT_PREF
    assert saved["publish_cooldown_minutes"] == 30


def test_ef_free_text_update_needs_no_confirm(server):
    """A deliberate free-text edit is not a template fallback — no gate."""
    base, ctx = server
    _seed_ef(ctx)
    new_text = FREE_TEXT_PREF + " Also batch fintwit."
    status, body = _post(f"{base}/api/eigenflux/settings",
                         {"feed_delivery_preference": new_text})
    assert status == 200 and body["ok"] is True
    saved = json.loads((ctx["eigenflux"] / "user_settings.json").read_text())
    assert saved["feed_delivery_preference"] == new_text


def test_ef_template_save_over_empty_pref_needs_no_confirm(server):
    base, ctx = server
    (ctx["eigenflux"] / "user_settings.json").write_text("{}")
    status, body = _post(f"{base}/api/eigenflux/settings",
                         {"feed_delivery_preference": "Silent"})
    assert status == 200 and body["ok"] is True


# ── REQ-47⑤ heartbeat editor guards ─────────────────────────────────

def _hb_payload(root, **overrides):
    payload = {
        "name": "test-task",
        "interval_str": "2h",
        "pre": "",
        "post": "",
        "prompt": "updated prompt",
        "mtime": (root / "HEARTBEAT.md").stat().st_mtime,
    }
    payload.update(overrides)
    return payload


def test_heartbeat_task_rejects_nonsense_interval(server):
    base, ctx = server
    before = (ctx["root"] / "HEARTBEAT.md").read_text()
    for bad in ("banana", "10 fortnights", "", "0m", "5x"):
        status, body = _post(f"{base}/api/heartbeat/task",
                             _hb_payload(ctx["root"], interval_str=bad))
        assert status == 400, f"interval {bad!r} must be rejected"
        assert "interval" in body.get("error", "")
    assert (ctx["root"] / "HEARTBEAT.md").read_text() == before  # untouched


def test_heartbeat_task_stale_mtime_409(server):
    base, ctx = server
    hb = ctx["root"] / "HEARTBEAT.md"
    stale = hb.stat().st_mtime
    # Concurrent edit happens after the client loaded the editor
    time.sleep(0.02)
    hb.write_text(hb.read_text() + "\n### sneaky-task\n- interval: 5m\n- prompt: hi\n")
    os.utime(hb, (time.time(), time.time() + 1))  # guarantee a different mtime

    status, body = _post(f"{base}/api/heartbeat/task",
                         _hb_payload(ctx["root"], mtime=stale))
    assert status == 409
    assert body.get("conflict") is True
    assert "sneaky-task" in hb.read_text()         # blind write did NOT happen


def test_heartbeat_task_save_ok_with_fresh_mtime(server):
    base, ctx = server
    hb = ctx["root"] / "HEARTBEAT.md"
    status, body = _post(f"{base}/api/heartbeat/task", _hb_payload(ctx["root"]))
    assert status == 200 and body["ok"] is True
    text = hb.read_text()
    assert "- interval: 2h" in text
    assert "updated prompt" in text
    assert "### other-task" in text                # other tasks preserved
    assert "other prompt" in text
    # fresh mtime returned for the next save
    assert abs(body["mtime"] - hb.stat().st_mtime) < 1e-6


def test_heartbeat_task_unknown_name_400(server):
    base, ctx = server
    status, body = _post(f"{base}/api/heartbeat/task",
                         _hb_payload(ctx["root"], name="ghost-task"))
    assert status == 400
    assert "not found" in body.get("error", "")
