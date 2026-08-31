#!/usr/bin/env python3
"""Emit one user-visible card for each new model-quota risk episode."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.safety import atomic_write
from core.model_usage import human_time, owner_route_label


def _state_path() -> Path:
    root = Path(os.environ.get("JARVIS_DIR") or Path(__file__).resolve().parent.parent)
    return root / "data" / "model_usage_alert_state.json"


def _read_state(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_state(path: Path, payload: dict) -> None:
    atomic_write(path, json.dumps(payload, ensure_ascii=False, indent=2))
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _issue_key(item: dict) -> str:
    code = str(item.get("code") or "")
    category = "codex_quota_risk" if code.startswith("codex_") else code
    return (
        f"{category}:{item.get('route_id', '')}:"
        f"{item.get('limit_id', '')}:{item.get('window_name', '')}"
    )


def render_usage_alert(report: dict, *, state_path: Path) -> str:
    # Unknown is not recovery. Preserve the current episode when the exact
    # package surface cannot be read, otherwise every transient read failure
    # clears and re-arms the same alert.
    if str((report.get("codex") or {}).get("error") or ""):
        return ""
    issues = [item for item in report.get("issues", []) if isinstance(item, dict)]
    keyed = {_issue_key(item): item for item in issues}
    current_keys = set(keyed)
    previous = _read_state(state_path)
    previous_keys = {
        str(key) for key in previous.get("open_keys", []) if str(key)
    }
    if not current_keys:
        if previous_keys:
            _write_state(state_path, {"open_keys": [], "status": "clear"})
        return ""
    new_keys = current_keys - previous_keys
    if current_keys != previous_keys:
        _write_state(state_path, {
            "open_keys": sorted(current_keys),
            "status": "open",
            "observed_at": report.get("observed_at", ""),
        })
    if not new_keys:
        return ""
    lines = [
        "TITLE: 模型额度需要留意",
        "WORKED: 已读取真实套餐窗口并核对当前备用链路",
    ]
    issue_lines: list[str] = []
    route_labels = {
        str(item.get("id") or ""): str(
            item.get("owner_label")
            or owner_route_label(item.get("id"), item.get("label"))
        )
        for item in report.get("routes", []) if isinstance(item, dict)
    }
    for item in [keyed[key] for key in sorted(new_keys)][:4]:
        if item.get("code", "").startswith("codex_"):
            prediction = item.get("predicted_exhaustion_at")
            used = float(item.get("used_percent") or 0)
            # 「已用 1%…预计 9/3 用尽」(2026-08-30) is self-contradicting copy:
            # a forecast is only worth a sentence once half the window is gone.
            timing = (
                f"，按当前速度预计 {human_time(prediction)} 用尽"
                if human_time(prediction) and used >= 50.0 else ""
            )
            issue_lines.append(
                f"Codex {item.get('window_label') or item.get('window_name', '')}"
                f"额度已用 {used:g}%，还剩约 {max(0.0, 100.0 - used):g}%；"
                f"{human_time(item.get('resets_at')) or '重置时间未知'} 重置{timing}。"
            )
        else:
            route_id = str(item.get("route_id") or "")
            issue_lines.append(
                f"{route_labels.get(route_id) or owner_route_label(route_id)} 已遇到账户额度限制。"
            )
    lines.append(" ".join(issue_lines[:3]))
    order = report.get("fallback_labels") or [
        route_labels.get(str(route_id)) or owner_route_label(route_id)
        for route_id in report.get("fallback_order", [])
    ]
    lines.append(
        "当前可尝试顺序：" + ("、".join(order) if order else "没有可执行通道")
    )
    lines.append("你不用现在打开网页；重置或链路变化后我再更新。")
    return "\n".join(lines)


def main() -> int:
    raw = sys.stdin.read().strip()
    try:
        report = json.loads(raw) if raw else {}
    except (json.JSONDecodeError, TypeError, ValueError):
        return 1
    if not isinstance(report, dict) or report.get("schema") != "jarvis.model-usage.v1":
        return 1
    alert = render_usage_alert(report, state_path=_state_path())
    if alert:
        print(alert)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
