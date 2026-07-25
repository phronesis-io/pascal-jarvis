"""Tests for admin.py API endpoints — read routes, ops-depth routes (REQ-48),
security trio, and RichView sanitization.

Destructive-actuator tests (stop_task, EF settings overwrite, heartbeat
editor guards) live in tests/test_admin_destructive.py.
"""

import http.server
import json
import socket
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

import admin as admin_mod

# Fixed POST token written into the isolated ROOT — every state-changing
# request must carry it as X-Admin-Token (REQ-48 security trio).
TEST_TOKEN = "test-post-token-0123456789abcdef"


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
    efdir = tmp_path / "eigenflux"
    efdir.mkdir()

    monkeypatch.setattr(admin_mod, "PROJECT_DIR", pdir)
    monkeypatch.setattr(admin_mod, "MEMORY_DIR", mdir)
    monkeypatch.setattr(admin_mod, "SESSION_TRACKER", tracker)
    monkeypatch.setattr(admin_mod, "SESSION_SEARCH_PATHS", [pdir])
    monkeypatch.setattr(admin_mod, "ADMIN_TOKEN", "")  # no GET auth for tests
    monkeypatch.setattr(admin_mod, "ROOT", tmp_path)
    monkeypatch.setattr(admin_mod, "EIGENFLUX_DIR", efdir)
    # NEVER the real /tmp trigger — the live heartbeat loop consumes it.
    monkeypatch.setattr(admin_mod, "HEARTBEAT_TRIGGER_PATH", tmp_path / "hb-trigger")

    # Create minimal required files
    (tmp_path / "heartbeat_state.json").write_text("{}")
    (tmp_path / "HEARTBEAT.md").write_text("### test-task\n- interval: 1h\n- prompt: test\n")
    (tmp_path / ".admin_token").write_text(TEST_TOKEN + "\n")

    admin_mod._sessions_meta_cache.update(key=None, data=[], time=0.0)
    admin_mod._lark_chats_cache.update(key=None, data=[], time=0.0)
    admin_mod._post_token_cache.update(path=None, token="")
    admin_mod._ef_status_cache.update(data=None, time=0.0)
    return {"root": tmp_path, "memory": mdir, "project": pdir, "eigenflux": efdir}


@pytest.fixture
def server(isolated_admin):
    port = _free_port()
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), admin_mod.Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    time.sleep(0.1)
    yield f"http://127.0.0.1:{port}", isolated_admin
    srv.shutdown()


def _get(url):
    return json.loads(urllib.request.urlopen(url, timeout=3).read())


def _post(url, data, token=TEST_TOKEN, ctype="application/json", extra_headers=None):
    """POST JSON; returns (status, parsed_body) without raising on 4xx/5xx."""
    body = json.dumps(data).encode() if data is not None else b""
    headers = {}
    if ctype:
        headers["Content-Type"] = ctype
    if token is not None:
        headers["X-Admin-Token"] = token
    headers.update(extra_headers or {})
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        resp = urllib.request.urlopen(req, timeout=3)
        return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except (json.JSONDecodeError, ValueError):
            return e.code, {}


def _post_ok(url, data, **kw):
    status, body = _post(url, data, **kw)
    assert 200 <= status < 300, f"expected 2xx, got {status}: {body}"
    return body


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
    _post_ok(f"{base}/api/memory", {
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
        headers={"X-Admin-Token": TEST_TOKEN},
    )
    resp = urllib.request.urlopen(req, timeout=3)
    assert resp.status == 200
    assert not (mdir / "warm" / "deleteme.md").exists()


def test_memory_delete_requires_token(server):
    """DELETE is state-changing — the security trio covers it too."""
    base, ctx = server
    mdir = ctx["memory"]
    (mdir / "warm" / "keepme.md").write_text("---\nname: KeepMe\ntype: user\n---\nContent")
    req = urllib.request.Request(f"{base}/api/memory/warm/keepme.md", method="DELETE")
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req, timeout=3)
    assert exc.value.code == 403
    assert (mdir / "warm" / "keepme.md").exists()


