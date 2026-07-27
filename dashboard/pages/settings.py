"""更多 — 次级面板入口、真实配置与安全手机接入。

The previous incarnation was a placebo settings page: it wrote 7 keys into
kv_store (0 rows ever written in production, zero consumers anywhere in the
codebase) while claiming "changes take effect immediately". Those controls
are gone (REQ-43/46). What remains is the truth: where each behavior is
actually configured, and its current live value.

This page also adopts the four secondary surfaces (用量/互动/开放问题/任务健康)
— /usage and /engagement previously had zero inbound links anywhere.
"""

import base64
import io
import json
import time
from pathlib import Path

from nicegui import run, ui

from ..uiutil import client_surface, jarvis_page, notify_safely

JARVIS_DIR = Path(__file__).parent.parent.parent

# 次级面板入口：名称、路由、一句人话说明。
_PANELS = [
    ("用量", "/usage", "这台电脑上 Claude 的使用量：会话、消耗、什么时段最活跃。"),
    ("互动", "/engagement", "Jarvis 主动发的消息，你看了没、回了没，哪些来源在打扰你。"),
    ("开放问题", "/thinking", "还没想明白的事，和进行中的个人项目。"),
    ("任务健康", "/agent-calendar", "每个后台任务实际跑没跑、失败了几次、卡在哪。"),
]


def _quiet_hours() -> tuple[str, str]:
    """Live quiet-hours window from the shared policy that enforces it."""
    from core.attention_policy import quiet_window_labels
    return quiet_window_labels()


def _interval_overrides(jarvis_dir: Path | None = None) -> list[dict]:
    """interval_overrides.json + applied-at metadata, joined with base intervals."""
    jd = jarvis_dir or JARVIS_DIR
    try:
        overrides = json.loads((jd / "interval_overrides.json").read_text())
    except (OSError, json.JSONDecodeError, ValueError):
        overrides = {}
    try:
        meta = json.loads((jd / "interval_overrides_meta.json").read_text())
    except (OSError, json.JSONDecodeError, ValueError):
        meta = {}
    base = {}
    try:
        from core.heartbeat import parse_heartbeat
        hb = jd / "HEARTBEAT.md"
        if hb.exists():
            base = {t["name"]: t["interval"] for t in parse_heartbeat(hb)}
    except ImportError:
        pass
    out = []
    for task, secs in sorted(overrides.items()):
        applied = meta.get(task)
        out.append({
            "task": task,
            "override_s": secs,
            "base_s": base.get(task),
            "applied_at": time.strftime("%Y-%m-%d %H:%M", time.localtime(applied)) if applied else "未记录",
        })
    return out


def _fmt_secs(secs) -> str:
    if secs is None:
        return "未知"
    if secs < 3600:
        return f"{secs // 60} 分钟"
    if secs < 86400:
        return f"{secs / 3600:g} 小时"
    return f"{secs / 86400:g} 天"


def _pair_qr_data_url(value: str) -> str:
    if not value:
        return ""
    try:
        import qrcode
    except ImportError:
        return ""
    image = qrcode.make(value)
    output = io.BytesIO()
    image.save(output, format="PNG")
    payload = base64.b64encode(output.getvalue()).decode("ascii")
    return f"data:image/png;base64,{payload}"


def _section(kicker: str, title: str, note: str = "") -> None:
    ui.label(kicker).classes("section-kicker")
    ui.label(title).classes("section-title")
    if note:
        ui.label(note).classes("section-note")


def reconcile_browser_push(device_id: str, browser_result) -> dict:
    """Fold browser permission/subscription truth back into the local store."""
    if not isinstance(browser_result, dict):
        return {
            "checked": False, "enabled": False,
            "reason": "unavailable", "endpoint": "",
        }
    status = str(browser_result.get("status") or "")
    from core.mobile_access import register_push, unregister_push
    if status == "enabled":
        subscription = browser_result.get("subscription") or {}
        endpoint = str(
            subscription.get("endpoint") if isinstance(subscription, dict)
            else ""
        )
        try:
            register_push(device_id, subscription)
        except (TypeError, ValueError):
            return {
                "checked": True, "enabled": False,
                "reason": "invalid", "endpoint": "",
            }
        return {
            "checked": True, "enabled": True,
            "reason": "", "endpoint": endpoint,
        }
    if status in {"denied", "default", "missing", "unsupported"}:
        endpoint = str(browser_result.get("endpoint") or "")
        if endpoint:
            unregister_push(endpoint, device_id)
        return {
            "checked": True, "enabled": False,
            "reason": status, "endpoint": endpoint,
        }
    return {
        "checked": False, "enabled": False,
        "reason": "unavailable", "endpoint": "",
    }


