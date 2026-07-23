"""Authenticated reverse proxy for the local-only Jarvis dashboard.

The gateway is the only process allowed to listen beyond loopback. It always
proxies to 127.0.0.1:3457 and has no route to the admin console on :3456.
"""

from __future__ import annotations

import argparse
import asyncio
import html
import ipaddress
import json
import os
import re
import socket
import ssl
import subprocess
import sys
import time
from collections import defaultdict, deque
from pathlib import Path

from aiohttp import ClientSession, WSMsgType, web

from core.mobile_access import audit_access, consume_pair_code, validate_device_token

BACKEND = "http://127.0.0.1:3457"
COOKIE = "jarvis_device"
SESSION_KEY = web.AppKey("session", ClientSession)
HOP_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host", "cookie",
    "authorization", "content-length",
}
PAIR_FAILURE_LIMIT = 60
PAIR_FAILURE_WINDOW_SECONDS = 10 * 60
_PAIR_FAILURES: dict[str, deque[float]] = defaultdict(deque)
PUBLIC_PROBE_NAMES = {
    ".dev.vars", ".env", ".git", ".aws", "config.json", "secrets.json",
    "wp-config.php", "wp-config.php.bak",
}


def _usable_lan_ip(value: str) -> bool:
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return False
    benchmark_net = ipaddress.ip_network("198.18.0.0/15")
    return bool(ip.version == 4 and ip.is_private and not ip.is_loopback
                and not ip.is_link_local and ip not in benchmark_net)


def detect_lan_ip() -> str:
    # UDP route probing can select a VPN/TUN interface (notably 198.18/15),
    # which is locally valid but unreachable from a phone. On macOS, ask the
    # routing table for the physical default interface and read that address.
    try:
        route = subprocess.run(
            ["/sbin/route", "-n", "get", "default"], capture_output=True,
            text=True, timeout=3, check=False,
        )
        match = re.search(r"^\s*interface:\s*(\S+)", route.stdout, re.M)
        if match:
            address = subprocess.run(
                ["/usr/sbin/ipconfig", "getifaddr", match.group(1)], capture_output=True,
                text=True, timeout=3, check=False,
            ).stdout.strip()
            if _usable_lan_ip(address):
                return address
    except (OSError, subprocess.SubprocessError):
        pass
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("1.1.1.1", 80))
        value = sock.getsockname()[0]
    except OSError:
        value = "127.0.0.1"
    finally:
        sock.close()
    return value if _usable_lan_ip(value) else "127.0.0.1"


def _ca_name_constraints():
    """Name constraints keeping the trusted local CA scoped to private space.

    The phone installs this CA as a trusted root. Without constraints a stolen
    CA key could mint certificates for ANY site; with them the blast radius is
    loopback + RFC1918 + CGNAT (Tailscale) space, which iOS/Safari enforce.
    """
    from cryptography import x509
    permitted = [x509.DNSName("localhost")]
    for net in ("127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12",
                "192.168.0.0/16", "100.64.0.0/10"):
        permitted.append(x509.IPAddress(ipaddress.ip_network(net)))
    return x509.NameConstraints(permitted_subtrees=permitted,
                                excluded_subtrees=None)


def _ca_is_constrained(ca_cert_path: Path) -> bool:
    from cryptography import x509
    try:
        ca_cert = x509.load_pem_x509_certificate(ca_cert_path.read_bytes())
        ca_cert.extensions.get_extension_for_class(x509.NameConstraints)
        return True
    except (OSError, ValueError, x509.ExtensionNotFound):
        return False


