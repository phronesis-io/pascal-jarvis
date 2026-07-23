"""Tailscale Serve status and idempotent mobile-gateway configuration."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path


DEFAULT_SOCKET = Path.home() / ".local/state/tailscaled/tailscaled.sock"
DEFAULT_BINARY = Path("/opt/homebrew/bin/tailscale")
ENABLE_URL_RE = re.compile(r"https://login\.tailscale\.com/f/serve\?[^\s]+")


def _command() -> list[str]:
    binary = os.environ.get("TAILSCALE_BIN") or shutil.which("tailscale")
    if not binary and DEFAULT_BINARY.exists():
        binary = str(DEFAULT_BINARY)
    if not binary:
        return []
    command = [binary]
    socket_path = os.environ.get("TAILSCALE_SOCKET", "").strip()
    if not socket_path and DEFAULT_SOCKET.exists():
        socket_path = str(DEFAULT_SOCKET)
    if socket_path:
        command.append(f"--socket={socket_path}")
    return command


def _run(args: list[str], timeout: float = 8) -> subprocess.CompletedProcess:
    command = _command()
    if not command:
        raise FileNotFoundError("tailscale CLI not found")
    return subprocess.run(
        [*command, *args], capture_output=True, text=True, timeout=timeout,
        check=False,
    )


def _contains_target(value, target: str) -> bool:
    if isinstance(value, str):
        return value.rstrip("/") == target.rstrip("/")
    if isinstance(value, dict):
        return any(_contains_target(item, target) for item in value.values())
    if isinstance(value, list):
        return any(_contains_target(item, target) for item in value)
    return False


def tailnet_status(port: int = 3458) -> dict:
    """Return a stable, display-safe summary of the local tailnet path."""
    target = f"https+insecure://localhost:{int(port)}"
    command = _command()
    if not command:
        return {
            "available": False, "online": False, "served": False,
            "target": target, "url": "", "detail": "Tailscale 未安装",
        }
    try:
        result = _run(["status", "--json"])
        data = json.loads(result.stdout or "{}") if result.returncode == 0 else {}
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError, ValueError) as exc:
        return {
            "available": True, "online": False, "served": False,
            "target": target, "url": "", "detail": f"Tailscale 状态不可用：{exc}",
        }

    self_node = data.get("Self") if isinstance(data, dict) else {}
    self_node = self_node if isinstance(self_node, dict) else {}
    backend_state = str(data.get("BackendState", "") or "")
    online = bool(self_node.get("Online")) and backend_state == "Running"
    dns_name = str(self_node.get("DNSName", "") or "").rstrip(".")
    ips = self_node.get("TailscaleIPs") or []
    ip = str(ips[0]) if ips else ""
    serve_config = {}
    if online:
        try:
            served = _run(["serve", "status", "--json"])
            if served.returncode == 0:
                serve_config = json.loads(served.stdout or "{}")
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError, ValueError):
            serve_config = {}
    is_served = _contains_target(serve_config, target)
    if is_served:
        detail = "Tailnet 入口已就绪"
    elif online:
        detail = "Tailscale 已连接，Serve 尚未启用"
    else:
        detail = "Tailscale 尚未连接"
    return {
        "available": True,
        "online": online,
        "served": is_served,
        "backend_state": backend_state,
        "dns_name": dns_name,
        "ip": ip,
        "url": f"https://{dns_name}" if dns_name else "",
        "target": target,
        "detail": detail,
    }


def ensure_mobile_serve(port: int = 3458, timeout: float = 8) -> dict:
    """Ensure this node serves the authenticated mobile gateway on its tailnet.

    The command is deliberately ``serve``, never ``funnel``: the resulting URL
    is reachable only by devices authenticated to the same tailnet.
    """
    status = tailnet_status(port)
    if not status.get("available") or not status.get("online") or status.get("served"):
        return status
    try:
        result = _run(
            ["serve", "--bg", "--yes", status["target"]], timeout=timeout,
        )
        output = "\n".join(part for part in (result.stdout, result.stderr) if part)
    except subprocess.TimeoutExpired as exc:
        parts = []
        for value in (exc.stdout, exc.stderr):
            if isinstance(value, bytes):
                value = value.decode("utf-8", errors="replace")
            if value:
                parts.append(str(value))
        output = "\n".join(parts)
        result = None
    except (OSError, subprocess.SubprocessError) as exc:
        return {**status, "detail": f"Serve 配置失败：{exc}"}

    enable = ENABLE_URL_RE.search(output)
    if enable:
        return {
            **status,
            "enable_required": True,
            "enable_url": enable.group(0),
            "detail": "需要在 Tailscale 管理页启用 Serve",
        }
    if result is not None and result.returncode != 0:
        message = (output.strip().splitlines() or ["未知错误"])[-1]
        return {**status, "detail": f"Serve 配置失败：{message[:240]}"}
    return tailnet_status(port)