def test_memory_save_path_traversal_blocked(server):
    base, ctx = server
    status, body = _post(f"{base}/api/memory", {
        "filename": "../../etc/passwd",
        "name": "Hack",
        "description": "...",
        "type": "user",
        "body": "pwned",
    })
    assert status in (400, 403)


# ── Heartbeat status ──

def test_heartbeat_status(server):
    base, ctx = server
    result = _get(f"{base}/api/heartbeat/status")
    assert "tasks" in result
    assert isinstance(result["tasks"], list)
    # mtime is the concurrent-edit token clients echo back on save (REQ-47⑤)
    assert "mtime" in result


# ── Skills ──

def test_skills_list(server):
    base, ctx = server
    result = _get(f"{base}/api/skills")
    assert isinstance(result, list)


# ── Bot status ──

def test_bot_status(server):
    base, ctx = server
    result = _get(f"{base}/api/bot_status")
    assert "alive" in result or "status" in result or "pid" in result


# ── Search ──

def test_search_empty(server):
    base, ctx = server
    result = _get(f"{base}/api/search?q=nonexistent")
    assert isinstance(result, list) or isinstance(result, dict)


# ── Settings ──

def test_settings(server):
    base, ctx = server
    result = _get(f"{base}/api/settings")
    assert isinstance(result, dict)


# ── Heartbeat force (REQ-47②) ──

def test_heartbeat_force_writes_bare_task_name(server):
    base, ctx = server
    body = _post_ok(f"{base}/api/heartbeat/force/test-task", {})
    assert body.get("ok") is True
    assert body.get("task") == "test-task"
    trigger = ctx["root"] / "hb-trigger"
    assert trigger.exists()
    # EXACT bare name: no trailing newline, no whitespace — heartbeat_loop
    # scopes the forced cycle to the file content.
    assert trigger.read_bytes() == b"test-task"


def test_heartbeat_force_unknown_task_404(server):
    base, ctx = server
    status, body = _post(f"{base}/api/heartbeat/force/no-such-task", {})
    assert status == 404
    assert "unknown task" in body.get("error", "")
    assert "test-task" in body.get("known_tasks", [])
    assert not (ctx["root"] / "hb-trigger").exists()


# ── Heartbeat timeline ──

def test_heartbeat_timeline(server):
    base, ctx = server
    result = _get(f"{base}/api/heartbeat-timeline")
    assert isinstance(result, list) or isinstance(result, dict)


# ── Views ──

def test_views_list(server):
    base, ctx = server
    result = _get(f"{base}/api/views")
    assert isinstance(result, list)


# ── Security trio (REQ-48④) ──

def test_post_without_token_403(server):
    base, ctx = server
    status, body = _post(f"{base}/api/bot/restart", {}, token=None)
    assert status == 403
    # And the side effect must NOT have happened
    assert not (ctx["root"] / ".restart_trigger").exists()


def test_post_with_wrong_token_403(server):
    base, ctx = server
    status, body = _post(f"{base}/api/bot/restart", {}, token="wrong-token")
    assert status == 403
    assert not (ctx["root"] / ".restart_trigger").exists()


def test_post_with_wrong_host_403(server):
    base, ctx = server
    status, body = _post(f"{base}/api/bot/restart", {},
                         extra_headers={"Host": "evil.example.com"})
    assert status == 403
    assert not (ctx["root"] / ".restart_trigger").exists()


def test_post_wrong_content_type_415(server):
    base, ctx = server
    status, body = _post(f"{base}/api/memory", {
        "filename": "hot/x.md", "name": "X", "description": "",
        "type": "user", "body": "b",
    }, ctype="text/plain")
    assert status == 415


def test_unknown_api_get_404(server):
    base, ctx = server
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(f"{base}/api/definitely-not-a-route", timeout=3)
    assert exc.value.code == 404
    assert json.loads(exc.value.read())["error"] == "not found"


