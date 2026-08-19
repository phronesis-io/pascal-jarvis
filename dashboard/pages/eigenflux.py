"""EigenFlux network desk: outgoing broadcasts and harness health."""

from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path

from nicegui import run, ui

from core import memorial
from core.interval_config import (
    parse_interval_overrides,
    resolve_effective_interval,
)

from ..telemetry import memorial_states, read_json
from ..uiutil import (
    guarded_refresh_timer,
    jarvis_page,
    memorial_option_label,
    memorial_visible_options,
    notify_safely,
)

JARVIS_DIR = Path(__file__).parent.parent.parent
TASKS = (
    ("eigenflux-inbox-reconcile", "私信补偿", 5 * 60, 15 * 60),
    ("eigenflux-feed-triage", "信号摄取", 10 * 60, 30 * 60),
    ("eigenflux-publish", "广播起草", 60 * 60, 3 * 3600),
    ("eigenflux-profile", "画像同步", 24 * 3600, 36 * 3600),
    ("eigenflux-friends", "好友申请", 10 * 60, 30 * 60),
    ("eigenflux-preinstall", "能力同步", 24 * 3600, 36 * 3600),
)
SUCCESS_STATUSES = frozenset({"ok", "empty_pre", "idle"})


def _truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _number(value, default: float = 0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _fmt_epoch(value) -> str:
    try:
        return datetime.fromtimestamp(float(value)).strftime("%m-%d %H:%M")
    except (TypeError, ValueError, OSError, OverflowError):
        return "尚无记录"


def _eigenflux_home() -> Path:
    return Path(os.environ.get("EIGENFLUX_HOME", "~/.eigenflux")).expanduser()


def load_network_overview(
    jarvis_dir: str | Path,
    *,
    eigenflux_home: str | Path | None = None,
    now_epoch: float | None = None,
) -> dict:
    """Read the local truth without calling the network on page load."""
    root = Path(jarvis_dir)
    ef_home = Path(eigenflux_home) if eigenflux_home else _eigenflux_home()
    config = read_json(ef_home / "config.json", ttl=5, default={}) or {}
    kv = config.get("kv") if isinstance(config, dict) else {}
    kv = kv if isinstance(kv, dict) else {}

    pending = []
    for path in sorted(
        (root / "eigenflux" / "pending_publish").glob("*.json"),
        reverse=True,
    ):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        data = dict(data)
        data["_path"] = str(path)
        pending.append(data)

    publish_state = read_json(
        root / "eigenflux" / "publish_state.json", ttl=5, default={}
    ) or {}
    recent = [
        item for item in (publish_state.get("recent") or [])
        if isinstance(item, dict)
    ]
    recent.sort(
        key=lambda item: _number(item.get("epoch")), reverse=True
    )

    heartbeat = read_json(root / "heartbeat_state.json", ttl=5, default={}) or {}
    task_state = heartbeat.get("tasks") if isinstance(heartbeat, dict) else {}
    if not isinstance(task_state, dict):
        task_state = heartbeat if isinstance(heartbeat, dict) else {}
    interval_overrides = parse_interval_overrides(read_json(
        root / "interval_overrides.json", ttl=5, default={}
    ))
    tasks = []
    current_epoch = (
        datetime.now().timestamp() if now_epoch is None else _number(now_epoch)
    )
    for task_id, label, default_interval_s, minimum_max_age_s in TASKS:
        state = task_state.get(task_id) or {}
        state = state if isinstance(state, dict) else {}
        circuit = state.get("circuit") or {}
        circuit = circuit if isinstance(circuit, dict) else {}
        effective_interval_s = _number(resolve_effective_interval(
            task_id,
            default_interval_s,
            state.get("effective_interval"),
            interval_overrides,
        ), default_interval_s)
        if effective_interval_s <= 0:
            effective_interval_s = default_interval_s
        max_age_s = max(minimum_max_age_s, 2 * effective_interval_s)
        disabled_until = _number(circuit.get("disabled_until"))
        last_status = str(state.get("last_status") or "")
        last_success = _number(state.get("last_success"))
        fresh = (
            last_success > 0
            and current_epoch - last_success <= max_age_s
        )
        healthy = (
            disabled_until <= current_epoch
            and fresh
            and last_status in SUCCESS_STATUSES
        )
        if disabled_until > current_epoch:
            detail = "熔断中"
        elif not fresh:
            detail = "超过应有周期未成功"
        elif last_status not in SUCCESS_STATUSES:
            detail = last_status or "状态未知"
        else:
            detail = "正常"
        tasks.append(
            {
                "id": task_id,
                "label": label,
                "healthy": healthy,
                "last_status": last_status or "unknown",
                "last_success": _fmt_epoch(last_success),
                "detail": detail,
            }
        )

    stream = read_json(
        root / "data" / "ef_stream_health.json", ttl=5, default={}
    ) or {}
    stream_status = str(stream.get("status") or "unknown")
    stream_updated = _number(stream.get("updated_epoch"))
    stream_fresh = stream_updated > 0 and current_epoch - stream_updated <= 2400
    ingress = read_json(
        root / "data" / "ef_ingress_health.json", ttl=5, default={}
    ) or {}
    poll_success = _number(ingress.get("last_success_epoch"))
    poll_fresh = (
        poll_success > 0
        and current_epoch - poll_success <= 900
        and str(ingress.get("status") or "") == "ok"
    )
    stream_active = stream_fresh and stream_status == "active"
    if stream_active and poll_fresh:
        ingress_mode = "实时 + 轮询双路"
    elif poll_fresh:
        ingress_mode = "轮询兜底"
    elif stream_active:
        ingress_mode = "仅实时流，兜底未验真"
    else:
        ingress_mode = "未验真"
    # A quiet WebSocket cannot prove end-to-end ingress. The five-minute poll
    # is both the no-loss fallback and the deterministic liveness receipt.
    stream_healthy = poll_fresh
    stream = {
        "status": stream_status,
        "healthy": stream_healthy,
        "mode": ingress_mode,
        "detail": str(ingress.get("detail") or stream.get("detail")
                      or "尚无私信接入验真记录"),
        "quiet_streak": int(_number(stream.get("quiet_streak"))),
        "updated": _fmt_epoch(stream_updated),
        "poll_updated": _fmt_epoch(poll_success),
    }

    return {
        "recurring_publish": _truthy(kv.get("recurring_publish")),
        "auto_comment": _truthy(kv.get("auto_comment")),
        "pending": pending,
        "recent": recent,
        "tasks": tasks,
        "stream": stream,
    }


def _pending_memorials(root: Path, active_ids: set[str]) -> dict[str, dict]:
    matches = {}
    for state in memorial_states(root):
        if (
            state.get("source") != "eigenflux-publish"
            or state.get("status") != "pending"
        ):
            continue
        context = str(state.get("context") or "")
        matched = re.search(r"pending_publish id=(\d+_\d+)", context)
        if matched and matched.group(1) in active_ids:
            matches[matched.group(1)] = state
    return matches


def create_official_dashboard_link(timeout: int = 12) -> str:
    """Generate, but never persist, EigenFlux's single-use dashboard URL."""
    result = subprocess.run(
        ["eigenflux", "-f", "json", "dashboard"],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "EigenFlux 暂时不可用").strip()
        raise RuntimeError(detail[:180])
    match = re.search(r"https://[^\s\"']+", result.stdout or "")
    if not match:
        raise RuntimeError("EigenFlux 没有返回可用的后台链接")
    return match.group(0).rstrip(".,)")


@ui.page("/eigenflux")
def eigenflux_page():
    with jarvis_page(
        "/eigenflux",
        "EigenFlux 网络",
        "在这里看 Jarvis 对外广播了什么、正在等你批什么，以及网络能力是否在工作。",
    ):
        @ui.refreshable
        def board():
            overview = load_network_overview(JARVIS_DIR)
            pending = overview["pending"]
            published = overview["recent"]
            healthy_tasks = sum(1 for item in overview["tasks"] if item["healthy"])

            with ui.element("span").classes("status-pill"):
                ui.element("span").classes(
                    "status-dot "
                    + ("is-green" if healthy_tasks == len(TASKS) else "is-amber")
                )
                ui.label(
                    f"定时任务 {healthy_tasks}/{len(TASKS)} 正常"
                )
            with ui.element("span").classes("status-pill"):
                ui.element("span").classes(
                    "status-dot "
                    + ("is-green" if overview["stream"]["healthy"] else "is-amber")
                )
                ui.label(
                    (
                        f"私信接入正常：{overview['stream']['mode']}"
                        if overview["stream"]["healthy"]
                        else f"私信接入未验真：{overview['stream']['status']}"
                    )
                )

            with ui.element("div").classes("metric-strip"):
                for value, label, alert in (
                    (len(pending), "待批广播", bool(pending)),
                    (len(published), "本机已发记录", False),
                    ("开" if overview["recurring_publish"] else "关", "自动起草", False),
                    ("开" if overview["auto_comment"] else "关", "自动回应", False),
                ):
                    with ui.element("div").classes("metric-cell"):
                        ui.label(str(value)).classes(
                            "metric-value" + (" is-alert" if alert else "")
                        )
                        ui.label(label).classes("metric-label")

            ui.label("壹 · 对外广播").classes("section-kicker")
            ui.label("自动起草，人工确认").classes("section-title")
            ui.label(
                "Jarvis 会定时准备候选，但不会替你公开发言。只有你点“发”以后，"
                "内容才会进入 EigenFlux。"
            ).classes("section-note")

            active_ids = {str(item.get("id") or "") for item in pending}
            approval_by_id = _pending_memorials(JARVIS_DIR, active_ids)

            def decide(memorial_id: str, key: str):
                payload = memorial.decide(
                    memorial_id, key, owner_authenticated=True
                )
                toast = payload.get("toast") or {}
                notify_safely(
                    toast.get("content", "已处理"),
                    type=(
                        "positive"
                        if toast.get("type") == "success"
                        else "warning"
                        if toast.get("type") == "error"
                        else "info"
                    ),
                )
                board.refresh()

            if pending:
                with ui.element("div").classes("memorial-grid"):
                    for draft in pending:
                        pending_id = str(draft.get("id") or "")
                        state = approval_by_id.get(pending_id)
                        notes = draft.get("notes") or {}
                        with ui.card().classes("memorial-card"):
                            with ui.row().classes(
                                "w-full items-center justify-between gap-3"
                            ):
                                ui.label(
                                    str(notes.get("type") or "broadcast")
                                ).classes("memorial-source")
                                ui.label(
                                    _fmt_epoch(draft.get("created_at"))
                                ).classes("memorial-time")
                            ui.label(
                                str(notes.get("summary") or "待确认广播")
                            ).classes("memorial-title")
                            ui.label(str(draft.get("content") or "")).classes(
                                "memorial-body"
                            )
                            with ui.row().classes("memorial-actions"):
                                if state:
                                    for index, option in enumerate(
                                        memorial_visible_options(state)[:2]
                                    ):
                                        ui.button(
                                            memorial_option_label(
                                                option.get("label", "处理")
                                            ),
                                            on_click=lambda mid=state["id"],
                                            key=option.get("key", ""): decide(
                                                mid, key
                                            ),
                                        ).props(
                                            "unelevated no-caps"
                                            if index == 0
                                            else "outline no-caps"
                                        ).classes(
                                            "memorial-primary"
                                            if index == 0
                                            else "memorial-secondary"
                                        )
                                else:
                                    ui.label(
                                        "审批卡正在恢复，请到“事项”查看"
                                    ).classes("section-note")
                                if state:
                                    with ui.link(
                                        target=f"/items/{state['id']}"
                                    ).classes("item-detail-link"):
                                        ui.icon("open_in_full", size="16px")
                                        ui.label("查看全文")
            else:
                ui.label("现在没有待你确认的广播。").classes("empty-guidance")

            ui.label("贰 · 最近已发").classes("section-kicker")
            ui.label("Jarvis 经你确认后发布的内容").classes("section-title")
            if not published:
                ui.label("本机还没有已发布记录。").classes("empty-guidance")
            else:
                with ui.element("div").classes("signal-list"):
                    for item in published[:10]:
                        with ui.element("article").classes("signal-row"):
                            with ui.row().classes(
                                "w-full items-center justify-between gap-3"
                            ):
                                ui.label("已广播").classes("memorial-source")
                                ui.label(
                                    _fmt_epoch(item.get("epoch"))
                                ).classes("memorial-time")
                            ui.label(
                                str(item.get("summary") or "EigenFlux 广播")
                            ).classes("signal-title")
                            preview = str(item.get("content_preview") or "")
                            if preview:
                                ui.label(preview).classes("signal-body")

            ui.label("叁 · 自动链路").classes("section-kicker")
            ui.label("每一环最后一次真的跑通了吗").classes("section-title")
            with ui.element("div").classes("signal-list"):
                for task in overview["tasks"]:
                    with ui.element("div").classes("signal-row"):
                        with ui.row().classes(
                            "w-full items-center justify-between gap-3"
                        ):
                            with ui.row().classes("items-center gap-2"):
                                ui.icon(
                                    "check_circle"
                                    if task["healthy"]
                                    else "error_outline",
                                    color=(
                                        "positive"
                                        if task["healthy"]
                                        else "negative"
                                    ),
                                    size="18px",
                                )
                                ui.label(task["label"]).classes("signal-title")
                            ui.label(task["last_success"]).classes(
                                "memorial-time"
                            )
                        ui.label(
                            task["detail"]
                        ).classes("section-note")

            async def open_dashboard():
                try:
                    target = await run.io_bound(create_official_dashboard_link)
                except Exception as exc:
                    notify_safely(f"官方后台暂时打不开：{exc}", type="negative")
                    return
                ui.navigate.to(target, new_tab=True)

            ui.label("肆 · 官方后台").classes("section-kicker")
            ui.label("全网数据与完整会话").classes("section-title")
            ui.label(
                "Jarvis 网络台负责你的工作流；官方后台负责全网广播、影响力、"
                "私信与好友的完整记录。登录链接一次有效。"
            ).classes("section-note")
            ui.button(
                "打开 EigenFlux 官方后台",
                icon="open_in_new",
                on_click=open_dashboard,
            ).props("unelevated no-caps").classes("memorial-primary")

        board()
        guarded_refresh_timer(30, board.refresh)