def send_browser_test_notification(device_id: str, endpoint: str) -> dict:
    """Send a diagnostic Push only to the subscription in this browser."""
    endpoint = str(endpoint or "").strip()
    if not endpoint:
        return {
            "sent": 0,
            "failed": 0,
            "disabled": 0,
            "reason": "no_current_subscription",
        }
    from core.mobile_access import send_push
    return send_push(
        "Jarvis 已连接",
        "这是一条来自 Jarvis 的测试通知。",
        device_id=device_id,
        endpoint=endpoint,
    )


def push_test_feedback(result: dict) -> tuple[str, str]:
    """Map transport truth to a user-facing diagnostic and NiceGUI type."""
    if int(result.get("sent") or 0) > 0:
        return "推送服务已接收测试通知，请在设备上确认显示", "positive"
    reason = str(result.get("reason") or "")
    failed = int(result.get("failed") or 0)
    disabled = int(result.get("disabled") or 0)
    error = str(result.get("error") or "").strip()
    if failed == 0 and reason in {
        "no_current_subscription",
        "no_subscriber",
    }:
        return "这台设备还没有可用的通知订阅", "warning"
    if disabled > 0:
        return "这台设备的通知订阅已失效，请重新开启通知", "warning"
    if failed > 0 or error:
        detail = error or reason or "推送服务暂时不可用"
        return f"测试通知发送失败：{detail}", "negative"
    return "测试通知未能送达，请稍后重试", "warning"


