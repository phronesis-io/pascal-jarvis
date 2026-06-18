"""Home page — Timeline Feed + Ambient Status.

Not a dashboard. A living timeline of "what happened while you were away".

REQ-46: the feed reads the LIVE streams — sched_events.jsonl (task finishes/
skips + intent_* lifecycle events) merged with heartbeat_outbox.jsonl (real
deliveries) — instead of the agent_log table (4 rows, frozen 2026-05-21).
The status dot derives from the age of the newest sched_events entry, not
from heartbeat_state watermarks. ui.timer(10) refresh.
"""

import json
import time
from pathlib import Path

from nicegui import ui

from ..db import engagement_stats, bookmark_list
from ..telemetry import read_jsonl_tail, read_sched_events, read_json

JARVIS_DIR = Path(__file__).parent.parent.parent


def _selfmon_headline(jarvis_dir: Path) -> dict:
    """Headline self-monitoring numbers from core.selfmon (REQ-67).

    Logic lives in core.selfmon (computed from LIVE sources, not the dead
    tables). This is a thin, defensive adapter: any read/import failure → an
    'n/a' shape so the panel always renders.
    """
    try:
        from core.selfmon import compute_selfmon
        m = compute_selfmon(jarvis_dir, window_hours=24)
        overdue = m["closure_overdue"]
        return {
            "noise_cards": m["noise_card_count"]["total_sent"],
            "low_engagement": len(m["noise_card_count"]["low_engagement_sources"]),
            "refires": m["same_intent_refires"]["offender_count"],
            "closure_overdue": (overdue["overdue_count"]
                                if overdue.get("db_available") else "n/a"),
            "crashes": m["model_crash_or_skip"]["total"],
            "silent": m["silent_failures"]["total"],
            "ok": True,
        }
    except Exception:  # noqa: BLE001 — panel must never break the home page
        return {"ok": False}

# feed 只收用户可理解的事件；task_spawn/batch_flush 是机器节拍，太吵
_FEED_EVENTS = ("task_finish", "task_skip", "task_timeout")


def _parse_epoch(ts: str) -> float | None:
    """'YYYY-MM-DD HH:MM[:SS]' local string → epoch."""
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return time.mktime(time.strptime(ts, fmt))
        except (ValueError, TypeError):
            continue
    return None


def build_activity_feed(jarvis_dir: str | Path, limit: int = 30) -> list[dict]:
    """Merge sched_events + outbox deliveries into one feed, newest first.

    Each entry: {ts, epoch, source, message}. Pure read — no DB, no writes.
    """
    jd = Path(jarvis_dir)
    feed: list[dict] = []

    # ── sched_events: 任务终态 + intent_* 生命周期 ──
    for e in read_sched_events(jd)[-400:]:
        ev = str(e.get("event", ""))
        ts = str(e.get("ts", ""))
        epoch = _parse_epoch(ts)
        if epoch is None:
            continue
        task = str(e.get("task", "") or "")
        if ev == "task_finish":
            status = str(e.get("status", "ok"))
            dur = e.get("duration_s", "")
            mark = "✓" if status == "ok" else "✗"
            feed.append({"ts": ts, "epoch": epoch, "source": "task",
                         "message": f"{mark} {task} {status}"
                                    + (f" ({dur}s)" if dur != "" else "")})
        elif ev == "task_skip":
            feed.append({"ts": ts, "epoch": epoch, "source": "skip",
                         "message": f"{task} skipped: {e.get('reason', '?')}"})
        elif ev == "task_timeout":
            feed.append({"ts": ts, "epoch": epoch, "source": "task",
                         "message": f"⏱ {task} timeout"})
        elif ev.startswith("intent_"):
            name = str(e.get("name", "") or task)
            feed.append({"ts": ts, "epoch": epoch, "source": "intent",
                         "message": f"{ev.removeprefix('intent_')}: {name}"})

    # ── heartbeat_outbox: 实际送达的消息 ──
    for o in read_jsonl_tail(jd / "heartbeat_outbox.jsonl")[-50:]:
        ts = str(o.get("ts", ""))
        epoch = _parse_epoch(ts)
        if epoch is None:
            continue
        text = str(o.get("text", "")).replace("\n", " ")
        if len(text) > 90:
            text = text[:90] + "…"
        feed.append({"ts": ts, "epoch": epoch, "source": "sent", "message": text})

    feed.sort(key=lambda x: x["epoch"], reverse=True)
    return feed[:limit]


def derive_status(jarvis_dir: str | Path, now_ts: float | None = None) -> tuple[str, str]:
    """(label, color) from the age of the newest sched_events entry.

    The event log is second-fresh whenever the heartbeat lives, so its age
    IS liveness — unlike heartbeat_state watermarks, which a wedged loop
    can leave looking healthy.
    """
    now_ts = now_ts if now_ts is not None else time.time()
    events = read_sched_events(jarvis_dir)
    newest = 0.0
    for e in events[-50:]:
        epoch = _parse_epoch(str(e.get("ts", "")))
        if epoch is not None and epoch > newest:
            newest = epoch
    if not newest:
        return "offline", "red"
    age = now_ts - newest
    if age < 120:
        return "active", "green"
    if age < 600:
        return "idle", "amber"
    return "stale", "red"


