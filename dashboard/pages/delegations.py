"""Desktop control plane for evidence-backed Delegations."""

from __future__ import annotations

from datetime import datetime

from nicegui import run, ui

from core.continuity import create_handoff
from core.delegations import DelegationStore, is_confirmable, is_retryable
from core.delegation_reconcile import sync_attention_item

from ..uiutil import (
    add_dashboard_head,
    client_surface,
    dashboard_header,
    notify_safely,
)


STATUS_LABELS = {
    "captured": "待绑定",
    "needs_clarification": "需要说明",
    "bound": "准备执行",
    "executing": "正在做",
    "verifying": "正在核验",
    "awaiting_external": "等外部",
    "needs_user": "需要你",
    "blocked": "受阻",
    "completed": "已完成",
    "failed": "未完成",
    "cancelled": "已取消",
    "superseded": "已取代",
}
STATUS_COLORS = {
    "needs_user": "negative",
    "needs_clarification": "warning",
    "failed": "negative",
    "completed": "positive",
    "awaiting_external": "info",
}


def _time(value) -> str:
    if not value:
        return ""
    return datetime.fromtimestamp(float(value)).strftime("%m-%d %H:%M")


async def _confirm(detail: dict, refresh) -> None:
    store = DelegationStore()
    try:
        updated = await run.io_bound(
            store.confirm,
            detail["id"],
            expected_version=detail["contract_version"],
            principal_id=detail["principal_id"],
        )
        await run.io_bound(
            sync_attention_item, updated, store=store, send=False
        )
    except Exception as exc:
        notify_safely(f"确认没有生效：{exc}", type="negative")
        return
    notify_safely("已确认，执行器可以继续", type="positive")
    refresh()


async def _cancel(detail: dict, refresh) -> None:
    store = DelegationStore()
    try:
        updated = await run.io_bound(
            store.terminal,
            detail["id"],
            expected_version=detail["contract_version"],
            status="cancelled",
            reason_code="owner_cancelled",
            actor_id=detail["principal_id"],
        )
        await run.io_bound(
            sync_attention_item, updated, store=store, send=False
        )
    except Exception as exc:
        notify_safely(f"取消没有生效：{exc}", type="negative")
        return
    notify_safely("已取消", type="positive")
    refresh()


async def _retry(detail: dict, refresh) -> None:
    try:
        await run.io_bound(
            DelegationStore().retry,
            detail["id"],
            expected_version=detail["contract_version"],
            actor_id=detail["principal_id"],
        )
    except Exception as exc:
        notify_safely(f"重试没有生效：{exc}", type="negative")
        return
    notify_safely("已回到可执行状态", type="positive")
    refresh()


async def _handoff(detail: dict, surface: str) -> None:
    target = "desktop" if surface == "mobile" else "mobile"
    try:
        await run.io_bound(
            create_handoff,
            "delegation",
            detail["id"],
            from_surface=surface,
            to_surface=target,
            title=detail["title"],
            matter_id=str(detail.get("matter_id") or ""),
            created_by="local",
        )
    except Exception as exc:
        notify_safely(f"接力没有建立：{exc}", type="negative")
        return
    notify_safely(
        "已放到电脑接力区" if target == "desktop" else "已发到手机",
        type="positive",
    )


def _status_badge(status: str):
    ui.badge(
        STATUS_LABELS.get(status, status),
        color=STATUS_COLORS.get(status, "grey-7"),
    ).props("outline")


@ui.page("/delegations")
def delegation_list_page():
    add_dashboard_head()
    with ui.column().classes("jarvis-page"):
        dashboard_header(
            "/delegations",
            "委托进展",
            "这里只看 Jarvis 正在承担的结果；需要你决定的仍归入事项。",
        )

        @ui.refreshable
        def body():
            rows = DelegationStore().list(limit=100)
            active = [
                row
                for row in rows
                if row["status"] not in {
                    "completed",
                    "cancelled",
                    "superseded",
                }
            ]
            recent = [
                row
                for row in rows
                if row["status"] in {"completed", "cancelled", "superseded"}
            ][:20]
            with ui.row().classes("w-full items-center justify-between"):
                ui.label("正在承担").classes("section-title")
                ui.badge(str(len(active)), color="grey-8").props("outline")
            if not active:
                ui.label("目前没有正在执行或等待核验的委托。").classes(
                    "text-sm text-grey-6"
                )
            for row in active:
                with ui.element("article").classes(
                    "w-full border-b border-grey-3 py-4 cursor-pointer"
                ).on("click", lambda item=row: ui.navigate.to(
                    f"/delegations/{item['id']}"
                )):
                    with ui.row().classes(
                        "w-full items-start justify-between gap-3 no-wrap"
                    ):
                        with ui.column().classes("gap-1 min-w-0"):
                            ui.label(row["title"]).classes(
                                "text-base font-medium break-words"
                            )
                            target = row.get("target_label") or row.get("target_id")
                            ui.label(
                                " · ".join(
                                    value
                                    for value in (
                                        target,
                                        f"R{row['risk_tier']}",
                                        _time(row["updated_at"]),
                                    )
                                    if value
                                )
                            ).classes("text-xs text-grey-6")
                        _status_badge(row["status"])
            if recent:
                ui.separator().classes("my-3")
                ui.label("最近结果").classes("section-title")
                for row in recent:
                    with ui.row().classes(
                        "w-full items-center justify-between border-b "
                        "border-grey-2 py-3 cursor-pointer"
                    ).on("click", lambda item=row: ui.navigate.to(
                        f"/delegations/{item['id']}"
                    )):
                        ui.label(row["title"]).classes("text-sm")
                        _status_badge(row["status"])

        body()
        ui.timer(15, body.refresh)


