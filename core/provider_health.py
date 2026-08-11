"""Observable health checks for Jarvis' model-provider fallback chain.

Configuration presence is not proof that a provider can answer.  This module
runs a tiny, bounded canary through each configured route and persists only
status metadata.  Credentials stay in environment variables or HTTP headers
and are never written to the state file or child-process argv.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .claude_bin import resolve_claude_bin
from .codex_fallback import resolve_codex_bin
from .config import Config


CANARY_MARKER = "JARVIS_CANARY_OK"
DEFAULT_TIMEOUT = 45
STATE_FILE = "data/provider_health.json"
_SECRET_RE = re.compile(
    r"(sk-[A-Za-z0-9_\-]{8,}|Bearer\s+\S+"
    r"|\b(?:token|secret|api[_-]?key|password)\b\s*[=:]\s*\S+)",
    re.IGNORECASE,
)


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _root(root: str | Path | None = None) -> Path:
    return Path(root or os.environ.get("JARVIS_DIR") or Path.cwd()).resolve()


def _safe_error(value: object, limit: int = 240) -> str:
    line = next(
        (part.strip() for part in str(value or "").splitlines() if part.strip()),
        "",
    )
    return _SECRET_RE.sub("[redacted]", line)[:limit]


def provider_specs(config: Config) -> list[dict[str, Any]]:
    claude = config.claude
    codex = config.codex
    openai = config.openai
    main_model = str(claude.get("main_model") or "opus")
    return [
        {
            "id": "primary",
            "label": "Claude primary",
            "kind": "claude",
            "enabled": True,
            "configured": True,
            "model": main_model,
            "token": "",
            "base_url": "",
        },
        {
            "id": "backup1",
            "label": "Claude backup",
            "kind": "claude",
            "enabled": bool(claude.get("backup_enabled", True)),
            "configured": bool(
                claude.get("backup_auth_token")
                and claude.get("backup_base_url")
            ),
            "model": str(claude.get("backup_model") or main_model),
            "token": str(claude.get("backup_auth_token") or ""),
            "base_url": str(claude.get("backup_base_url") or ""),
        },
        {
            "id": "backup2",
            "label": "Claude backup2",
            "kind": "claude",
            "enabled": bool(claude.get("backup2_enabled", False)),
            "configured": bool(
                claude.get("backup2_auth_token")
                and claude.get("backup2_base_url")
            ),
            "model": str(claude.get("backup2_model") or main_model),
            "token": str(claude.get("backup2_auth_token") or ""),
            "base_url": str(claude.get("backup2_base_url") or ""),
        },
        {
            "id": "codex",
            "label": "Codex fallback",
            "kind": "codex",
            "enabled": bool(codex.get("fallback_enabled", True)),
            "configured": bool(
                codex.get("binary") or resolve_codex_bin()
            ),
            "model": str(codex.get("fallback_model") or "gpt-5.5"),
            "binary": str(codex.get("binary") or ""),
        },
        {
            "id": "openai",
            "label": "GPT fallback",
            "kind": "openai",
            "enabled": bool(openai.get("fallback_enabled", True)),
            "configured": bool(
                openai.get("api_key") or os.environ.get("OPENAI_API_KEY")
            ),
            "model": str(openai.get("fallback_model") or "gpt-5.5"),
            "token": str(
                openai.get("api_key") or os.environ.get("OPENAI_API_KEY") or ""
            ),
            "base_url": str(
                openai.get("base_url") or "https://api.openai.com/v1"
            ),
            "user_agent": str(openai.get("user_agent") or ""),
        },
    ]


def _base_result(spec: dict[str, Any]) -> dict[str, Any]:
    if not spec["enabled"]:
        status = "disabled"
        detail = "disabled in jarvis.yaml"
    elif not spec["configured"]:
        status = "unconfigured"
        detail = "credentials or endpoint missing"
    else:
        status = "not_run"
        detail = "configured; no canary result yet"
    return {
        "id": spec["id"],
        "label": spec["label"],
        "status": status,
        "configured": bool(spec["configured"]),
        "enabled": bool(spec["enabled"]),
        "requested_model": spec["model"],
        "actual_model": "",
        "model_source": "",
        "checked_at": "",
        "latency_ms": None,
        "detail": detail,
    }


def _probe_claude(
    spec: dict[str, Any],
    *,
    root: Path,
    timeout: int,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> dict[str, Any]:
    cmd = [
        resolve_claude_bin(),
        "--no-session-persistence",
        "--disable-slash-commands",
        "--permission-mode",
        "dontAsk",
        "--tools",
        "",
        "--mcp-config",
        '{"mcpServers":{}}',
        "--strict-mcp-config",
        "--output-format",
        "json",
        "--model",
        spec["model"],
        "-p",
        (
            f"Provider health canary. Reply with exactly {CANARY_MARKER} "
            "and nothing else. Do not use tools."
        ),
    ]
    env = os.environ.copy()
    if spec["id"] != "primary":
        env["ANTHROPIC_AUTH_TOKEN"] = spec["token"]
        env["ANTHROPIC_BASE_URL"] = spec["base_url"]
    started = time.monotonic()
    try:
        completed = runner(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(root),
            env=env,
        )
    except subprocess.TimeoutExpired:
        return {
            "status": "unhealthy",
            "detail": f"canary timed out after {timeout}s",
            "latency_ms": round((time.monotonic() - started) * 1000),
        }
    except Exception as exc:
        return {
            "status": "unhealthy",
            "detail": _safe_error(f"{type(exc).__name__}: {exc}"),
            "latency_ms": round((time.monotonic() - started) * 1000),
        }
    latency = round((time.monotonic() - started) * 1000)
    output = completed.stdout or ""
    actual_model = ""
    api_status = ""
    if output.strip().startswith("{"):
        try:
            payload = json.loads(output)
            output = str(payload.get("result") or payload.get("output_text") or "")
            actual_model = str(payload.get("model") or "")
            status_code = payload.get("api_error_status")
            if payload.get("is_error") and status_code:
                api_status = f"HTTP {status_code}"
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
    if completed.returncode == 0 and output.strip() == CANARY_MARKER:
        return {
            "status": "healthy",
            "detail": "bounded canary answered",
            "latency_ms": latency,
            "actual_model": actual_model or spec["model"],
            "model_source": "response" if actual_model else "requested",
        }
    # The CLI writes advisory notices to stderr even when the real failure is
    # in the JSON result — a relay token legitimately triggers "claude.ai
    # connectors are disabled". Preferring stderr made a 403 authentication
    # failure on the backup relay read as a connector notice, which points an
    # operator at the wrong thing. The reported reason is the run's own result
    # first; stderr only when the result says nothing.
    error = _safe_error(output) or _safe_error(completed.stderr)
    if api_status and not error.startswith(api_status):
        error = f"{api_status}: {error}" if error else api_status
    return {
        "status": "unhealthy",
        "detail": error or f"canary exited {completed.returncode}",
        "latency_ms": latency,
    }


def _probe_openai(
    spec: dict[str, Any],
    *,
    timeout: int,
    caller: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    from .openai_fallback import call_openai, extract_text

    api_call = caller or call_openai
    payload = {
        "model": spec["model"],
        "instructions": "Return the exact canary marker and do not use tools.",
        "input": CANARY_MARKER,
        "max_output_tokens": 32,
    }
    started = time.monotonic()
    try:
        response = api_call(
            payload,
            spec["token"],
            spec["base_url"],
            timeout,
            spec.get("user_agent", ""),
        )
        text = extract_text(response)
    except Exception as exc:
        return {
            "status": "unhealthy",
            "detail": _safe_error(f"{type(exc).__name__}: {exc}"),
            "latency_ms": round((time.monotonic() - started) * 1000),
        }
    latency = round((time.monotonic() - started) * 1000)
    if text.strip() != CANARY_MARKER:
        return {
            "status": "unhealthy",
            "detail": "canary returned unexpected content",
            "latency_ms": latency,
        }
    actual_model = str(response.get("model") or spec["model"])
    return {
        "status": "healthy",
        "detail": "bounded canary answered",
        "latency_ms": latency,
        "actual_model": actual_model,
        "model_source": "response" if response.get("model") else "requested",
    }


def _probe_codex(
    spec: dict[str, Any],
    *,
    root: Path,
    timeout: int,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> dict[str, Any]:
    binary = resolve_codex_bin(str(spec.get("binary") or ""))
    if not binary:
        return {
            "status": "unhealthy",
            "detail": "Codex CLI not found",
            "latency_ms": 0,
        }
    command = [
        binary,
        "exec",
        "--json",
        "--color",
        "never",
        "--sandbox",
        "read-only",
        "--skip-git-repo-check",
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        "-C",
        str(root),
        "-m",
        spec["model"],
        "-",
    ]
    started = time.monotonic()
    try:
        completed = runner(
            command,
            input=(
                f"Provider health canary. Reply with exactly {CANARY_MARKER} "
                "and nothing else. Do not use tools."
            ),
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(root),
        )
    except subprocess.TimeoutExpired:
        return {
            "status": "unhealthy",
            "detail": f"canary timed out after {timeout}s",
            "latency_ms": round((time.monotonic() - started) * 1000),
        }
    except Exception as exc:
        return {
            "status": "unhealthy",
            "detail": _safe_error(f"{type(exc).__name__}: {exc}"),
            "latency_ms": round((time.monotonic() - started) * 1000),
        }
    text = ""
    for line in str(completed.stdout or "").splitlines():
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        item = event.get("item") if event.get("type") == "item.completed" else None
        if isinstance(item, dict) and item.get("type") == "agent_message":
            text = str(item.get("text") or "")
    latency = round((time.monotonic() - started) * 1000)
    if completed.returncode == 0 and text.strip() == CANARY_MARKER:
        return {
            "status": "healthy",
            "detail": "bounded read-only canary answered",
            "latency_ms": latency,
            "actual_model": spec["model"],
            "model_source": "requested",
        }
    return {
        "status": "unhealthy",
        "detail": (
            "canary returned unexpected content"
            if completed.returncode == 0
            else _safe_error(completed.stderr)
            or f"canary exited {completed.returncode}"
        ),
        "latency_ms": latency,
    }


def probe_provider(
    spec: dict[str, Any],
    *,
    root: str | Path | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    openai_caller: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    result = _base_result(spec)
    if result["status"] != "not_run":
        return result
    if spec["kind"] == "claude":
        outcome = _probe_claude(
            spec, root=_root(root), timeout=timeout, runner=runner
        )
    elif spec["kind"] == "codex":
        outcome = _probe_codex(
            spec, root=_root(root), timeout=timeout, runner=runner
        )
    else:
        outcome = _probe_openai(spec, timeout=timeout, caller=openai_caller)
    result.update(outcome)
    result["checked_at"] = _now()
    return result


def _write_state(root: Path, rows: list[dict[str, Any]]) -> dict[str, Any]:
    state = {"version": 1, "updated_at": _now(), "providers": rows}
    path = root / STATE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return state


def probe_all(
    root: str | Path | None = None,
    *,
    timeout: int = DEFAULT_TIMEOUT,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    openai_caller: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    base = _root(root)
    config = Config(base / "jarvis.yaml")
    specs = provider_specs(config)

    def probe(spec: dict[str, Any]) -> dict[str, Any]:
        return probe_provider(
            spec,
            root=base,
            timeout=timeout,
            runner=runner,
            openai_caller=openai_caller,
        )

    # Heartbeat gives deterministic pre-scripts a 60-second process budget.
    # Probing independent providers concurrently keeps the total bounded by
    # one provider timeout instead of multiplying it by the chain length.
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=max(1, len(specs)),
        thread_name_prefix="provider-canary",
    ) as pool:
        rows = list(pool.map(probe, specs))
    primary = next((row for row in rows if row["id"] == "primary"), None)
    if primary:
        try:
            from .model_fallback import clear, limit_reason, trip

            reason = limit_reason(str(primary.get("detail") or ""))
            if primary["status"] == "unhealthy" and reason:
                trip(reason, base)
            elif primary["status"] == "healthy":
                clear(base)
        except Exception:
            # Canary observability must survive a damaged failover-state file.
            pass
    return _write_state(base, rows)


def snapshot(root: str | Path | None = None) -> dict[str, Any]:
    base = _root(root)
    config = Config(base / "jarvis.yaml")
    path = base / STATE_FILE
    saved: dict[str, Any] = {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        saved = {
            str(row.get("id")): row
            for row in raw.get("providers", [])
            if isinstance(row, dict)
        }
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        pass
    rows = []
    for spec in provider_specs(config):
        current = _base_result(spec)
        old = saved.get(spec["id"])
        if (
            old
            and current["status"] == "not_run"
            and old.get("requested_model") == spec["model"]
        ):
            for key in (
                "status",
                "actual_model",
                "model_source",
                "checked_at",
                "latency_ms",
                "detail",
            ):
                current[key] = old.get(key, current.get(key))
        rows.append(current)
    return {
        "version": 1,
        "updated_at": max(
            (str(row.get("checked_at") or "") for row in rows), default=""
        ),
        "providers": rows,
    }


def summary_text(root: str | Path | None = None) -> str:
    labels = {
        "healthy": "正常",
        "unhealthy": "异常",
        "disabled": "未启用",
        "unconfigured": "未配置",
        "not_run": "待验真",
    }
    parts = []
    for row in snapshot(root)["providers"]:
        model = row.get("actual_model") or row.get("requested_model") or "unknown"
        parts.append(
            f"{row['label']} / {model}："
            f"{labels.get(str(row['status']), row['status'])}"
        )
    return "\n".join(parts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("probe", "status"), nargs="?",
                        default="status")
    parser.add_argument("--root", default="")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    args = parser.parse_args(argv)
    state = (
        probe_all(args.root or None, timeout=max(1, args.timeout))
        if args.command == "probe"
        else snapshot(args.root or None)
    )
    print(json.dumps(state, ensure_ascii=False))
    unhealthy = any(
        row["status"] == "unhealthy" for row in state["providers"]
    )
    return 1 if unhealthy else 0


if __name__ == "__main__":
    raise SystemExit(main())