def test_unknown_api_post_404(server):
    base, ctx = server
    status, body = _post(f"{base}/api/definitely-not-a-route", {})
    assert status == 404


def test_spa_serves_post_token_meta(server):
    """The served HTML embeds the POST token so the frontend can send it."""
    base, ctx = server
    html = urllib.request.urlopen(f"{base}/", timeout=3).read().decode()
    assert TEST_TOKEN in html
    assert "__ADMIN_TOKEN__" not in html


# ── /api/logs (REQ-48①) ──

def test_logs_flags_failure_signatures(server):
    base, ctx = server
    (ctx["root"] / "jarvis.log").write_text(
        "[2026-06-13 08:00:00] [INFO] normal line\n"
        "[2026-06-13 08:01:00] [INFO] Script tasks/repos_sync_pre.sh timed out (60s)\n"
        "[2026-06-13 08:02:00] [INFO] multi-task JSON parse failed for cycle ab12\n"
        "[2026-06-13 08:03:00] [INFO] pre exited with code 1\n"
        "[2026-06-13 08:04:00] [INFO] stderr: traceback follows\n"
        "[2026-06-13 08:05:00] [INFO] another normal line\n")
    result = _get(f"{base}/api/logs?file=jarvis&lines=50")
    assert result["file"] == "jarvis"
    flagged = [l["text"] for l in result["lines"] if l["flagged"]]
    assert len(flagged) == 4
    assert result["flagged_count"] == 4
    assert any("timed out (60s)" in l for l in flagged)
    normal = [l for l in result["lines"] if not l["flagged"]]
    assert len(normal) == 2


def test_logs_grep_filter_keeps_flagging(server):
    base, ctx = server
    (ctx["root"] / "jarvis.log").write_text(
        "[ts] alpha ok\n[ts] beta JSON parse failed\n[ts] beta ok\n")
    result = _get(f"{base}/api/logs?file=jarvis&grep=beta")
    texts = [l["text"] for l in result["lines"]]
    assert len(texts) == 2 and all("beta" in t for t in texts)
    assert sum(1 for l in result["lines"] if l["flagged"]) == 1


def test_logs_lines_limit(server):
    base, ctx = server
    (ctx["root"] / "daemon.log").write_text(
        "\n".join(f"line {i}" for i in range(100)) + "\n")
    result = _get(f"{base}/api/logs?file=daemon&lines=10")
    assert len(result["lines"]) == 10
    assert result["lines"][-1]["text"] == "line 99"


def test_logs_unknown_file_400(server):
    base, ctx = server
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(f"{base}/api/logs?file=passwd", timeout=3)
    assert exc.value.code == 400


# ── /api/sched_events (REQ-48①) ──

def test_sched_events_endpoint(server):
    base, ctx = server
    (ctx["root"] / "sched_events.jsonl").write_text(
        '{"ts": "2026-06-13 08:00:00", "event": "task_spawn", "task": "checkin", "run_id": "r1"}\n'
        '{"ts": "2026-06-13 08:00:40", "event": "task_finish", "task": "checkin", "run_id": "r1", "status": "ok"}\n'
        '{"ts": "2026-06-13 08:01:00", "event": "task_skip", "task": "repos-sync", "run_id": "r2", "reason": "empty_pre"}\n')
    result = _get(f"{base}/api/sched_events")
    assert result["total"] == 3
    assert len(result["events"]) == 3

    filtered = _get(f"{base}/api/sched_events?task=repos-sync")
    assert len(filtered["events"]) == 1
    assert filtered["events"][0]["reason"] == "empty_pre"

    limited = _get(f"{base}/api/sched_events?limit=2")
    assert len(limited["events"]) == 2
    assert limited["events"][-1]["event"] == "task_skip"


# ── /api/queues (REQ-48①) ──