@ui.page("/delegations/{delegation_id}")
def delegation_detail_page(delegation_id: str):
    add_dashboard_head()
    surface, _ = client_surface()
    with ui.column().classes("jarvis-page"):
        dashboard_header(
            "/delegations",
            "委托详情",
            "契约、执行步骤和权威证据共用同一个版本。",
        )

        @ui.refreshable
        def body():
            try:
                detail = DelegationStore().get(delegation_id)
            except Exception:
                ui.label("找不到这个委托。").classes("text-negative")
                return
            with ui.row().classes(
                "w-full items-start justify-between gap-4 no-wrap"
            ):
                with ui.column().classes("gap-1 min-w-0"):
                    ui.label(detail["title"]).classes(
                        "text-xl font-semibold break-words"
                    )
                    ui.label(
                        f"契约 v{detail['contract_version']} · "
                        f"{detail['operation']} · R{detail['risk_tier']}"
                    ).classes("text-xs text-grey-6")
                _status_badge(detail["status"])

            with ui.element("section").classes("w-full py-4 border-b border-grey-3"):
                ui.label("完成条件").classes("section-kicker")
                ui.label(
                    json_compact(detail["expected_postcondition"])
                ).classes("text-sm break-words")
                ui.label(
                    " · ".join(
                        value
                        for value in (
                            detail.get("target_label") or detail.get("target_id"),
                            detail.get("authority"),
                            f"更新于 {_time(detail['updated_at'])}",
                        )
                        if value
                    )
                ).classes("text-xs text-grey-6")

            with ui.row().classes("w-full gap-2 py-3"):
                if is_confirmable(detail):
                    ui.button(
                        "确认", icon="check",
                        on_click=lambda: _confirm(detail, body.refresh),
                    ).props("unelevated no-caps color=positive")
                if is_retryable(detail):
                    ui.button(
                        (
                            "重新核验"
                            if detail.get("waiting_on") == "verification_recovery"
                            else "重试"
                        ),
                        icon="refresh",
                        on_click=lambda: _retry(detail, body.refresh),
                    ).props("flat no-caps")
                if detail["status"] not in {
                    "completed",
                    "cancelled",
                    "superseded",
                }:
                    ui.button(
                        "取消", icon="close",
                        on_click=lambda: _cancel(detail, body.refresh),
                    ).props("flat no-caps color=negative")
                ui.button(
                    icon="devices",
                    on_click=lambda: _handoff(detail, surface),
                ).props("flat round").tooltip("跨设备继续")

            ui.label("执行步骤").classes("section-title")
            for step in detail["steps"]:
                with ui.row().classes(
                    "w-full items-start justify-between border-b "
                    "border-grey-2 py-3 gap-3 no-wrap"
                ):
                    with ui.column().classes("gap-1 min-w-0"):
                        ui.label(
                            f"{step['sequence']}. {step['kind']}"
                        ).classes("text-sm font-medium")
                        meta = [
                            step.get("executor"),
                            (
                                f"执行 {step['attempt_count']} 次"
                                if step.get("attempt_count")
                                else ""
                            ),
                            step.get("artifact_locator"),
                        ]
                        ui.label(" · ".join(value for value in meta if value)).classes(
                            "text-xs text-grey-6 break-all"
                        )
                    _status_badge(step["status"])

            ui.label("核验证据").classes("section-title mt-4")
            if not detail["evidence"]:
                ui.label("尚无合格证据，因此系统不会声称完成。").classes(
                    "text-sm text-grey-6"
                )
            for evidence in reversed(detail["evidence"]):
                with ui.element("div").classes(
                    "w-full border-b border-grey-2 py-3"
                ):
                    with ui.row().classes(
                        "w-full items-center justify-between gap-2"
                    ):
                        ui.label(evidence["evidence_type"]).classes(
                            "text-sm font-medium"
                        )
                        ui.badge(
                            "匹配" if evidence["matched"] else "不匹配",
                            color="positive" if evidence["matched"] else "negative",
                        ).props("outline")
                    ui.label(
                        f"{evidence['strength']} · {evidence['authority']} · "
                        f"{_time(evidence['observed_at'])}"
                    ).classes("text-xs text-grey-6")
                    ui.label(evidence["observed_summary"]).classes(
                        "text-xs break-words mt-1"
                    )

        body()
        ui.timer(10, body.refresh)


def json_compact(value) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, sort_keys=True)