def _format_relative_time(epoch: float, now_ts: float | None = None) -> str:
    """Format epoch as relative time (e.g., '3m ago')."""
    now_ts = now_ts if now_ts is not None else time.time()
    delta = max(now_ts - epoch, 0)
    if delta < 60:
        return "just now"
    elif delta < 3600:
        return f"{int(delta / 60)}m ago"
    elif delta < 86400:
        return f"{int(delta / 3600)}h ago"
    else:
        return f"{int(delta / 86400)}d ago"


@ui.page("/")
def home_page():
    """Main dashboard page."""
    ui.add_head_html("""
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; }
        .timeline-entry { border-left: 2px solid #e5e7eb; padding-left: 1rem; margin-bottom: 0.75rem; }
        .timeline-entry:hover { border-left-color: #3b82f6; }
        .status-dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }
        .stat-card { background: #f9fafb; border-radius: 12px; padding: 1rem; }
    </style>
    """)

    with ui.column().classes("w-full max-w-4xl mx-auto p-4 gap-4"):

        @ui.refreshable
        def live_content():
            # Header
            with ui.row().classes("w-full items-center justify-between"):
                ui.label("Jarvis").classes("text-2xl font-bold")
                status, color = derive_status(JARVIS_DIR)
                with ui.row().classes("items-center gap-2"):
                    ui.html(f'<span class="status-dot" style="background:{color}"></span>')
                    ui.label(status).classes("text-sm text-gray-500")

            # Quick stats row
            stats = engagement_stats(7)
            with ui.row().classes("w-full gap-4"):
                with ui.card().classes("stat-card flex-1"):
                    ui.label(f"{stats['rate']}%").classes("text-2xl font-bold")
                    ui.label("7d engagement").classes("text-xs text-gray-500")

                bookmarks_count = len(bookmark_list(status="inbox"))
                with ui.card().classes("stat-card flex-1"):
                    ui.label(str(bookmarks_count)).classes("text-2xl font-bold")
                    ui.label("inbox items").classes("text-xs text-gray-500")

                state = read_json(JARVIS_DIR / "heartbeat_state.json", ttl=10, default={}) or {}
                active_tasks = sum(1 for s in state.values()
                                   if time.time() - s.get("last_run", 0) < 3600)
                with ui.card().classes("stat-card flex-1"):
                    ui.label(str(active_tasks)).classes("text-2xl font-bold")
                    ui.label("active tasks").classes("text-xs text-gray-500")

            # 🩺 Self-monitoring (24h) — headline numbers from core.selfmon,
            # computed from LIVE sources (REQ-67). Defensive: any failure → n/a.
            sm = _selfmon_headline(JARVIS_DIR)
            with ui.card().classes("stat-card w-full mt-2"):
                ui.label("🩺 Self-monitoring (24h)").classes("text-sm font-semibold")
                if not sm.get("ok"):
                    ui.label("n/a").classes("text-xs text-gray-400 italic")
                else:
                    def _metric(label: str, value, alert: bool = False):
                        with ui.column().classes("items-center gap-0"):
                            cls = "text-xl font-bold" + (
                                " text-red-600" if alert and value not in (0, "n/a") else "")
                            ui.label(str(value)).classes(cls)
                            ui.label(label).classes("text-xs text-gray-500")

                    with ui.row().classes("w-full gap-6 justify-around"):
                        _metric("noise cards", sm["noise_cards"])
                        _metric("re-fires", sm["refires"], alert=True)
                        _metric("closure overdue", sm["closure_overdue"], alert=True)
                        _metric("crashes/skips", sm["crashes"], alert=True)
                        _metric("silent fails", sm["silent"], alert=True)

            # Activity timeline — 活数据: sched_events + 真实送达
            ui.label("Recent Activity").classes("text-lg font-semibold mt-4")

            feed = build_activity_feed(JARVIS_DIR, limit=30)
            now_ts = time.time()
            if feed:
                badge_colors = {
                    "task": "blue",
                    "skip": "amber",
                    "intent": "purple",
                    "sent": "green",
                }
                for entry in feed:
                    with ui.element("div").classes("timeline-entry"):
                        with ui.row().classes("items-center gap-2"):
                            source = entry["source"]
                            ui.badge(source, color=badge_colors.get(source, "gray")).classes("text-xs")
                            ui.label(entry["message"]).classes("text-sm flex-1")
                            ui.label(_format_relative_time(entry["epoch"], now_ts)).classes(
                                "text-xs text-gray-400"
                            )
            else:
                ui.label("No activity yet — sched_events.jsonl is empty.").classes(
                    "text-gray-400 text-sm italic"
                )

        live_content()
        ui.timer(10, live_content.refresh)

        # Navigation
        ui.separator()
        with ui.row().classes("gap-4"):
            ui.link("Tasks", "/tasks").classes("text-blue-600")
            ui.link("Task Health", "/agent-calendar").classes("text-green-600")
            ui.link("Engagement", "/engagement").classes("text-green-600")
            ui.link("Thinking", "/thinking").classes("text-purple-600")
            ui.link("Intentions", "/intentions").classes("text-purple-600")
            ui.link("Bookmarks", "/bookmarks").classes("text-blue-600")
            ui.link("Settings", "/settings").classes("text-blue-600")