def test_queues_endpoint(server):
    base, ctx = server
    root = ctx["root"]
    (root / "night_queue.jsonl").write_text(
        '{"ts": "2026-06-12 21:04", "text": "' + "x" * 300 + '"}\n')
    (root / "data").mkdir()
    (root / "data" / ".intent_breach_queue.jsonl").write_text(
        '{"id": "int_abc", "name": "提醒：体检", "reason": "3 attempts exhausted"}\n')
    (root / "jobs").mkdir()
    (root / "jobs" / "registry.json").write_text(json.dumps({
        "j-1": {"status": "running", "description": "research task",
                "started_at": "2026-06-13 08:00:00", "pid": 12345},
        "j-2": {"status": "failed", "description": "old", "started_at": "", "pid": None},
    }))
    (root / ".delivery_state.json").write_text('{"consec_fails": 2}')

    result = _get(f"{base}/api/queues")
    assert len(result["night_queue"]) == 1
    nq = result["night_queue"][0]
    assert len(nq["text"]) <= 200          # truncated overview
    assert "age_minutes" in nq             # localtime age computed at API layer
    assert result["breach_queue"][0]["id"] == "int_abc"
    assert result["jobs"]["counts"] == {"running": 1, "failed": 1}
    assert result["jobs"]["running"][0]["id"] == "j-1"
    assert result["delivery_state"]["consec_fails"] == 2


def test_queues_endpoint_empty_files_ok(server):
    base, ctx = server
    result = _get(f"{base}/api/queues")
    assert result["night_queue"] == []
    assert result["breach_queue"] == []
    assert result["jobs"]["running"] == []


# ── /api/intents (REQ-48①) — tmp sqlite, never the real data/jarvis.db ──

@pytest.fixture
def intent_db(tmp_path, monkeypatch):
    """Redirect core.intentions._get_db to a throwaway sqlite (the
    tests/test_intentions.py pattern)."""
    import core.intentions as intentions_mod

    db_path = tmp_path / "intents-db" / "jarvis.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    def _test_get_db():
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    monkeypatch.setattr(intentions_mod, "_get_db", _test_get_db)
    monkeypatch.setattr(intentions_mod, "_table_ready", False)
    intentions_mod._ensure_table()
    return db_path


def test_intents_list_and_stats(server, intent_db):
    from core.intentions import create_intent
    base, ctx = server
    create_intent(name="future ping", trigger_type="date",
                  trigger_config={"datetime": "2999-01-01T09:00:00"})
    result = _get(f"{base}/api/intents")
    assert len(result["intents"]) == 1
    assert result["intents"][0]["name"] == "future ping"
    assert result["stats"]["pending"] == 1
    assert isinstance(result["closure"], dict)

    filtered = _get(f"{base}/api/intents?status=executed")
    assert filtered["intents"] == []


def test_intents_cancel(server, intent_db):
    from core.intentions import create_intent, get_intent
    base, ctx = server
    iid = create_intent(name="cancel me", trigger_type="date",
                        trigger_config={"datetime": "2999-01-01T09:00:00"})
    body = _post_ok(f"{base}/api/intents/cancel", {"id": iid})
    assert body == {"ok": True, "id": iid}
    assert get_intent(iid)["status"] == "cancelled"

    status, body = _post(f"{base}/api/intents/cancel", {"id": "int_nope"})
    assert status == 404


def test_intents_rearm(server, intent_db):
    from core.intentions import create_intent, get_intent, update_intent
    import core.intentions as intentions_mod
    base, ctx = server
    iid = create_intent(name="dead intent", trigger_type="date",
                        trigger_config={"datetime": "2026-01-01T09:00:00"})
    update_intent(iid, status="expired")
    db = intentions_mod._get_db()
    # Stamp a PAST expires_at — without clearing it, cleanup_expired() re-kills
    # the re-armed row (FIX 1/5 shared root, admin side).
    db.execute("UPDATE intentions SET attempt = 3, "
               "expires_at = '2026-01-01T00:00:00' WHERE id = ?", (iid,))
    db.commit()

    body = _post_ok(f"{base}/api/intents/rearm", {"id": iid})
    assert body["ok"] is True and body["id"] == iid
    row = get_intent(iid)
    assert row["status"] == "pending"
    assert row["attempt"] == 0
    assert row["expires_at"] is None  # cleared so cleanup_expired won't re-kill
    target = json.loads(row["trigger_config"])["datetime"]
    assert body["next_fire"] == target
    # fires ~10 minutes out, not in the past
    from datetime import datetime
    from core.timeutil import now_local
    delta = (datetime.fromisoformat(target)
             - now_local().replace(tzinfo=None)).total_seconds()
    assert 8 * 60 < delta < 12 * 60

    status, body = _post(f"{base}/api/intents/rearm", {"id": "int_nope"})
    assert status == 404


