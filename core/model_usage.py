"""Unified, credential-free model package usage and exhaustion forecast.

Exact quota evidence is provider-specific. Codex ships a local
app-server read method; Claude subscriptions expose account type but no
documented quota read in the CLI; relay/API routes may expose neither. Unknown
is therefore a first-class state. Health canaries never become quota evidence.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from core.claude_bin import resolve_claude_bin
from core.codex_app_server import CodexAppServerClient, CodexAppServerError
from core.config import Config
from core.model_control import catalog_report, route_plan
from core.model_fallback import gate
from core.provider_health import snapshot as health_snapshot
from core.safety import atomic_write


LATEST_FILE = "data/model_usage_latest.json"
CODEX_TIMEOUT_SECONDS = 8
OWNER_ROUTE_LABELS = {
    "primary": "Claude 主通道",
    "backup1": "Claude 第一备用",
    "backup2": "Claude 第二备用",
    "codex": "Codex 备用通道",
    "openai": "GPT 备用通道",
}


class UsageReadError(RuntimeError):
    """A provider's read-only usage surface did not produce a valid result."""


def _epoch_iso(value: Any) -> str:
    try:
        epoch = float(value or 0)
    except (TypeError, ValueError):
        return ""
    if epoch <= 0:
        return ""
    return datetime.fromtimestamp(epoch).astimezone().isoformat(timespec="minutes")


def human_time(value: Any) -> str:
    """Render one ISO timestamp in compact Chinese for owner-facing copy."""
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone()
    except (TypeError, ValueError, OSError):
        return ""
    return f"{moment.month}月{moment.day}日 {moment:%H:%M}"


def owner_route_label(route_id: Any, fallback: Any = "") -> str:
    """Stable owner-facing route name without internal fallback jargon."""
    value = str(route_id or "").strip()
    return OWNER_ROUTE_LABELS.get(value, str(fallback or value or "模型通道"))


def _window_label(minutes: int, fallback: str = "") -> str:
    value = max(0, int(minutes or 0))
    if value and value % 10080 == 0:
        return f"{value // 10080} 周" if value > 10080 else "7 天"
    if value and value % 1440 == 0:
        return f"{value // 1440} 天"
    if value and value % 60 == 0:
        return f"{value // 60} 小时"
    if value:
        return f"{value} 分钟"
    return str(fallback or "当前")


def read_codex_rate_limits(
    binary: str = "", *, timeout: int = CODEX_TIMEOUT_SECONDS,
    popen_factory: Callable[..., Any] = subprocess.Popen,
) -> dict[str, Any]:
    """Read the signed-in ChatGPT/Codex account's rate-limit snapshot."""
    try:
        with CodexAppServerClient(
            binary,
            timeout=timeout,
            client_name="jarvis-usage",
            client_version="0.1.0",
            experimental_api=False,
            popen_factory=popen_factory,
        ) as client:
            result = client.request("account/rateLimits/read", {})
    except CodexAppServerError as exc:
        raise UsageReadError("Codex app-server usage read failed") from exc
    if not isinstance(result, dict):
        raise UsageReadError("Codex app-server returned an invalid result")
    return result


def read_claude_account(
    binary: str = "", *, timeout: int = 5,
    runner: Callable[..., Any] = subprocess.run,
) -> dict[str, Any]:
    """Read non-secret Claude account metadata; quota remains explicitly unknown."""
    try:
        executable = binary or resolve_claude_bin()
        result = runner(
            [executable, "auth", "status", "--json"],
            capture_output=True, text=True, timeout=timeout,
        )
        payload = json.loads(result.stdout) if result.returncode == 0 else {}
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError, ValueError):
        payload = {}
    return {
        "logged_in": bool(payload.get("loggedIn")),
        "subscription_type": str(payload.get("subscriptionType") or "unknown"),
        "auth_method": str(payload.get("authMethod") or "unknown"),
        "usage_evidence": "unknown",
    }