@ui.page("/settings")
def settings_page():
    """次级面板入口、只读配置真相和设备安全动作。"""
    surface, actor = client_surface()
    with jarvis_page("/settings", "更多",
                     "次级面板、真实生效的配置，以及已授权的手机设备。"):

        # ── 次级面板入口 ──
        with ui.column().classes("w-full gap-3"):
            _section("壹 · 面板", "更多面板", "不常看、但要用时得找得到的几块。")
            with ui.element("div").classes("memorial-grid"):
                for name, href, desc in _PANELS:
                    with ui.link(target=href).classes("no-underline"):
                        with ui.card().classes("memorial-card w-full is-decided"
                                               " cursor-pointer"):
                            ui.label(name).classes("memorial-title")
                            ui.label(desc).classes("memorial-body")
                            ui.label("进入 →").classes("memorial-source")

        # ── 真实配置 ──
        with ui.column().classes("w-full gap-3"):
            _section("贰 · 配置", "真实生效的配置",
                     "每一项在哪里生效、要改去哪里改，下面如实写明。")

            # 免打扰时段 — core/heartbeat_loop.py 常量强制执行
            with ui.card().classes("w-full p-4"):
                start, end = _quiet_hours()
                ui.label("免打扰时段").classes("font-medium")
                ui.label(f"{start} → {end}（不着急的消息排队到次日再发）").classes(
                    "section-note")
                ui.label(
                    "生效机制：core/attention_policy.py 的统一静默时段，心跳和 "
                    "Push 共用。默认值在代码中；启动环境可用 "
                    "JARVIS_QUIET_START / JARVIS_QUIET_END 覆盖。"
                ).classes("text-xs text-gray-500")

            # 自动调频 — engagement-analyze 写 interval_overrides.json
            with ui.card().classes("w-full p-4"):
                ui.label("任务频率的自动微调").classes("font-medium")
                overrides = _interval_overrides()
                if overrides:
                    for o in overrides:
                        with ui.row().classes("w-full items-center justify-between"):
                            ui.label(o["task"]).classes("text-sm")
                            ui.label(
                                f"基准每 {_fmt_secs(o['base_s'])} → 现在每 "
                                f"{_fmt_secs(o['override_s'])} · 调整于 {o['applied_at']}"
                            ).classes("text-xs text-gray-500")
                else:
                    ui.label("当前没有微调——所有任务按 HEARTBEAT.md 的基准频率跑。").classes(
                        "section-note")
                ui.label(
                    "生效机制：engagement-analyze 任务根据互动数据写 "
                    "interval_overrides.json（3 天内不重复加码），心跳每轮读取。"
                    "手工调整：直接编辑 interval_overrides.json。"
                ).classes("text-xs text-gray-500 mt-1")

            # 任务节奏事实源
            with ui.card().classes("w-full p-4"):
                ui.label("任务节奏").classes("font-medium")
                ui.label("HEARTBEAT.md 是所有心跳任务的频率和内容的事实源。").classes(
                    "section-note")
                ui.label(
                    "查看运行情况：任务健康页。编辑：admin (:3456) 的心跳编辑器，"
                    "或直接改仓库里的 HEARTBEAT.md。"
                ).classes("text-xs text-gray-500")
                ui.link("任务健康 →", "/agent-calendar").classes("text-sm")

            # 系统信息（jarvis.yaml 只读摘要）
            config_path = JARVIS_DIR / "jarvis.yaml"
            if config_path.exists():
                with ui.card().classes("w-full p-4"):
                    ui.label("系统信息").classes("font-medium")
                    try:
                        import yaml
                    except ImportError:
                        ui.label("这台机器没装 PyYAML，读不了 jarvis.yaml——"
                                 "跳过这一块。").classes("section-note")
                    else:
                        try:
                            with open(config_path) as f:
                                config = yaml.safe_load(f)
                            if not isinstance(config, dict):
                                config = {}
                            ui.label(f"工作目录：{config.get('work_dir', '未设置')}").classes(
                                "text-xs font-mono")
                            ui.label("心跳模型："
                                     f"{config.get('claude', {}).get('heartbeat_model', '未设置')}").classes(
                                "text-xs font-mono")
                            ui.label("检查间隔："
                                     f"{config.get('heartbeat', {}).get('check_interval', '未设置')} 秒").classes(
                                "text-xs font-mono")
                            ui.label("Admin 端口："
                                     f"{config.get('admin', {}).get('port', '未设置')}").classes(
                                "text-xs font-mono")
                            ui.label("生效机制：jarvis.yaml，bot 启动时读取。").classes(
                                "text-xs text-gray-500 mt-1")
                        except (OSError, yaml.YAMLError):
                            ui.label("jarvis.yaml 存在但读不出来（文件损坏或没有读取权限）。"
                                     "修好文件后刷新这一页。").classes("section-note")

        # ── 数据操作（真实动作，保留） ──
        with ui.column().classes("w-full gap-3"):
            _section("叁 · 手机", "手机访问")
            pair_state = {"result": None}

            with ui.dialog() as pair_dialog, ui.card().classes(
                    "matter-dialog matter-dialog-small"):
                ui.label("连接一台手机").classes("matter-dialog-title")

                @ui.refreshable
                def pair_content():
                    result = pair_state["result"] or {}
                    if not result:
                        return
                    pair_url = str(result.get("pair_url") or "")
                    qr_url = _pair_qr_data_url(pair_url)
                    if qr_url:
                        ui.image(qr_url).classes("mobile-pair-qr")
                    ui.label(result.get("code", "")).classes("mobile-pair-code")
                    if pair_url:
                        ui.link("打开确认页", pair_url, new_tab=True).classes(
                            "matter-artifact-link")
                    if result.get("lan_pair_url"):
                        base = result["lan_pair_url"].split("/pair/", 1)[0]
                        ui.link("局域网备用入口", result["lan_pair_url"],
                                new_tab=True).classes("matter-artifact-link")
                        ui.link("局域网证书", f"{base}/mobile-ca.cer",
                                new_tab=True).classes("matter-artifact-link")
                    ui.label(f"有效至 {result.get('expires_at', '')}").classes("section-note")

                pair_content()
                ui.button("关闭", on_click=pair_dialog.close).props(
                    "flat no-caps").classes("memorial-secondary")

            def create_mobile_pair():
                from core.config import Config
                from core.mobile_access import create_pair_code
                cfg = Config()
                result = create_pair_code(f"{cfg.owner_name} 的手机", 15)
                try:
                    gateway = json.loads(
                        (Config().jarvis_dir / "mobile_access.json").read_text(
                            encoding="utf-8"))
                except (OSError, json.JSONDecodeError, ValueError):
                    gateway = {}
                lan_base = str(gateway.get("url") or "").rstrip("/")
                tailnet = gateway.get("tailnet") or {}
                tailnet_base = (str(tailnet.get("url") or "").rstrip("/")
                                if tailnet.get("ready") else "")
                result["lan_pair_url"] = (
                    f"{lan_base}/pair/{result['code']}" if lan_base else "")
                result["tailnet_pair_url"] = (
                    f"{tailnet_base}/pair/{result['code']}" if tailnet_base else "")
                result["pair_url"] = (
                    result["tailnet_pair_url"] or result["lan_pair_url"])
                pair_state["result"] = result
                pair_content.refresh()
                pair_dialog.open()

            browser_push_state = {
                "checked": False,
                "enabled": False,
                "reason": "",
                "endpoint": "",
            }

            async def enable_notifications():
                script = r"""
                return await (async () => {
                  if (!('serviceWorker' in navigator) || !('PushManager' in window))
                    return 'unsupported';
                  const permission = await Notification.requestPermission();
                  if (permission !== 'granted') return permission;
                  const keyData = await fetch('/api/mobile/vapid-public-key').then(r => r.json());
                  const pad = '='.repeat((4 - keyData.public_key.length % 4) % 4);
                  const raw = atob((keyData.public_key + pad).replace(/-/g, '+').replace(/_/g, '/'));
                  const key = Uint8Array.from([...raw].map(c => c.charCodeAt(0)));
                  const reg = await navigator.serviceWorker.ready;
                  let sub = await reg.pushManager.getSubscription();
                  if (!sub) sub = await reg.pushManager.subscribe({userVisibleOnly: true,
                    applicationServerKey: key});
                  localStorage.setItem('jarvisPushEndpoint', sub.endpoint);
                  return {status: 'ok', subscription: sub.toJSON()};
                })();
                """
                try:
                    result = await ui.run_javascript(script, timeout=30)
                except Exception:
                    result = "failed"
                if isinstance(result, dict) and result.get("status") == "ok":
                    state = reconcile_browser_push(actor, {
                        "status": "enabled",
                        "subscription": result.get("subscription") or {},
                    })
                elif isinstance(result, str) and result in {
                        "denied", "default", "unsupported"}:
                    state = reconcile_browser_push(
                        actor, {"status": result})
                else:
                    state = {
                        "checked": False,
                        "enabled": False,
                        "reason": "unavailable",
                        "endpoint": "",
                    }
                browser_push_state.update(state)
                if state["enabled"]:
                    notify_safely(
                        "这台设备的通知已开启", type="positive")
                    mobile_panel.refresh()
                elif state["reason"] == "denied":
                    notify_safely(
                        "浏览器已拒绝通知，请在系统设置中重新允许",
                        type="warning",
                    )
                else:
                    notify_safely(
                        "当前浏览器暂时不能开启 Push 通知",
                        type="warning",
                    )

            async def test_notifications():
                try:
                    result = await run.io_bound(
                        send_browser_test_notification,
                        actor,
                        browser_push_state["endpoint"],
                    )
                except Exception:
                    result = {
                        "sent": 0,
                        "failed": 1,
                        "error": "推送服务暂时不可用",
                    }
                if not isinstance(result, dict):
                    result = {
                        "sent": 0,
                        "failed": 1,
                        "error": "推送服务返回了无效结果",
                    }
                message, notification_type = push_test_feedback(result)
                notify_safely(message, type=notification_type)

            @ui.refreshable
            def mobile_panel():
                from core.mobile_access import (
                    list_devices,
                    push_subscription_status,
                )
                from core.tailnet import tailnet_status
                try:
                    gateway = json.loads((JARVIS_DIR / "mobile_access.json").read_text(
                        encoding="utf-8"))
                except (OSError, json.JSONDecodeError, ValueError):
                    gateway = {}
                saved_tailnet = gateway.get("tailnet") or {}
                live_tailnet = tailnet_status(
                    int(gateway.get("port") or 3458),
                    mode=saved_tailnet.get("mode") or "funnel",
                )
                if saved_tailnet.get("enable_url") and not live_tailnet.get("ready"):
                    live_tailnet["enable_url"] = saved_tailnet["enable_url"]
                    live_tailnet["enable_required"] = True
                paired_push_status = push_subscription_status(
                    paired_only=True)
                with ui.element("section").classes("mobile-access-band"):
                    with ui.row().classes("w-full items-center justify-between gap-3"):
                        with ui.column().classes("gap-1"):
                            ui.label("手机入口").classes("font-medium")
                            with ui.element("div").classes("mobile-access-route"):
                                ui.icon("wifi", size="17px")
                                ui.label("同一 Wi-Fi").classes("mobile-route-label")
                                ui.label(gateway.get("url") or "网关尚未运行").classes(
                                    "section-note")
                            with ui.element("div").classes("mobile-access-route"):
                                ui.icon("public", size="17px")
                                ui.label("外出访问").classes("mobile-route-label")
                                if live_tailnet.get("ready") and live_tailnet.get("url"):
                                    ui.link(live_tailnet["url"], live_tailnet["url"],
                                            new_tab=True).classes("matter-artifact-link")
                                else:
                                    ui.label(live_tailnet.get("detail") or "尚未配置").classes(
                                        "section-note")
                            if live_tailnet.get("enable_url"):
                                ui.link("启用安全公网入口",
                                        live_tailnet["enable_url"], new_tab=True).classes(
                                    "matter-artifact-link")
                        with ui.row().classes("gap-2"):
                            ui.button(icon="add_link", on_click=create_mobile_pair).props(
                                'outline round aria-label="连接新设备"').tooltip("连接新设备")
                            ui.button(icon="notifications_active",
                                      on_click=enable_notifications).props(
                                'outline round aria-label="开启此设备通知"').tooltip(
                                    "开启此设备通知")
                            ui.button(
                                icon="send",
                                on_click=test_notifications,
                            ).props(
                                'outline round aria-label="测试此设备通知"'
                            ).tooltip("测试此设备通知")
                    if not browser_push_state["checked"]:
                        notification_label = "正在核对这台设备的通知状态"
                    elif surface == "mobile":
                        notification_label = (
                            "这台手机的通知已开启"
                            if browser_push_state["enabled"]
                            else "这台手机的通知尚未开启"
                        )
                    else:
                        notification_label = (
                            "这台电脑的通知已开启"
                            if browser_push_state["enabled"]
                            else "这台电脑的通知尚未开启"
                        )
                        if paired_push_status["count"]:
                            notification_label += (
                                f" · {paired_push_status['count']} 台手机已开启")
                    with ui.element("div").classes("mobile-notification-status"):
                        ui.icon(
                            "notifications_active"
                            if browser_push_state["checked"]
                            and browser_push_state["enabled"]
                            else "notifications_off",
                            size="17px",
                        )
                        ui.label(notification_label).classes("section-note")
                    devices = list_devices()
                    if devices:
                        for device in devices:
                            with ui.row().classes("mobile-device-row"):
                                ui.icon("smartphone", size="18px")
                                with ui.column().classes("gap-0 min-w-0"):
                                    ui.label(device["label"]).classes("font-medium")
                                    ui.label(f"最近访问 {device.get('last_seen_at') or '尚未访问'}").classes(
                                        "section-note")

                                def revoke(device_id=device["id"]):
                                    from core.mobile_access import revoke_device
                                    revoke_device(device_id)
                                    mobile_panel.refresh()

                                ui.button(icon="link_off", on_click=revoke).props(
                                    "flat round dense").tooltip("撤销设备")
                    else:
                        ui.label("还没有已连接的手机。").classes("section-note")

            async def reconcile_notifications():
                script = r"""
                return await (async () => {
                  const endpoint = localStorage.getItem('jarvisPushEndpoint') || '';
                  if (!('Notification' in window) ||
                      !('serviceWorker' in navigator) ||
                      !('PushManager' in window))
                    return {status: 'unsupported', endpoint};
                  if (Notification.permission !== 'granted')
                    return {status: Notification.permission, endpoint};
                  const reg = await navigator.serviceWorker.ready;
                  const sub = await reg.pushManager.getSubscription();
                  if (!sub) return {status: 'missing', endpoint};
                  localStorage.setItem('jarvisPushEndpoint', sub.endpoint);
                  return {status: 'enabled', subscription: sub.toJSON()};
                })();
                """
                try:
                    result = await ui.run_javascript(script, timeout=10)
                except Exception:
                    result = None
                browser_push_state.update(
                    reconcile_browser_push(actor, result))
                mobile_panel.refresh()

            mobile_panel()
            ui.timer(0.2, reconcile_notifications, once=True)
            ui.timer(10, mobile_panel.refresh)

        # ── 数据操作（真实动作，保留） ──
        with ui.column().classes("w-full gap-3"):
            _section("肆 · 数据", "数据搬家")

            async def migrate_watchlater():
                from ..bookmark_pipeline import migrate_from_jsonl
                wl_path = JARVIS_DIR.parent / "memory" / "system" / "watchlater.jsonl"
                if not wl_path.exists():
                    # Try alternative path
                    from core.claude_projects import heartbeat_memory_dir
                    wl_path = heartbeat_memory_dir() / "system" / "watchlater.jsonl"
                count = migrate_from_jsonl(str(wl_path))
                ui.notify(f"已从 watchlater.jsonl 导入 {count} 条收藏", type="positive")

            ui.label("把旧的稍后看清单（watchlater.jsonl）导入收藏库。已导入过的不会重复。").classes(
                "section-note")
            ui.button("导入旧收藏", on_click=migrate_watchlater).classes(
                "memorial-secondary").props("outline no-caps")
