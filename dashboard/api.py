"""REST API for bot.sh integration.

Exposes endpoints that bot.sh and heartbeat tasks can call
to interact with the dashboard's SQLite store. This is the bridge
between the shell-based harness and the structured data layer.

Run as part of the NiceGUI app (same process, same port).
Or standalone for headless mode: python3 -m dashboard.api
"""

import json
import time
from pathlib import Path

from fastapi import Request
from nicegui import app

from core.timeutil import now_local_str

from .db import (
    bookmark_add, bookmark_list, bookmark_search, bookmark_update, bookmark_delete,
    log_event, log_list, task_list, task_register, task_update, task_delete,
    kv_get, kv_set, engagement_record, engagement_stats, get_db,
)
from .scheduler import get_due_tasks, mark_executed, register_alarm, register_recurring


def register_api_routes():
    """Register FastAPI routes on the NiceGUI app."""

    # ── Bookmarks ────────────────────────────────────────────────────

    @app.post("/api/bookmarks")
    async def api_bookmark_add(request: Request):
        """Add a bookmark. Body: {title, url?, source?, summary?, tags?}"""
        data = await request.json()
        bm_id = bookmark_add(
            title=data["title"],
            url=data.get("url", ""),
            source=data.get("source", "api"),
            summary=data.get("summary", ""),
            tags=data.get("tags"),
            content=data.get("content", ""),
        )
        return {"id": bm_id, "status": "ok"}

    @app.get("/api/bookmarks")
    async def api_bookmark_list(status: str = "", limit: int = 50, q: str = ""):
        """List or search bookmarks."""
        if q:
            items = bookmark_search(q, limit=limit)
        elif status:
            items = bookmark_list(status=status, limit=limit)
        else:
            items = bookmark_list(limit=limit)
        return {"items": items}

    @app.patch("/api/bookmarks/{bookmark_id}")
    async def api_bookmark_update(bookmark_id: int, request: Request):
        """Update a bookmark. Body: {status?, summary?, tags?}"""
        data = await request.json()
        bookmark_update(bookmark_id, **data)
        return {"status": "ok"}

    @app.delete("/api/bookmarks/{bookmark_id}")
    async def api_bookmark_delete(bookmark_id: int):
        """Delete a bookmark."""
        bookmark_delete(bookmark_id)
        return {"status": "ok"}

    # ── Agent Log ────────────────────────────────────────────────────

    @app.post("/api/log")
    async def api_log_event(request: Request):
        """Log an agent event. Body: {source, message, level?, context?}"""
        data = await request.json()
        log_event(
            source=data["source"],
            message=data["message"],
            level=data.get("level", "info"),
            context=data.get("context"),
        )
        return {"status": "ok"}

    @app.get("/api/log")
    async def api_log_list(source: str = "", limit: int = 100, since: str = ""):
        """Query agent logs."""
        items = log_list(source=source or None, limit=limit, since=since or None)
        return {"items": items}

    # ── Scheduled Tasks ──────────────────────────────────────────────

    @app.get("/api/tasks")
    async def api_task_list(category: str = ""):
        """List scheduled tasks."""
        items = task_list(category=category or None)
        return {"items": items}

    @app.post("/api/tasks")
    async def api_task_register(request: Request):
        """Register a new task. Body: {id, name, trigger_type, trigger_config, ...}"""
        data = await request.json()
        task_register(
            task_id=data["id"],
            name=data["name"],
            trigger_type=data["trigger_type"],
            trigger_config=data["trigger_config"],
            action_type=data.get("action_type", "prompt"),
            action_config=data.get("action_config", {}),
            conditions=data.get("conditions", []),
            category=data.get("category", "user"),
            priority=data.get("priority", 5),
        )
        return {"status": "ok", "id": data["id"]}

    @app.post("/api/tasks/{task_id}/execute")
    async def api_task_mark_executed(task_id: str, request: Request):
        """Mark a task as executed."""
        data = await request.json()
        mark_executed(task_id, result=data.get("result", ""))
        return {"status": "ok"}

    @app.delete("/api/tasks/{task_id}")
    async def api_task_delete(task_id: str):
        """Delete a task."""
        task_delete(task_id)
        return {"status": "ok"}

    @app.get("/api/tasks/due")
    async def api_tasks_due():
        """Get tasks that are due now."""
        due = get_due_tasks()
        return {"items": due}

    # ── Convenience: alarms and recurring ────────────────────────────

    @app.post("/api/tasks/alarm")
    async def api_alarm(request: Request):
        """Create a one-shot alarm. Body: {name, datetime, message}"""
        from datetime import datetime as dt
        data = await request.json()
        target = dt.fromisoformat(data["datetime"])
        task_id = register_alarm(
            name=data["name"],
            dt=target,
            action_config={"message": data.get("message", data["name"])},
        )
        return {"status": "ok", "id": task_id}

    @app.post("/api/tasks/recurring")
    async def api_recurring(request: Request):
        """Create a recurring task. Body: {name, cron, action_type, action_config, conditions?}"""
        data = await request.json()
        task_id = register_recurring(
            name=data["name"],
            cron_expr=data["cron"],
            action_type=data.get("action_type", "notify"),
            action_config=data.get("action_config", {}),
            conditions=data.get("conditions"),
            priority=data.get("priority", 5),
        )
        return {"status": "ok", "id": task_id}

    # ── KV Store ─────────────────────────────────────────────────────

    @app.get("/api/kv/{key}")
    async def api_kv_get(key: str):
        """Get a KV value."""
        return {"key": key, "value": kv_get(key)}

    @app.post("/api/kv/{key}")
    async def api_kv_set(key: str, request: Request):
        """Set a KV value. Body: {value}"""
        data = await request.json()
        kv_set(key, data["value"])
        return {"status": "ok"}

    # ── Engagement ───────────────────────────────────────────────────

    @app.post("/api/engagement")
    async def api_engagement_record(request: Request):
        """Record engagement event. Body: {event_type, source?, engaged?, gap_seconds?, metadata?}"""
        data = await request.json()
        engagement_record(
            event_type=data["event_type"],
            source=data.get("source", ""),
            engaged=data.get("engaged", False),
            gap_seconds=data.get("gap_seconds", 0),
            metadata=data.get("metadata"),
        )
        return {"status": "ok"}

    @app.get("/api/engagement/stats")
    async def api_engagement_stats(days: int = 7):
        """Get engagement statistics."""
        return engagement_stats(days)

    # ── Health ───────────────────────────────────────────────────────

    @app.get("/api/health")
    async def api_health():
        """Health check."""
        return {
            "status": "ok",
            "timestamp": now_local_str("%Y-%m-%dT%H:%M:%S"),
            "db": "connected",
        }