def test_intents_rearm_cron_single_update_sets_next_fire(server, intent_db):
    """FIX 5: the cron rearm path must be ONE atomic statement+commit — no
    intermediate pending + stale-past next_fire_at visible to a concurrent
    heartbeat. Verify status/attempt/expires_at/next_fire_at all land and the
    cron expression is preserved (never overwritten with a datetime)."""
    from core.intentions import create_intent, get_intent, update_intent
    import core.intentions as intentions_mod
    base, ctx = server
    iid = create_intent(name="dead cron", trigger_type="cron",
                        trigger_config={"expression": "0 9 * * *"})
    update_intent(iid, status="expired")
    db = intentions_mod._get_db()
    db.execute("UPDATE intentions SET attempt = 3, next_fire_at = "
               "'2026-01-01T09:00:00', expires_at = '2026-01-01T00:00:00' "
               "WHERE id = ?", (iid,))
    db.commit()

    body = _post_ok(f"{base}/api/intents/rearm", {"id": iid})
    assert body["ok"] is True
    row = get_intent(iid)
    assert row["status"] == "pending"
    assert row["attempt"] == 0
    assert row["expires_at"] is None
    # expression preserved; trigger_config NOT replaced by a datetime
    cfg = json.loads(row["trigger_config"])
    assert cfg.get("expression") == "0 9 * * *"
    assert "datetime" not in cfg
    assert row["next_fire_at"] == body["next_fire"]


def test_intents_rearm_interval_preserves_cadence(server, intent_db):
    from datetime import datetime, timedelta
    from core.intentions import create_intent, get_intent, update_intent
    from core.timeutil import now_local
    import core.intentions as intentions_mod

    base, ctx = server
    iid = create_intent(
        name="dead interval",
        trigger_type="interval",
        trigger_config={"seconds": 3600},
    )
    update_intent(iid, status="expired")
    db = intentions_mod._get_db()
    stale = (now_local().replace(tzinfo=None)
             - timedelta(hours=5)).isoformat(timespec="seconds")
    db.execute(
        "UPDATE intentions SET attempt=3,next_fire_at=? WHERE id=?",
        (stale, iid),
    )
    db.commit()

    body = _post_ok(f"{base}/api/intents/rearm", {"id": iid})
    row = get_intent(iid)

    assert json.loads(row["trigger_config"]) == {"seconds": 3600}
    assert row["next_fire_at"] == body["next_fire"]
    delta = (
        datetime.fromisoformat(row["next_fire_at"])
        - now_local().replace(tzinfo=None)
    ).total_seconds()
    assert 58 * 60 < delta < 62 * 60


# ── EigenFlux status cache (REQ-48③) ──

def test_eigenflux_status_cached_60s(server, monkeypatch):
    base, ctx = server
    calls = {"n": 0}

    def fake_status():
        calls["n"] += 1
        return {"authed": False, "error": "stubbed"}

    monkeypatch.setattr(admin_mod, "eigenflux_status", fake_status)
    admin_mod._ef_status_cache.update(data=None, time=0.0)
    r1 = _get(f"{base}/api/eigenflux/status")
    r2 = _get(f"{base}/api/eigenflux/status")
    assert r1 == r2 == {"authed": False, "error": "stubbed"}
    assert calls["n"] == 1  # second hit served from the 60s cache


# ── RichView sanitization (REQ-48⑤) ──