def _window_rows(payload: dict[str, Any]) -> tuple[list[dict], dict]:
    limits = payload.get("rateLimitsByLimitId")
    if not isinstance(limits, dict) or not limits:
        primary = payload.get("rateLimits")
        limits = {str((primary or {}).get("limitId") or "codex"): primary}
    rows: list[dict] = []
    for limit_id, item in limits.items():
        if not isinstance(item, dict):
            continue
        for window_name in ("primary", "secondary"):
            window = item.get(window_name)
            if not isinstance(window, dict):
                continue
            try:
                used = max(0.0, min(100.0, float(window.get("usedPercent"))))
            except (TypeError, ValueError):
                continue
            rows.append({
                "limit_id": str(item.get("limitId") or limit_id),
                "limit_name": str(item.get("limitName") or "Codex"),
                "window_name": window_name,
                "window_minutes": int(window.get("windowDurationMins") or 0),
                "window_label": _window_label(
                    int(window.get("windowDurationMins") or 0), window_name,
                ),
                "used_percent": used,
                "remaining_percent": round(100.0 - used, 2),
                "resets_at_epoch": float(window.get("resetsAt") or 0),
                "resets_at": _epoch_iso(window.get("resetsAt")),
                "reached_type": str(item.get("rateLimitReachedType") or ""),
                "spend_control_reached": bool(item.get("spendControlReached")),
                "plan_type": str(item.get("planType") or "unknown"),
            })
    credits = payload.get("rateLimitResetCredits")
    safe_credits = {
        "available_count": int((credits or {}).get("availableCount") or 0),
        "next_expiry": "",
    }
    if isinstance(credits, dict) and isinstance(credits.get("credits"), list):
        expiries = [
            float(item.get("expiresAt") or 0)
            for item in credits["credits"] if isinstance(item, dict)
        ]
        safe_credits["next_expiry"] = _epoch_iso(min(
            (value for value in expiries if value > 0), default=0,
        ))
    return rows, safe_credits


def _forecast_window(
    row: dict, *, observed_epoch: float, persist: bool = True,
) -> dict:
    from core.db import get_db

    db = get_db()
    prior = db.execute(
        """SELECT used_percent,observed_epoch
             FROM model_usage_observations
            WHERE route_id='codex' AND limit_id=? AND window_name=?
              AND COALESCE(resets_at_epoch,0)=?
              AND observed_epoch < ?
            ORDER BY observed_epoch ASC LIMIT 1""",
        (
            row["limit_id"], row["window_name"],
            float(row.get("resets_at_epoch") or 0), observed_epoch,
        ),
    ).fetchone()
    prediction = 0.0
    if prior is not None:
        elapsed = observed_epoch - float(prior["observed_epoch"])
        consumed = float(row["used_percent"]) - float(prior["used_percent"])
        window_seconds = max(0, int(row.get("window_minutes") or 0)) * 60
        reset_epoch = float(row.get("resets_at_epoch") or 0)
        window_start = reset_epoch - window_seconds
        elapsed_ratio = (
            max(0.0, min(1.0, (observed_epoch - window_start) / window_seconds))
            if window_seconds > 0 and reset_epoch > 0
            else 0.0
        )
        forecast_eligible = (
            float(row["used_percent"]) >= 50.0 or elapsed_ratio >= 0.25
        )
        if elapsed >= 300 and consumed > 0 and forecast_eligible:
            prediction = observed_epoch + (
                (100.0 - float(row["used_percent"])) / (consumed / elapsed)
            )
            if prediction >= float(row.get("resets_at_epoch") or 0):
                prediction = 0.0
    if persist:
        db.execute(
            """INSERT OR IGNORE INTO model_usage_observations
               (route_id,limit_id,window_name,used_percent,resets_at_epoch,
                observed_epoch,source)
               VALUES ('codex',?,?,?,?,?,'codex_app_server')""",
            (
                row["limit_id"], row["window_name"], row["used_percent"],
                row.get("resets_at_epoch") or None, observed_epoch,
            ),
        )
        db.execute(
            "DELETE FROM model_usage_observations WHERE observed_epoch < ?",
            (observed_epoch - 45 * 86400,),
        )
        db.commit()
    row["predicted_exhaustion_epoch"] = prediction
    row["predicted_exhaustion_at"] = _epoch_iso(prediction)
    reached = bool(row["reached_type"] or row["spend_control_reached"])
    used = float(row["used_percent"])
    if reached or used >= 100:
        row["risk"] = "exhausted"
    elif used >= 90:
        row["risk"] = "critical"
    elif used >= 80:
        row["risk"] = "warning"
    else:
        row["risk"] = "ok"
    return row


