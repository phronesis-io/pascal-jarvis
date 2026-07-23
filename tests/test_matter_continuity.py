"""Cross-entry Matter continuity, mobile auth, and closure guard tests."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import types
from pathlib import Path

import pytest
from aiohttp import CookieJar, web
from aiohttp.test_utils import TestClient, TestServer

import dashboard.db as db_module
from core import intentions, memorial
from core.jobs import JobManager
from core.matter_bridge import (
    bind_conversation,
    context_for_conversation,
    get_binding,
    handle_lark_command,
    lark_deep_link,
    record_turn,
)
from core.matter_context import build_context_bundle, render_context_markdown
from core.matter_executor import prepare_handoff, record_completion
from core.matter_router import classify_signal, ingest_signal
from core.matters import (
    MatterConflict,
    create_matter,
    get_matter,
    open_followups,
    update_matter,
)
from core.mobile_access import (
    audit_access,
    consume_pair_code,
    create_pair_code,
    list_devices,
    recent_access,
    register_push,
    revoke_device,
    send_push,
    validate_device_token,
)


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "jarvis.db")
    if db_module._connection is not None:
        db_module._connection.close()
    db_module._connection = None
    intentions._table_ready = False
    monkeypatch.setattr(memorial, "JARVIS_DIR", tmp_path)
    yield
    if db_module._connection is not None:
        db_module._connection.close()
    db_module._connection = None
    intentions._table_ready = False


def test_binding_and_bounded_context_bundle():
    matter = create_matter("移动端统一", summary="同一事项跨入口继续",
                           next_action="接飞书", priority=9)
    binding = bind_conversation("ou_owner", matter["id"],
                                destination_id="ou_owner", chat_type="p2p")

    assert get_binding("ou_owner")["matter_id"] == matter["id"]
    assert "openId=ou_owner" in lark_deep_link(binding)
    rendered = context_for_conversation("ou_owner")
    assert matter["id"] in rendered
    assert "同一事项跨入口继续" in rendered
    assert "raw transcript" in rendered


def test_group_lark_deep_link_uses_open_chat_id():
    assert lark_deep_link({"channel": "lark", "destination_id": "oc_group"}) == (
        "https://applink.feishu.cn/client/chat/open?openChatId=oc_group")


def test_lark_matter_commands_create_switch_and_finish():
    created = handle_lark_command("/matter new 整理 Jarvis", "ou_owner", "ou_owner")
    assert created["handled"] is True
    matter_id = get_binding("ou_owner")["matter_id"]
    current = handle_lark_command("/matter current", "ou_owner")
    assert "整理 Jarvis" in current["reply"]
    done = handle_lark_command("/matter done 已上线", "ou_owner")
    assert "已完成" in done["reply"]
    assert get_matter(matter_id)["outcome"] == "已上线"


def test_model_command_reports_last_actual_provider_and_model():
    assert "首选通道" in handle_lark_command("/model", "ou_owner")["reply"]
    record_turn("ou_owner", "assistant", "回答", provider="GPT fallback",
                model="gpt-test", session_id="sid-1")
    reply = handle_lark_command("当前模型", "ou_owner")["reply"]
    assert "GPT fallback / gpt-test" in reply


def test_intent_link_blocks_close_until_cancelled():
    matter = create_matter("闭环检查")
    intent_id = intentions.create_intent(
        name="发出确认", trigger_type="date",
        trigger_config={"datetime": "2026-08-01T10:00:00+08:00"},
        context={"matter_id": matter["id"]},
    )
    assert open_followups(matter["id"])[0]["entity_id"] == intent_id
    with pytest.raises(MatterConflict) as caught:
        update_matter(matter["id"], status="done")
    assert caught.value.open_items[0]["title"] == "发出确认"
    assert intentions.cancel_intent(intent_id)
    assert open_followups(matter["id"]) == []
    assert update_matter(matter["id"], status="done")["status"] == "done"


def test_force_close_keeps_an_audited_warning():
    matter = create_matter("明确强制完成")
    intentions.create_intent(
        name="仍在等待", trigger_type="date",
        trigger_config={"datetime": "2026-08-01T10:00:00+08:00"},
        matter_id=matter["id"],
    )
    closed = update_matter(matter["id"], status="done", force=True)
    assert closed["status"] == "done"
    assert any(e["event_type"] == "matter_closed_with_followups"
               for e in closed["events"])


def test_memorial_decision_writes_same_matter_ledger():
    matter = create_matter("批红归档")
    mid, _ = memorial.create("test", "方案选择", "请选择",
                             preset="decision", send=False,
                             matter_id=matter["id"])
    assert get_matter(matter["id"])["links"][0]["entity_id"] == mid
    memorial.decide(mid, "approve")
    loaded = get_matter(matter["id"])
    link = next(link for link in loaded["links"] if link["entity_id"] == mid)
    assert link["metadata"]["status"] == "decided"
    assert any(e["event_type"] == "memorial_decided" for e in loaded["events"])


def test_job_lifecycle_links_output_artifact(tmp_path):
    matter = create_matter("后台工作")
    manager = JobManager(tmp_path / "jobs")
    job_id = manager.create_job("ou_owner", "执行检查", matter_id=matter["id"])
    output = Path(manager.get_job(job_id)["output_file"])
    output.write_text("检查通过", encoding="utf-8")
    manager.finish_job(job_id)
    loaded = get_matter(matter["id"])
    assert any(link["entity_type"] == "job"
               and link["metadata"]["status"] == "completed"
               for link in loaded["links"])
    assert any(link["entity_type"] == "artifact" for link in loaded["links"])


def test_cancelled_and_lost_jobs_release_matter_close_guard(tmp_path):
    matter = create_matter("后台终态")
    manager = JobManager(tmp_path / "jobs")
    cancelled = manager.create_job("ou_owner", "取消任务", matter_id=matter["id"])
    assert manager.cancel_job(cancelled)
    assert open_followups(matter["id"]) == []

    lost = manager.create_job("ou_owner", "丢失任务", matter_id=matter["id"])
    manager.update_job(lost, started_at="2020-01-01 00:00:00")
    assert manager.sweep_lost(grace_seconds=0) == [lost]
    assert open_followups(matter["id"]) == []


def test_executor_records_real_model_summary_and_artifact(tmp_path):
    matter = create_matter("Codex 交接")
    transcript = tmp_path / "rollout.jsonl"
    transcript.write_text(json.dumps({
        "type": "event_msg",
        "payload": {"type": "agent_message", "message": "实现完成，测试通过"},
    }, ensure_ascii=False) + "\n", encoding="utf-8")
    artifact = tmp_path / "result.md"
    artifact.write_text("result", encoding="utf-8")
    result = record_completion(
        matter["id"], "codex",
        {"session_id": "codex-1", "title": "交接", "model": "gpt-test",
         "workspace": str(tmp_path), "path": str(transcript)},
        tmp_path, {"result.md"},
    )
    assert result["summary"] == "实现完成，测试通过"
    loaded = get_matter(matter["id"])
    session = next(link for link in loaded["links"] if link["entity_type"] == "session")
    assert session["metadata"]["model"] == "gpt-test"
    assert any(link["title"] == "result.md" for link in loaded["links"])


def test_prepare_handoff_uses_the_same_launcher_for_both_entry_points(
        tmp_path, monkeypatch):
    context_path = tmp_path / "context.md"
    context_path.write_text("bounded context", encoding="utf-8")
    monkeypatch.setattr(
        "core.matter_executor.write_context_bundle",
        lambda _matter_id: context_path,
    )
    matter = create_matter("统一执行入口")
    handoff = prepare_handoff(matter["id"], "codex", actor="test")
    assert handoff["command"] == (
        f"./scripts/jarvis-matter launch {matter['id']} codex")
    assert Path(handoff["context_path"]).exists()
    events = get_matter(matter["id"])["events"]
    event = next(item for item in events
                 if item["event_type"] == "handoff_prepared")
    assert event["payload"]["provider"] == "codex"


def test_router_classifies_and_attaches_only_to_existing_matter():
    matter = create_matter("EigenFlux 路由")
    signal = {"source_id": "eigenflux", "source_type": "cli_stream",
              "event_id": "ef-1", "title": "请确认处理方式",
              "metadata": {"matter_id": matter["id"]}}
    assert classify_signal(signal) == "decision"
    result = ingest_signal(signal)
    assert result == {"route": "decision", "matter_id": matter["id"],
                      "memorial_id": ""}
    assert any(e["event_type"] == "signal_decision"
               for e in get_matter(matter["id"])["events"])
    assert ingest_signal({"source_id": "news", "event_id": "n1",
                          "title": "无关信息"})["matter_id"] == ""


def test_context_bundle_contains_pointers_not_transcript_contents(tmp_path):
    matter = create_matter("隐私边界")
    from core.matters import link_entity
    link_entity(matter["id"], "session", "sid", provider="codex",
                metadata={"path": str(tmp_path / "private.jsonl"),
                          "model": "gpt-test"})
    bundle = build_context_bundle(matter["id"])
    rendered = render_context_markdown(bundle)
    assert "sid" in rendered
    assert "private.jsonl" not in rendered
    assert "private.jsonl" not in json.dumps(bundle)
    assert bundle["privacy"].startswith("This bundle contains summaries")


def test_context_bundle_enforces_limit_and_filters_untrusted_metadata():
    matter = create_matter("有界交接包", summary="当前共识")
    from core.matters import add_event, link_entity
    link_entity(matter["id"], "session", "sid", provider="codex",
                title="工作会话", metadata={"model": "gpt-test",
                                           "raw_transcript": "secret" * 500})
    for index in range(30):
        add_event(matter["id"], "note", f"{index}: " + "长记录" * 500)
    bundle = build_context_bundle(matter["id"], event_limit=30, char_limit=2000)
    rendered = render_context_markdown(bundle)
    assert len(rendered) <= 2000
    assert "raw_transcript" not in json.dumps(bundle)
    assert "gpt-test" in json.dumps(bundle)


def test_mobile_pair_code_is_one_time_and_device_is_revocable():
    pair = create_pair_code("iPhone", ttl_minutes=15)
    device = consume_pair_code(pair["code"])
    assert device and validate_device_token(device["token"])["label"] == "iPhone"
    assert consume_pair_code(pair["code"]) is None
    assert len(list_devices()) == 1
    assert revoke_device(device["device_id"])
    assert validate_device_token(device["token"]) is None


def test_mobile_certificate_chain_is_reusable_and_rotates_leaf_for_host(tmp_path):
    from cryptography import x509
    from cryptography.x509.oid import ExtensionOID
    from dashboard.mobile_gateway import ensure_certificate

    cert, key, ca_download = ensure_certificate("192.168.1.8", tmp_path)
    leaf = x509.load_pem_x509_certificate(cert.read_bytes())
    ca = x509.load_der_x509_certificate(ca_download.read_bytes())
    assert key.stat().st_mode & 0o777 == 0o600
    assert leaf.issuer == ca.subject
    assert ca.extensions.get_extension_for_oid(
        ExtensionOID.BASIC_CONSTRAINTS).value.ca is True
    first_ca = ca_download.read_bytes()
    first_leaf = cert.read_bytes()
    cert2, _, ca2 = ensure_certificate("192.168.1.9", tmp_path)
    assert ca2.read_bytes() == first_ca
    assert cert2.read_bytes() != first_leaf
    san = x509.load_pem_x509_certificate(cert2.read_bytes()).extensions.get_extension_for_oid(
        ExtensionOID.SUBJECT_ALTERNATIVE_NAME).value
    assert "192.168.1.9" in [str(value) for value in san.get_values_for_type(x509.IPAddress)]


def test_mobile_ca_is_name_constrained_to_private_space(tmp_path):
    from cryptography import x509
    from dashboard.mobile_gateway import ensure_certificate

    _, _, ca_download = ensure_certificate("192.168.1.8", tmp_path)
    ca = x509.load_der_x509_certificate(ca_download.read_bytes())
    constraints = ca.extensions.get_extension_for_class(x509.NameConstraints)
    assert constraints.critical is True
    permitted = constraints.value.permitted_subtrees
    nets = [str(v.value) for v in permitted if isinstance(v, x509.IPAddress)]
    assert "192.168.0.0/16" in nets
    assert "100.64.0.0/10" in nets  # Tailscale CGNAT space
    assert "localhost" in [v.value for v in permitted if isinstance(v, x509.DNSName)]


def test_mobile_unconstrained_legacy_ca_is_rotated_with_backup(tmp_path):
    from cryptography import x509
    from dashboard import mobile_gateway
    from dashboard.mobile_gateway import ensure_certificate

    # Forge a legacy (unconstrained) CA by generating one with constraints
    # stripped, mimicking the pre-rotation deployment.
    real_constraints = mobile_gateway._ca_name_constraints
    mobile_gateway._ca_name_constraints = lambda: (_ for _ in ()).throw(AssertionError)
    try:
        import cryptography.x509 as x509mod
        original_add = x509mod.CertificateBuilder.add_extension

        def skip_constraints(self, ext, critical):
            if isinstance(ext, x509mod.NameConstraints):
                return self
            return original_add(self, ext, critical=critical)

        x509mod.CertificateBuilder.add_extension = skip_constraints
        mobile_gateway._ca_name_constraints = real_constraints
        ensure_certificate("192.168.1.8", tmp_path)
    finally:
        x509mod.CertificateBuilder.add_extension = original_add
        mobile_gateway._ca_name_constraints = real_constraints
    assert not mobile_gateway._ca_is_constrained(tmp_path / "jarvis-mobile-ca.pem")

    _, _, ca_download = ensure_certificate("192.168.1.8", tmp_path)
    assert mobile_gateway._ca_is_constrained(tmp_path / "jarvis-mobile-ca.pem")
    assert (tmp_path / "jarvis-mobile-ca.pem.unconstrained.bak").exists()
    ca = x509.load_der_x509_certificate(ca_download.read_bytes())
    ca.extensions.get_extension_for_class(x509.NameConstraints)


def test_mobile_lan_detection_ignores_tunnel_benchmark_address(monkeypatch):
    from dashboard import mobile_gateway

    def fake_run(command, **_kwargs):
        if command[0].endswith("/route"):
            return types.SimpleNamespace(stdout="  interface: en0\n")
        return types.SimpleNamespace(stdout="192.168.13.108\n")

    monkeypatch.setattr(mobile_gateway.subprocess, "run", fake_run)
    assert mobile_gateway.detect_lan_ip() == "192.168.13.108"
    assert mobile_gateway._usable_lan_ip("198.18.0.1") is False


def test_tailnet_status_detects_the_mobile_serve_target(monkeypatch):
    from core import tailnet

    def fake_run(args, timeout=8):
        if args == ["status", "--json"]:
            payload = {
                "BackendState": "Running",
                "Self": {
                    "Online": True,
                    "DNSName": "jarvis.test.ts.net.",
                    "TailscaleIPs": ["100.64.0.8"],
                },
            }
        else:
            payload = {
                "Web": {
                    "jarvis.test.ts.net:443": {
                        "Handlers": {
                            "/": {"Proxy": "https+insecure://localhost:3458"}
                        }
                    }
                }
            }
        return types.SimpleNamespace(
            returncode=0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr(tailnet, "_command", lambda: ["tailscale"])
    monkeypatch.setattr(tailnet, "_run", fake_run)
    status = tailnet.tailnet_status()
    assert status["served"] is True
    assert status["ready"] is True
    assert status["funnel"] is False
    assert status["url"] == "https://jarvis.test.ts.net"


def test_tailnet_status_requires_allow_funnel_for_public_mode(monkeypatch):
    from core import tailnet

    serve_config = {
        "Web": {
            "jarvis.test.ts.net:443": {
                "Handlers": {
                    "/": {"Proxy": "https+insecure://localhost:3458"}
                }
            }
        }
    }

    def fake_run(args, timeout=8):
        if args == ["status", "--json"]:
            payload = {
                "BackendState": "Running",
                "Self": {
                    "Online": True,
                    "DNSName": "jarvis.test.ts.net.",
                    "TailscaleIPs": ["100.64.0.8"],
                },
            }
        else:
            payload = serve_config
        return types.SimpleNamespace(
            returncode=0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr(tailnet, "_command", lambda: ["tailscale"])
    monkeypatch.setattr(tailnet, "_run", fake_run)
    private = tailnet.tailnet_status(mode="funnel")
    assert private["served"] is True
    assert private["ready"] is False
    assert private["funnel"] is False

    serve_config["AllowFunnel"] = {"jarvis.test.ts.net:443": True}
    public = tailnet.tailnet_status(mode="funnel")
    assert public["ready"] is True
    assert public["funnel"] is True
    assert "公网 HTTPS" in public["detail"]


def test_tailnet_serve_reports_the_admin_enable_url(monkeypatch):
    from core import tailnet

    calls = []

    def fake_run(args, timeout=8):
        calls.append(args)
        if args == ["status", "--json"]:
            payload = {
                "BackendState": "Running",
                "Self": {"Online": True, "DNSName": "jarvis.test.ts.net."},
            }
            return types.SimpleNamespace(
                returncode=0, stdout=json.dumps(payload), stderr="")
        if args == ["serve", "status", "--json"]:
            return types.SimpleNamespace(returncode=0, stdout="{}", stderr="")
        return types.SimpleNamespace(
            returncode=1, stdout="",
            stderr=("Serve is not enabled.\n"
                    "https://login.tailscale.com/f/serve?node=test"),
        )

    monkeypatch.setattr(tailnet, "_command", lambda: ["tailscale"])
    monkeypatch.setattr(tailnet, "_run", fake_run)
    status = tailnet.ensure_mobile_serve()
    assert status["enable_required"] is True
    assert status["enable_url"].endswith("node=test")
    assert calls[-1] == [
        "serve", "--bg", "--yes", "https+insecure://localhost:3458"]


def test_tailnet_funnel_reports_enable_url_and_uses_funnel_command(monkeypatch):
    from core import tailnet

    calls = []

    def fake_run(args, timeout=8):
        calls.append(args)
        if args == ["status", "--json"]:
            payload = {
                "BackendState": "Running",
                "Self": {"Online": True, "DNSName": "jarvis.test.ts.net."},
            }
            return types.SimpleNamespace(
                returncode=0, stdout=json.dumps(payload), stderr="")
        if args == ["serve", "status", "--json"]:
            payload = {
                "Web": {
                    "jarvis.test.ts.net:443": {
                        "Handlers": {
                            "/": {"Proxy": "https+insecure://localhost:3458"}
                        }
                    }
                }
            }
            return types.SimpleNamespace(
                returncode=0, stdout=json.dumps(payload), stderr="")
        return types.SimpleNamespace(
            returncode=1, stdout="",
            stderr=("Funnel is not enabled.\n"
                    "https://login.tailscale.com/f/funnel?node=test"),
        )

    monkeypatch.setattr(tailnet, "_command", lambda: ["tailscale"])
    monkeypatch.setattr(tailnet, "_run", fake_run)
    status = tailnet.ensure_mobile_funnel()
    assert status["enable_required"] is True
    assert status["enable_url"].endswith("node=test")
    assert calls[-1] == [
        "funnel", "--bg", "--yes", "https+insecure://localhost:3458"]


def test_mobile_audit_does_not_trust_spoofed_forwarded_address(monkeypatch):
    from dashboard import mobile_gateway
    request = types.SimpleNamespace(
        headers={"X-Forwarded-For": "203.0.113.9"}, remote="192.168.13.20")
    assert mobile_gateway._remote(request) == "192.168.13.20"
    monkeypatch.setenv("JARVIS_TRUST_PROXY_HEADERS", "1")
    assert mobile_gateway._remote(request) == "192.168.13.20"
    request.remote = "127.0.0.1"
    assert mobile_gateway._remote(request) == "203.0.113.9"


def test_mobile_audit_and_push_subscription_validation(monkeypatch):
    audit_access("dev_x", "10.0.0.2", "GET", "/matters", 200)
    assert recent_access(1)[0]["device_id"] == "dev_x"
    with pytest.raises(ValueError):
        register_push("dev_x", {"endpoint": "http://bad", "keys": {}})
    register_push("dev_x", {
        "endpoint": "https://push.example.test/one",
        "keys": {"p256dh": "abc", "auth": "def"},
    })
    class PushError(Exception):
        response = None

    monkeypatch.setitem(sys.modules, "pywebpush", types.SimpleNamespace(
        WebPushException=PushError,
        webpush=lambda **_: (_ for _ in ()).throw(PushError("offline")),
    ))
    result = send_push("title", "body")
    assert result["failed"] == 1


def test_gateway_requires_pairing_and_forwards_only_to_configured_backend(monkeypatch):
    from dashboard import mobile_gateway

    async def scenario():
        backend_app = web.Application()

        async def backend(request):
            return web.json_response({
                "path": request.path,
                "origin": request.headers.get("Origin", ""),
                "device": request.headers.get("X-Jarvis-Device", ""),
            })

        backend_app.router.add_route("*", "/{tail:.*}", backend)
        backend_server = TestServer(backend_app)
        await backend_server.start_server()
        monkeypatch.setattr(mobile_gateway, "BACKEND",
                            str(backend_server.make_url("/")).rstrip("/"))
        client = TestClient(TestServer(mobile_gateway.create_gateway_app()),
                            cookie_jar=CookieJar(unsafe=True))
        await client.start_server()
        try:
            denied = await client.get("/matters")
            assert denied.status == 401
            assert denied.headers["X-Frame-Options"] == "DENY"
            assert denied.headers["Referrer-Policy"] == "no-referrer"
            assert "form-action 'self'" in denied.headers["Content-Security-Policy"]
            pair = create_pair_code("test phone")
            paired = await client.get(f"/pair/{pair['code']}", allow_redirects=False)
            assert paired.status == 302
            allowed = await client.get("/matters")
            assert allowed.status == 200
            payload = await allowed.json()
            assert payload["path"] == "/matters"
            assert payload["device"].startswith("dev_")
            rejected = await client.post(
                "/api/test", headers={"Origin": "https://attacker.example"})
            assert rejected.status == 403
        finally:
            await client.close()
            await backend_server.close()

    asyncio.run(scenario())


def test_gateway_pair_form_and_failure_limit(monkeypatch):
    from dashboard import mobile_gateway

    async def scenario():
        mobile_gateway._PAIR_FAILURES.clear()
        monkeypatch.setattr(mobile_gateway, "PAIR_FAILURE_LIMIT", 2)
        client = TestClient(TestServer(mobile_gateway.create_gateway_app()),
                            cookie_jar=CookieJar(unsafe=True))
        await client.start_server()
        try:
            first = await client.post("/pair", data={"code": "invalid"})
            second = await client.post("/pair", data={"code": "invalid"})
            limited = await client.post("/pair", data={"code": "invalid"})
            assert first.status == 401
            assert second.status == 401
            assert limited.status == 429
            assert limited.headers["Cache-Control"] == "no-store"

            mobile_gateway._PAIR_FAILURES.clear()
            pair = create_pair_code("form phone")
            paired = await client.post(
                "/pair", data={"code": pair["code"]}, allow_redirects=False)
            assert paired.status == 302
        finally:
            mobile_gateway._PAIR_FAILURES.clear()
            await client.close()

    asyncio.run(scenario())


def test_gateway_backend_constant_never_points_at_admin():
    from dashboard.mobile_gateway import BACKEND
    assert BACKEND == "http://127.0.0.1:3457"
    assert "3456" not in BACKEND


def test_web_decision_updates_every_delivered_lark_card(monkeypatch):
    from core import memorial_thread

    calls = []
    monkeypatch.setattr(memorial_thread, "sent_message_ids",
                        lambda _: ["om_first", "om_second"])
    monkeypatch.setattr(subprocess, "run", lambda command, **kwargs: (
        calls.append((command, kwargs)) or types.SimpleNamespace(
            returncode=0, stdout="", stderr="")))
    memorial._sync_lark_card("m_test", {"schema": "2.0"})
    assert [call[0][3] for call in calls] == [
        "/open-apis/im/v1/messages/om_first",
        "/open-apis/im/v1/messages/om_second",
    ]
    assert all(call[0][0:3] == ["lark-cli", "api", "PATCH"] for call in calls)