XSS_VIEW = {
    "id": "xsstest01",
    "title": "xss test",
    "created_at": 1765000000.0,
    "expires_at": 9999999999.0,
    "meta": {},
    "sections": [{
        "type": "html",
        "content": (
            '<script>alert("pwned")</script>'
            '<img src=x onerror="alert(1)">'
            '<a href="javascript:alert(2)">click</a> '
            '<a href="https://example.com" onclick="alert(3)">ok-link</a> '
            '<b>bold stays</b><p>para stays</p>'
        ),
    }],
}


def test_sanitize_html_neutralizes_xss():
    out = admin_mod._sanitize_html(XSS_VIEW["sections"][0]["content"])
    assert "<script" not in out
    assert "alert" not in out          # script content + js hrefs + handlers all dropped
    assert "onerror" not in out
    assert "<img" not in out
    assert "javascript:" not in out
    assert "onclick" not in out
    # whitelist survives
    assert "<b>bold stays</b>" in out
    assert "<p>para stays</p>" in out
    assert 'href="https://example.com"' in out
    assert "noopener" in out           # injected rel on allowed links


def test_sanitize_html_escapes_plain_text_and_strips_unknown_tags():
    out = admin_mod._sanitize_html("a < b & c <marquee>boo</marquee>")
    assert "&lt; b &amp; c" in out
    assert "<marquee>" not in out
    assert "boo" in out                # disallowed tag stripped, text kept


def test_richview_html_section_sanitized_in_render():
    # The page template legitimately contains ONE <script> (the chart
    # loader); the payload's script must not survive into the rendered page.
    html = admin_mod._render_richview(XSS_VIEW)
    assert "<script>alert" not in html
    assert 'alert("pwned")' not in html
    assert "onerror" not in html
    assert "javascript:" not in html
    assert "<b>bold stays</b>" in html


def test_richview_route_serves_sanitized(server, monkeypatch, tmp_path):
    import core.richview as richview_mod
    views_dir = tmp_path / "views"
    views_dir.mkdir()
    (views_dir / "xsstest01.json").write_text(json.dumps(XSS_VIEW))
    monkeypatch.setattr(richview_mod, "VIEWS_DIR", views_dir)

    base, ctx = server
    html = urllib.request.urlopen(f"{base}/view/xsstest01", timeout=3).read().decode()
    assert "<script>alert" not in html
    assert "javascript:" not in html
    assert "<b>bold stays</b>" in html


# ── .admin_token permissions (FIX 4) ──────────────────────────────────

def test_admin_token_created_0600(tmp_path, monkeypatch):
    """FIX 4: .admin_token must be created atomically at 0600. write_text()+
    chmod left a window at 0644, and a failed chmod left the leak permanent.
    os.open(O_CREAT, 0o600) closes the window."""
    import os
    monkeypatch.setattr(admin_mod, "ROOT", tmp_path)
    admin_mod._post_token_cache.update(path=None, token="")
    token = admin_mod._get_post_token()
    assert token  # a real token was generated
    tok_path = tmp_path / ".admin_token"
    assert tok_path.exists()
    mode = tok_path.stat().st_mode & 0o777
    assert mode == 0o600, f"expected 0600, got {oct(mode)}"
    # no group/other bits leaked
    assert tok_path.stat().st_mode & 0o077 == 0


def test_admin_token_self_heals_leaked_0644(tmp_path, monkeypatch):
    """FIX 4: a previously-leaked 0644 token file tightens itself to 0600 on
    the next read (the defensive re-chmod in the read-existing branch)."""
    import os
    monkeypatch.setattr(admin_mod, "ROOT", tmp_path)
    admin_mod._post_token_cache.update(path=None, token="")
    tok_path = tmp_path / ".admin_token"
    tok_path.write_text("preexisting-token\n", encoding="utf-8")
    os.chmod(tok_path, 0o644)  # simulate the historical leak
    token = admin_mod._get_post_token()
    assert token == "preexisting-token"
    assert tok_path.stat().st_mode & 0o077 == 0  # group/other bits stripped