def _route_health(root: Path) -> dict[str, dict]:
    try:
        rows = health_snapshot(root).get("providers", [])
    except Exception:
        rows = []
    return {str(row.get("id") or ""): row for row in rows}


def build_report(
    root: str | Path | None = None, *, now: float | None = None,
    codex_reader: Callable[[], dict[str, Any]] = read_codex_rate_limits,
    claude_reader: Callable[[], dict[str, Any]] = read_claude_account,
    persist: bool = True,
) -> dict[str, Any]:
    """Join exact quota, account, route-health, and fallback evidence."""
    base = Path(root or os.environ.get("JARVIS_DIR") or Path.cwd()).resolve()
    observed_epoch = float(time.time() if now is None else now)
    config = Config(base / "jarvis.yaml")
    catalog = catalog_report(config)
    health = _route_health(base)
    try:
        gate_state = gate(base, probe=False)
    except Exception:
        gate_state = "primary"
    plan = route_plan(
        "owner_chat", config=config, gate_state=gate_state,
        health_rows=list(health.values()), now_epoch=observed_epoch,
    )
    try:
        codex_payload = codex_reader()
        windows, credits = _window_rows(codex_payload)
        windows = [
            _forecast_window(
                row, observed_epoch=observed_epoch, persist=persist,
            )
            for row in windows
        ]
        codex_error = ""
    except Exception as exc:
        windows, credits = [], {"available_count": 0, "next_expiry": ""}
        codex_error = type(exc).__name__
    try:
        claude_account = claude_reader()
    except Exception:
        claude_account = {
            "logged_in": False,
            "subscription_type": "unknown",
            "auth_method": "unknown",
            "usage_evidence": "unknown",
        }
    claude_account_known = bool(
        claude_account.get("logged_in")
        or str(claude_account.get("subscription_type") or "unknown") != "unknown"
    )
    routes = []
    for route in catalog["routes"]:
        row = health.get(route["id"], {})
        routes.append({
            **route,
            "owner_label": owner_route_label(route["id"], route.get("label")),
            "health": str(row.get("status") or "not_run"),
            "health_source": str(row.get("observation_source") or "unknown"),
            "health_detail": str(row.get("detail") or "")[:160],
            "quota_evidence": (
                "exact" if route["id"] == "codex" and windows else
                "account_only" if (
                    route["id"] == "primary" and claude_account_known
                ) else "unknown"
            ),
        })
    issues = [
        {
            "code": f"codex_{row['risk']}",
            "route_id": "codex",
            "limit_id": row["limit_id"],
            "window_name": row["window_name"],
            "window_label": row["window_label"],
            "used_percent": row["used_percent"],
            "resets_at": row["resets_at"],
            "predicted_exhaustion_at": row["predicted_exhaustion_at"],
        }
        for row in windows if row["risk"] in {"critical", "exhausted"}
    ]
    for route in routes:
        if route["health_source"] == "real_request" and (
            "account_limit" in route["health_detail"]
        ):
            issues.append({
                "code": "provider_account_limited",
                "route_id": route["id"],
            })
    report = {
        "schema": "jarvis.model-usage.v1",
        "observed_epoch": observed_epoch,
        "observed_at": _epoch_iso(observed_epoch),
        "active_route": plan.routes[0].id if plan.routes else "none",
        "fallback_order": [route.id for route in plan.routes],
        "fallback_labels": [
            owner_route_label(route.id, route.label) for route in plan.routes
        ],
        "claude_account": claude_account,
        "codex": {
            "source": "codex_app_server" if windows else "unknown",
            "error": codex_error,
            "windows": windows,
            "reset_credits": credits,
        },
        "routes": routes,
        "issues": issues,
    }
    if persist:
        path = base / LATEST_FILE
        atomic_write(path, json.dumps(report, ensure_ascii=False, indent=2))
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    return report