def ensure_certificate(host: str, directory: Path) -> tuple[Path, Path, Path]:
    cert_path, key_path = directory / "gateway-cert.pem", directory / "gateway-key.pem"
    ca_cert_path, ca_key_path = directory / "jarvis-mobile-ca.pem", directory / "jarvis-mobile-ca-key.pem"
    ca_download_path = directory / "jarvis-mobile-ca.cer"
    host_path = directory / "gateway-cert-host.txt"
    try:
        host_matches = host_path.read_text(encoding="utf-8").strip() == host
    except OSError:
        host_matches = False
    if (cert_path.exists() and key_path.exists() and ca_cert_path.exists()
            and ca_key_path.exists() and ca_download_path.exists() and host_matches
            and _ca_is_constrained(ca_cert_path)):
        return cert_path, key_path, ca_download_path
    from datetime import datetime, timedelta, timezone
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
    directory.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    if (ca_cert_path.exists() and ca_key_path.exists()
            and not _ca_is_constrained(ca_cert_path)):
        # Rotate an unconstrained legacy CA: keep a backup, then re-issue with
        # name constraints. Paired phones must re-install the new .cer once.
        for path in (ca_cert_path, ca_key_path, ca_download_path):
            if path.exists():
                path.rename(path.with_suffix(path.suffix + ".unconstrained.bak"))
    if ca_cert_path.exists() and ca_key_path.exists():
        ca_cert = x509.load_pem_x509_certificate(ca_cert_path.read_bytes())
        ca_key = serialization.load_pem_private_key(ca_key_path.read_bytes(), password=None)
    else:
        ca_key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
        ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Jarvis Mobile Local CA")])
        ca_cert = (
            x509.CertificateBuilder()
            .subject_name(ca_name)
            .issuer_name(ca_name)
            .public_key(ca_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5))
            .not_valid_after(now + timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.KeyUsage(
                digital_signature=True, content_commitment=False, key_encipherment=False,
                data_encipherment=False, key_agreement=False, key_cert_sign=True,
                crl_sign=True, encipher_only=False, decipher_only=False), critical=True)
            .add_extension(_ca_name_constraints(), critical=True)
            .sign(ca_key, hashes.SHA256())
        )
        ca_key_path.write_bytes(ca_key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption()))
        ca_cert_path.write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
        ca_key_path.chmod(0o600)
    ca_download_path.write_bytes(ca_cert.public_bytes(serialization.Encoding.DER))
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    names = [x509.DNSName("localhost")]
    try:
        names.append(x509.IPAddress(ipaddress.ip_address(host)))
    except ValueError:
        names.append(x509.DNSName(host))
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Jarvis Mobile")]))
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=825))
        .add_extension(x509.SubjectAlternativeName(names), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    key_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()))
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    host_path.write_text(host, encoding="utf-8")
    key_path.chmod(0o600)
    return cert_path, key_path, ca_download_path


def _remote(request: web.Request) -> str:
    peer = request.remote or ""
    try:
        peer_is_loopback = ipaddress.ip_address(peer).is_loopback
    except ValueError:
        peer_is_loopback = False
    if os.environ.get("JARVIS_TRUST_PROXY_HEADERS") == "1" and peer_is_loopback:
        forwarded = request.headers.get("X-Forwarded-For", "").split(",", 1)[0].strip()
        if forwarded:
            return forwarded
    return peer


def _token(request: web.Request) -> str:
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.cookies.get(COOKIE, "")


def _same_origin(request: web.Request) -> bool:
    origin = request.headers.get("Origin", "")
    if not origin:
        return True
    return origin.split("://", 1)[-1].rstrip("/") == request.host


def _looks_like_public_probe(path: str) -> bool:
    parts = [part.lower() for part in str(path).split("/") if part]
    return any(
        part in PUBLIC_PROBE_NAMES
        or part.startswith(".env.")
        or part.startswith("wp-config.php")
        for part in parts
    )