def load_latest(root: str | Path | None = None) -> dict[str, Any]:
    base = Path(root or os.environ.get("JARVIS_DIR") or Path.cwd()).resolve()
    try:
        payload = json.loads((base / LATEST_FILE).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def status_text(report: dict[str, Any]) -> str:
    route_labels = {
        str(route.get("id") or ""): str(
            route.get("owner_label")
            or owner_route_label(route.get("id"), route.get("label"))
        )
        for route in report.get("routes", []) if isinstance(route, dict)
    }
    active = str(report.get("active_route") or "")
    lines = [
        f"当前可执行通道：{route_labels.get(active) or owner_route_label(active)}"
    ]
    account = report.get("claude_account") or {}
    subscription = str(account.get("subscription_type") or "unknown")
    if account.get("logged_in") or subscription != "unknown":
        lines.append(
            f"Claude：{subscription} 套餐；官方 CLI 暂未提供剩余额度数字"
        )
    else:
        lines.append("Claude：账号信息未知，剩余额度也未知")
    codex = report.get("codex") or {}
    windows = codex.get("windows") or []
    if windows:
        lines.append("Codex：")
        for row in windows:
            label = row.get("limit_name") or row.get("limit_id")
            window = row.get("window_label") or _window_label(
                row.get("window_minutes", 0), row.get("window_name", ""),
            )
            forecast = (
                f"，按当前速度预计 {human_time(row['predicted_exhaustion_at'])} 用尽"
                if human_time(row.get("predicted_exhaustion_at"))
                and float(row.get("used_percent") or 0) >= 50.0 else ""
            )
            remaining = row.get("remaining_percent")
            if remaining is None:
                remaining = max(0.0, 100.0 - float(row.get("used_percent") or 0))
            lines.append(
                f"- {label} {window}额度已用 "
                f"{row['used_percent']:g}%，还剩约 "
                f"{remaining:g}%；"
                f"{human_time(row['resets_at']) or '重置时间未知'} 重置{forecast}"
            )
        credits = codex.get("reset_credits") or {}
        if credits.get("available_count"):
            lines.append(f"- 可用完整重置：{credits['available_count']} 次")
    else:
        lines.append("Codex：暂时读不到套餐窗口，状态标为未知")
    unknown = [
        str(route.get("owner_label") or owner_route_label(
            route.get("id"), route.get("label")))
        for route in report.get("routes", [])
        if route.get("quota_evidence") == "unknown" and route.get("enabled")
    ]
    if unknown:
        lines.append("未提供余额接口：" + "、".join(unknown))
    order = report.get("fallback_labels") or [
        owner_route_label(route_id) for route_id in report.get("fallback_order", [])
    ]
    lines.append("当前尝试顺序：" + ("、".join(order) if order else "无"))
    if report.get("observed_at"):
        lines.append(
            f"观测时间：{human_time(report['observed_at']) or '刚刚'}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m core.model_usage")
    parser.add_argument("command", choices=("status", "latest"), nargs="?", default="status")
    parser.add_argument("--root", default="")
    parser.add_argument("--text", action="store_true")
    args = parser.parse_args(argv)
    report = (
        load_latest(args.root or None)
        if args.command == "latest" else build_report(args.root or None)
    )
    print(status_text(report) if args.text else json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