def _security_headers(request: web.Request, response: web.StreamResponse) -> None:
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault(
        "Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    secure = request.headers.get("X-Forwarded-Proto") == "https" or request.secure
    if secure:
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains")


@web.middleware
async def security_headers_middleware(
        request: web.Request, handler) -> web.StreamResponse:
    try:
        response = await handler(request)
    except web.HTTPException as response:
        _security_headers(request, response)
        raise
    _security_headers(request, response)
    return response


def _pair_attempt_allowed(remote: str, now: float | None = None) -> bool:
    timestamp = time.monotonic() if now is None else now
    attempts = _PAIR_FAILURES[remote or "unknown"]
    cutoff = timestamp - PAIR_FAILURE_WINDOW_SECONDS
    while attempts and attempts[0] < cutoff:
        attempts.popleft()
    return len(attempts) < PAIR_FAILURE_LIMIT


def _record_pair_failure(remote: str, now: float | None = None) -> None:
    timestamp = time.monotonic() if now is None else now
    _PAIR_FAILURES[remote or "unknown"].append(timestamp)


def _clear_pair_failures(remote: str) -> None:
    _PAIR_FAILURES.pop(remote or "unknown", None)


def _login_page(
        message: str = "", status: int = 401, pair_code: str = "") -> web.Response:
    error = f'<p class="error">{html.escape(message)}</p>' if message else ""
    if pair_code:
        heading = "确认连接 Jarvis"
        field = (
            f'<input type="hidden" name="code" '
            f'value="{html.escape(pair_code, quote=True)}">'
        )
        button = "确认连接这台设备"
    else:
        heading = "Jarvis 设备配对"
        field = (
            '<input name="code" autocomplete="one-time-code" aria-label="配对码" '
            'placeholder="XXXX-XXXX-XXXX" maxlength="32" autocapitalize="characters" '
            'spellcheck="false" required>'
        )
        button = "连接这台设备"
    page = f"""<!doctype html><html lang="zh-CN"><meta name="viewport"
content="width=device-width,initial-scale=1"><title>Jarvis 设备配对</title>
<style>body{{margin:0;background:#10181d;color:#e6edef;font:16px -apple-system,sans-serif}}
main{{max-width:420px;margin:15vh auto;padding:28px}}h1{{font:700 30px Songti SC,serif}}
input,button{{box-sizing:border-box;width:100%;min-height:48px;margin-top:12px;border-radius:7px}}
input{{padding:0 14px;border:1px solid #41545d;background:#17232a;color:#fff}}
button{{border:0;background:#4aa78f;color:#07110e;font-weight:700}}.error{{color:#e98273}}</style>
<main><h1>{heading}</h1>{error}<form method="post" action="/pair">
{field}<button>{button}</button></form>
<p><a href="/mobile-ca.cer">下载手机信任证书</a></p></main></html>"""
    return web.Response(
        text=page, content_type="text/html", status=status,
        headers={
            "Cache-Control": "no-store",
            "Content-Security-Policy": (
                "default-src 'none'; style-src 'unsafe-inline'; "
                "form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
            ),
        },
    )


async def _complete_pair(request: web.Request, code: str) -> web.Response:
    remote = _remote(request)
    if not _pair_attempt_allowed(remote):
        audit_access("", remote, request.method, "/pair/[redacted]", 429,
                     {"event": "pair_rate_limited"})
        return _login_page("尝试次数过多，请十分钟后重试", status=429)
    result = consume_pair_code(str(code).strip()[:256])
    if not result:
        _record_pair_failure(remote)
        audit_access("", remote, request.method, "/pair/[redacted]", 401,
                     {"event": "pair_failed"})
        return _login_page("配对码无效或已过期")
    _clear_pair_failures(remote)
    try:
        from core import memorial
        memorial.create(
            "mobile-onboarding",
            "手机入口已连接",
            "这台设备已经可以安全查看和处理 Jarvis 事项。",
            preset="fyi",
            dedup_key=f"mobile-onboarding:{result['device_id']}",
        )
    except Exception as exc:
        print(f"mobile onboarding card failed: {exc}", file=sys.stderr)
    response = web.HTTPFound("/")
    secure = request.headers.get("X-Forwarded-Proto") == "https" or request.secure
    response.set_cookie(COOKIE, result["token"], httponly=True, secure=secure,
                        samesite="Strict", max_age=60 * 60 * 24 * 90, path="/")
    audit_access(result["device_id"], remote, request.method,
                 "/pair/[redacted]", 302, {"event": "paired"})
    raise response


async def pair_link(request: web.Request) -> web.Response:
    code = str(request.match_info.get("code", "")).strip()[:256]
    if not code:
        return _login_page()
    # Link previews and security scanners routinely prefetch GET URLs. A GET
    # therefore never consumes a one-time code; only the explicit POST below
    # can pair the device.
    return _login_page(status=200, pair_code=code)


async def pair_form(request: web.Request) -> web.Response:
    data = await request.post()
    return await _complete_pair(request, str(data.get("code", "")))


async def mobile_ca(_request: web.Request) -> web.Response:
    """Serve only the public local CA certificate needed for a trusted PWA."""
    from core.config import Config
    path = Config().jarvis_dir / "data" / "mobile" / "jarvis-mobile-ca.cer"
    if not path.is_file():
        raise web.HTTPNotFound(text="mobile certificate is not ready")
    return web.FileResponse(
        path,
        headers={"Content-Disposition": 'attachment; filename="jarvis-mobile-ca.cer"',
                 "Cache-Control": "no-store"},
    )


async def _proxy_websocket(request: web.Request, device: dict) -> web.StreamResponse:
    client_ws = web.WebSocketResponse(heartbeat=25)
    await client_ws.prepare(request)
    target = BACKEND.replace("http://", "ws://") + request.rel_url.path_qs
    session = request.app[SESSION_KEY]
    backend_cookies = "; ".join(
        f"{key}={value}" for key, value in request.cookies.items() if key != COOKIE)
    ws_headers = {"Origin": BACKEND}
    if backend_cookies:
        ws_headers["Cookie"] = backend_cookies
    async with session.ws_connect(target, heartbeat=25,
                                  headers=ws_headers) as backend_ws:
        async def client_to_backend():
            async for msg in client_ws:
                if msg.type == WSMsgType.TEXT:
                    await backend_ws.send_str(msg.data)
                elif msg.type == WSMsgType.BINARY:
                    await backend_ws.send_bytes(msg.data)
                elif msg.type in {WSMsgType.CLOSE, WSMsgType.ERROR}:
                    break

        async def backend_to_client():
            async for msg in backend_ws:
                if msg.type == WSMsgType.TEXT:
                    await client_ws.send_str(msg.data)
                elif msg.type == WSMsgType.BINARY:
                    await client_ws.send_bytes(msg.data)
                elif msg.type in {WSMsgType.CLOSE, WSMsgType.ERROR}:
                    break

        await asyncio.gather(client_to_backend(), backend_to_client())
    audit_access(device["id"], _remote(request), "WS", request.path, 101)
    return client_ws


async def proxy(request: web.Request) -> web.StreamResponse:
    if _looks_like_public_probe(request.path):
        return web.Response(
            text="Not found", status=404,
            headers={"Cache-Control": "no-store"},
        )
    if not _same_origin(request):
        audit_access("", _remote(request), request.method, request.path, 403,
                     {"event": "origin_rejected"})
        raise web.HTTPForbidden(text="cross-origin request rejected")
    device = validate_device_token(_token(request))
    if not device:
        audit_access("", _remote(request), request.method, request.path, 401)
        return _login_page()
    if request.headers.get("Upgrade", "").lower() == "websocket":
        return await _proxy_websocket(request, device)
    headers = {key: value for key, value in request.headers.items()
               if key.lower() not in HOP_HEADERS}
    if "Origin" in headers:
        headers["Origin"] = BACKEND
    backend_cookies = "; ".join(
        f"{key}={value}" for key, value in request.cookies.items() if key != COOKIE)
    if backend_cookies:
        headers["Cookie"] = backend_cookies
    headers["X-Jarvis-Device"] = device["id"]
    headers["X-Forwarded-For"] = _remote(request)
    target = BACKEND + request.rel_url.path_qs
    body = await request.read()
    session = request.app[SESSION_KEY]
    try:
        async with session.request(request.method, target, data=body, headers=headers,
                                   allow_redirects=False) as upstream:
            response_headers = {key: value for key, value in upstream.headers.items()
                                if key.lower() not in HOP_HEADERS and key.lower() != "set-cookie"}
            response = web.Response(body=await upstream.read(), status=upstream.status,
                                    headers=response_headers)
            for cookie in upstream.headers.getall("Set-Cookie", []):
                response.headers.add("Set-Cookie", cookie)
            status = upstream.status
    except Exception as exc:
        response = web.Response(text="Jarvis dashboard is unavailable", status=502)
        status = 502
        audit_access(device["id"], _remote(request), request.method, request.path,
                     status, {"error": str(exc)[:200]})
        return response
    if not request.path.startswith(("/static/", "/_nicegui/")):
        audit_access(device["id"], _remote(request), request.method, request.path, status)
    return response


async def _startup(app: web.Application) -> None:
    app[SESSION_KEY] = ClientSession(auto_decompress=False)


async def _cleanup(app: web.Application) -> None:
    await app[SESSION_KEY].close()


def create_gateway_app() -> web.Application:
    app = web.Application(
        client_max_size=25 * 1024 * 1024,
        middlewares=[security_headers_middleware],
    )
    app.router.add_get("/mobile-ca.cer", mobile_ca)
    app.router.add_post("/pair", pair_form)
    app.router.add_get("/pair/{code}", pair_link, allow_head=False)
    app.router.add_route("*", "/{tail:.*}", proxy)
    app.on_startup.append(_startup)
    app.on_cleanup.append(_cleanup)
    return app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m dashboard.mobile_gateway")
    parser.add_argument("--host", default="")
    parser.add_argument("--port", type=int, default=3458)
    parser.add_argument("--no-tls", action="store_true")
    args = parser.parse_args(argv)
    host = args.host or detect_lan_ip()
    ssl_context = None
    scheme = "http"
    if not args.no_tls:
        from core.config import Config
        cert, key, _ = ensure_certificate(
            host, Config().jarvis_dir / "data" / "mobile")
        ssl_context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        ssl_context.load_cert_chain(cert, key)
        scheme = "https"
    from core.config import Config
    status_path = Config().jarvis_dir / "mobile_access.json"
    url = f"{scheme}://{host}:{args.port}"
    from core.tailnet import ensure_mobile_access, tailnet_status
    tailnet_mode = os.environ.get("JARVIS_TAILSCALE_MODE", "serve")
    tailnet = (
        ensure_mobile_access(args.port, mode=tailnet_mode)
        if os.environ.get("JARVIS_TAILSCALE_AUTO_SERVE", "0") == "1"
        else tailnet_status(args.port, mode=tailnet_mode)
    )
    status_path.write_text(json.dumps({"host": host, "port": args.port,
                                       "url": url,
                                       "ca_url": f"{url}/mobile-ca.cer",
                                       "trust_required": bool(ssl_context),
                                       "pid": os.getpid(), "tls": bool(ssl_context),
                                       "tailnet": tailnet},
                                      ensure_ascii=False, indent=2), encoding="utf-8")
    from core.deploy import register_runtime
    register_runtime("mobile-gateway")
    # Also bind loopback: tailscale serve proxies from the local node, so the
    # tailnet path (phone off the home network) reaches us via 127.0.0.1.
    hosts = [host] if host == "127.0.0.1" else [host, "127.0.0.1"]
    web.run_app(create_gateway_app(), host=hosts, port=args.port,
                ssl_context=ssl_context, print=None, access_log=None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
