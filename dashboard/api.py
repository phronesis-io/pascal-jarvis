"""REST API for bot.sh integration.

Exposes endpoints that bot.sh and heartbeat tasks can call
to interact with the dashboard's SQLite store. This is the bridge
between the shell-based harness and the structured data layer.

Run as part of the NiceGUI app (same process, same port).
Or standalone for headless mode: python3 -m dashboard.api
"""

import os
import re
import time
from pathlib import Path

from fastapi import Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from nicegui import app

from core.timeutil import now_local_str

from .db import (
    bookmark_add, bookmark_list, bookmark_search, bookmark_update, bookmark_delete,
    log_event, log_list, task_list, task_register, task_update, task_delete,
    kv_get, kv_set, engagement_record, engagement_stats, get_db,
)
from .scheduler import (
    get_due_tasks, mark_executed, register_alarm, register_recurring,
    validate_trigger,
)

_LOCAL_ORIGIN_RE = re.compile(r"^https?://(127\.0\.0\.1|localhost)(:\d+)?$")


async def _write_guard(request: Request) -> None:
    """Shared guard on all write routes — blocks cross-site blind POSTs.

    There is no auth and FastAPI parses JSON regardless of Content-Type, so
    any webpage could fire a CORS "simple request" (text/plain POST, no
    preflight) at 127.0.0.1:3457. Requiring application/json forces browsers
    into a preflight we never answer; a foreign Origin is rejected outright.
    curl/bot.sh (no Origin, application/json) are unaffected. DELETE has no
    body and is never a simple request (always preflights), so it only needs
    the Origin check.
    """
    if request.method in ("POST", "PATCH", "PUT"):
        ctype = request.headers.get("content-type", "")
        if not ctype.lower().startswith("application/json"):
            raise HTTPException(415, "Content-Type must be application/json")
    origin = request.headers.get("origin")
    if origin and not _LOCAL_ORIGIN_RE.match(origin):
        raise HTTPException(403, "cross-origin write rejected")


_WRITE = [Depends(_write_guard)]


def _validate_task_input(trigger_type: str, trigger_config,
                         action_type: str) -> None:
    """Reject malformed triggers and executor-less actions with a 400."""
    err = validate_trigger(trigger_type, trigger_config)
    if err:
        raise HTTPException(400, err)
    if action_type != "notify":
        # No executor exists for prompt/script — registering one would be a
        # silent no-op forever marked "executed".
        raise HTTPException(
            400, f"action_type {action_type!r} has no executor; only 'notify' is supported")


def register_api_routes():
    """Register FastAPI routes on the NiceGUI app."""

    # ── Bookmarks ────────────────────────────────────────────────────

    @app.post("/api/bookmarks", dependencies=_WRITE)
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

    @app.patch("/api/bookmarks/{bookmark_id}", dependencies=_WRITE)
    async def api_bookmark_update(bookmark_id: int, request: Request):
        """Update a bookmark. Body: {status?, summary?, tags?}"""
        data = await request.json()
        bookmark_update(bookmark_id, **data)
        return {"status": "ok"}

    @app.delete("/api/bookmarks/{bookmark_id}", dependencies=_WRITE)
    async def api_bookmark_delete(bookmark_id: int):
        """Delete a bookmark."""
        bookmark_delete(bookmark_id)
        return {"status": "ok"}

    # ── Agent Log ────────────────────────────────────────────────────

    @app.post("/api/log", dependencies=_WRITE)
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

    @app.post("/api/tasks", dependencies=_WRITE)
    async def api_task_register(request: Request):
        """Register a new task. Body: {id, name, trigger_type, trigger_config, ...}"""
        data = await request.json()
        action_type = data.get("action_type", "notify")
        _validate_task_input(data["trigger_type"], data["trigger_config"], action_type)
        task_register(
            task_id=data["id"],
            name=data["name"],
            trigger_type=data["trigger_type"],
            trigger_config=data["trigger_config"],
            action_type=action_type,
            action_config=data.get("action_config", {}),
            conditions=data.get("conditions", []),
            category=data.get("category", "user"),
            priority=data.get("priority", 5),
        )
        return {"status": "ok", "id": data["id"]}

    @app.post("/api/tasks/{task_id}/execute", dependencies=_WRITE)
    async def api_task_mark_executed(task_id: str, request: Request):
        """Mark a task as executed."""
        data = await request.json()
        mark_executed(task_id, result=data.get("result", ""))
        return {"status": "ok"}

    @app.delete("/api/tasks/{task_id}", dependencies=_WRITE)
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

    @app.post("/api/tasks/alarm", dependencies=_WRITE)
    async def api_alarm(request: Request):
        """Create a one-shot alarm. Body: {name, datetime, message}"""
        from datetime import datetime as dt
        data = await request.json()
        _validate_task_input("date", {"datetime": data.get("datetime", "")}, "notify")
        target = dt.fromisoformat(data["datetime"])
        task_id = register_alarm(
            name=data["name"],
            dt=target,
            action_config={"message": data.get("message", data["name"])},
        )
        return {"status": "ok", "id": task_id}

    @app.post("/api/tasks/recurring", dependencies=_WRITE)
    async def api_recurring(request: Request):
        """Create a recurring task. Body: {name, cron, action_type, action_config, conditions?}"""
        data = await request.json()
        _validate_task_input("cron", {"expression": data.get("cron", "")},
                             data.get("action_type", "notify"))
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

    @app.post("/api/kv/{key}", dependencies=_WRITE)
    async def api_kv_set(key: str, request: Request):
        """Set a KV value. Body: {value}"""
        data = await request.json()
        kv_set(key, data["value"])
        return {"status": "ok"}

    # ── Engagement ───────────────────────────────────────────────────

    @app.post("/api/engagement", dependencies=_WRITE)
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

    # ── Matters and work sessions ─────────────────────────────────────

    @app.get("/api/matters")
    async def api_matter_list(status: str = "", limit: int = 100):
        """List durable work matters, optionally filtered by status."""
        from core.matters import list_matters
        try:
            items = list_matters(status=status or None, limit=limit)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"items": items}

    @app.post("/api/matters", dependencies=_WRITE)
    async def api_matter_create(request: Request):
        """Create a matter. Body: {title, summary?, next_action?, ...}."""
        from core.matters import create_matter
        data = await request.json()
        try:
            matter = create_matter(
                title=data.get("title", ""),
                summary=data.get("summary", ""),
                next_action=data.get("next_action", ""),
                kind=data.get("kind", "project"),
                status=data.get("status", "active"),
                priority=data.get("priority", 5),
                source=data.get("source", "api"),
                actor=data.get("actor", "api"),
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return matter

    @app.get("/api/matters/{matter_id}")
    async def api_matter_get(matter_id: str):
        """Return a matter with its linked entities and event trail."""
        from core.matters import get_matter
        matter = get_matter(matter_id)
        if matter is None:
            raise HTTPException(404, "matter not found")
        return matter

    @app.patch("/api/matters/{matter_id}", dependencies=_WRITE)
    async def api_matter_update(matter_id: str, request: Request):
        """Update a matter's state, summary, next action, or outcome."""
        from core.matters import MatterConflict, update_matter
        data = await request.json()
        actor = data.pop("actor", "api")
        try:
            force = bool(data.pop("force", False))
            return update_matter(matter_id, actor=actor, force=force, **data)
        except KeyError as exc:
            raise HTTPException(404, "matter not found") from exc
        except MatterConflict as exc:
            raise HTTPException(409, {"message": str(exc),
                                      "open_items": exc.open_items}) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/matters/{matter_id}/links", dependencies=_WRITE)
    async def api_matter_link(matter_id: str, request: Request):
        """Attach a provider entity such as a Claude or Codex session."""
        from core.matters import link_entity
        data = await request.json()
        try:
            return link_entity(
                matter_id=matter_id,
                entity_type=data.get("entity_type", ""),
                entity_id=data.get("entity_id", ""),
                provider=data.get("provider", ""),
                title=data.get("title", ""),
                metadata=data.get("metadata"),
                actor=data.get("actor", "api"),
                move=bool(data.get("move", False)),
            )
        except KeyError as exc:
            raise HTTPException(404, "matter not found") from exc
        except ValueError as exc:
            detail = str(exc)
            status_code = 409 if "already linked" in detail else 400
            raise HTTPException(status_code, detail) from exc

    @app.delete("/api/matters/{matter_id}/links/{link_id}", dependencies=_WRITE)
    async def api_matter_unlink(matter_id: str, link_id: int):
        """Detach one linked entity without deleting its provider-native data."""
        from core.matters import unlink_entity
        if not unlink_entity(matter_id, link_id, actor="api"):
            raise HTTPException(404, "matter link not found")
        return {"status": "ok"}

    @app.get("/api/matters/{matter_id}/artifacts/{link_id}")
    async def api_matter_artifact(matter_id: str, link_id: int):
        """Download only a file explicitly linked to this Matter."""
        from fastapi.responses import FileResponse
        row = get_db().execute(
            "SELECT entity_id, title FROM matter_links WHERE id=? AND matter_id=? "
            "AND entity_type='artifact' AND provider='file'",
            (link_id, matter_id),
        ).fetchone()
        if not row:
            raise HTTPException(404, "artifact not found")
        path = Path(row["entity_id"]).expanduser()
        if not path.is_file():
            raise HTTPException(404, "artifact file is unavailable")
        return FileResponse(path, filename=path.name)

    @app.get("/api/work-sessions")
    async def api_work_sessions(provider: str = "", days: int = 30,
                                limit: int = 30):
        """Discover recent Claude Code and Codex sessions by metadata only."""
        from core.work_sessions import discover_sessions
        try:
            items = discover_sessions(provider=provider, days=days, limit=limit)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"items": items}

    @app.get("/api/matters/{matter_id}/context")
    async def api_matter_context(matter_id: str, format: str = "json"):
        """Return the bounded handoff bundle, never raw transcripts or memory."""
        from fastapi.responses import PlainTextResponse
        from core.matter_context import build_context_bundle, render_context_markdown
        try:
            bundle = build_context_bundle(matter_id)
        except KeyError as exc:
            raise HTTPException(404, "matter not found") from exc
        if format == "markdown":
            return PlainTextResponse(render_context_markdown(bundle))
        return bundle

    @app.get("/api/matters/{matter_id}/bindings")
    async def api_matter_bindings(matter_id: str):
        from core.matter_bridge import bindings_for_matter, lark_deep_link
        items = bindings_for_matter(matter_id)
        for item in items:
            item["deep_link"] = lark_deep_link(item)
        return {"items": items}

    # ── Memorial-first Items and delivery state ─────────────────────

    @app.get("/api/items")
    async def api_item_list(mode: str = "pending", topic_id: str = "",
                            time_window: str = "all", surface: str = "",
                            limit: int = 100):
        from core.config import Config
        from core.intent_lifecycle import list_intents
        from core.matters import list_matters
        from dashboard.pages.items import enrich_items, filter_items
        from dashboard.telemetry import memorial_states

        root = Path(os.environ.get("JARVIS_DIR") or Config().jarvis_dir)
        matters = list_matters(limit=300)
        intents = list_intents(limit=500)
        rows = get_db().execute(
            "SELECT entity_id,matter_id FROM matter_links "
            "WHERE entity_type='intent' AND provider='jarvis'"
        ).fetchall()
        intent_topics = {
            str(row["entity_id"]): str(row["matter_id"]) for row in rows
        }
        items = filter_items(
            enrich_items(
                memorial_states(root), matters=matters, intents=intents,
                intent_topics=intent_topics,
            ),
            mode=mode, topic_id=topic_id, time_window=time_window,
            surface=surface,
        )
        return {"items": items[:max(1, min(int(limit), 500))]}

    @app.post("/api/items/{memorial_id}/chat", dependencies=_WRITE)
    async def api_item_chat(memorial_id: str, request: Request):
        from core.memorial import chat
        await request.json()
        return chat(memorial_id)

    @app.get("/api/deliveries")
    async def api_delivery_list(state: str = "", limit: int = 100):
        from core.delivery import DeliveryPipeline
        return {"items": DeliveryPipeline().list(state=state, limit=limit)}

    @app.post("/api/deliveries/{delivery_id}/confirm", dependencies=_WRITE)
    async def api_delivery_confirm(delivery_id: str, request: Request):
        from core.delivery import DeliveryPipeline
        data = await request.json()
        try:
            result = DeliveryPipeline().confirm(
                delivery_id, str(data.get("state", "read")))
        except KeyError as exc:
            raise HTTPException(404, "delivery not found") from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {
            "delivery_id": result.delivery_id,
            "state": result.state,
            "channel": result.channel,
        }

    # ── Cross-device continuity ──────────────────────────────────────
    #
    # Trust boundary (REQ-120): the mobile gateway that used to authenticate
    # a device and stamp X-Jarvis-Device is retired, so that header has no
    # authenticator behind it anymore. These endpoints therefore operate as
    # the local desktop surface only — a client-supplied header is at most an
    # unverified hint and never switches the surface identity.

    @app.get("/api/handoffs")
    async def api_handoff_list(target_surface: str = "",
                               status: str = "active",
                               limit: int = 100):
        from core.continuity import list_handoffs
        try:
            items = list_handoffs(
                target_surface=target_surface, status=status, limit=limit)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"items": items}

    @app.post("/api/handoffs", dependencies=_WRITE)
    async def api_handoff_create(request: Request):
        """Create a handoff as the desktop surface (see trust note above)."""
        from core.continuity import create_handoff
        data = await request.json()
        from_surface = "desktop"
        created_by = "local"
        requested_source = str(data.get("from_surface", "") or "").strip()
        if requested_source and requested_source != from_surface:
            raise HTTPException(
                403, "only the desktop surface can create handoffs "
                     "(REQ-120: no authenticated mobile caller exists)")
        try:
            return await run_in_threadpool(
                create_handoff,
                str(data.get("entity_type", "")),
                str(data.get("entity_id", "")),
                from_surface=from_surface,
                to_surface=str(data.get("to_surface", "")),
                title=str(data.get("title", "")),
                matter_id=str(data.get("matter_id", "")),
                note=str(data.get("note", "")),
                created_by=created_by,
                metadata=(
                    data.get("metadata")
                    if isinstance(data.get("metadata"), dict) else {}
                ),
            )
        except KeyError as exc:
            raise HTTPException(404, "handoff entity not found") from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/handoffs/{handoff_id}/claim", dependencies=_WRITE)
    async def api_handoff_claim(handoff_id: str, request: Request):
        """Claim a handoff as the desktop surface (see trust note above)."""
        from core.continuity import claim_handoff
        data = await request.json()
        surface = "desktop"
        requested_surface = str(data.get("surface", "") or "").strip()
        if requested_surface and requested_surface != surface:
            raise HTTPException(
                403, "only the desktop surface can claim handoffs "
                     "(REQ-120: no authenticated mobile caller exists)")
        try:
            return claim_handoff(handoff_id, surface=surface)
        except KeyError as exc:
            raise HTTPException(404, "handoff not found") from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/handoffs/{handoff_id}/complete", dependencies=_WRITE)
    async def api_handoff_complete(handoff_id: str, request: Request):
        """Complete a desktop-bound handoff (see trust note above).

        Legacy mobile-bound rows are not completable here — they close
        through their entity's terminal projection instead.
        """
        from core.continuity import complete_handoff, get_handoff
        await request.json()
        surface = "desktop"
        handoff = get_handoff(handoff_id)
        if handoff is None:
            raise HTTPException(404, "handoff not found")
        if handoff.get("to_surface") != surface:
            raise HTTPException(
                403, "handoff belongs to another surface")
        try:
            return complete_handoff(handoff_id)
        except KeyError as exc:
            raise HTTPException(404, "handoff not found") from exc

    # ── Verified Delegations ─────────────────────────────────────────

    def _delegation_error(exc: Exception) -> HTTPException:
        from core.delegations import (
            DelegationConflict,
            DelegationNotFound,
        )
        if isinstance(exc, DelegationNotFound):
            return HTTPException(404, str(exc))
        if isinstance(exc, DelegationConflict):
            return HTTPException(409, str(exc))
        return HTTPException(400, str(exc))

    @app.post("/api/delegations", dependencies=_WRITE)
    async def api_delegation_create(request: Request):
        from core.delegations import DelegationStore
        data = await request.json()
        owner = str(os.environ.get("USER_ID") or data.get("principal_id") or "")
        data["principal_id"] = owner
        # This producer endpoint may capture work, but it is not an owner
        # decision surface. R3 authorization is granted only by the protected
        # confirm endpoint or an in-process trusted rule.
        data["authorized"] = False
        try:
            row, created = await run_in_threadpool(
                DelegationStore().create, **data
            )
        except Exception as exc:
            raise _delegation_error(exc) from exc
        return {"created": created, "delegation": row}

    @app.get("/api/delegations")
    async def api_delegation_list(status: str = "", matter_id: str = "",
                                  needs_attention: bool = False,
                                  limit: int = 100):
        from core.delegations import DelegationStore
        try:
            items = await run_in_threadpool(
                DelegationStore().list,
                status=status,
                matter_id=matter_id,
                needs_attention=needs_attention,
                limit=limit,
            )
        except Exception as exc:
            raise _delegation_error(exc) from exc
        return {"items": items}

    @app.get("/api/delegations/metrics")
    async def api_delegation_metrics():
        from core.delegations import DelegationStore
        return await run_in_threadpool(DelegationStore().metrics)

    @app.get("/api/delegations/{delegation_id}")
    async def api_delegation_get(delegation_id: str):
        from core.delegations import DelegationStore
        try:
            return await run_in_threadpool(
                DelegationStore().get, delegation_id
            )
        except Exception as exc:
            raise _delegation_error(exc) from exc

    @app.get("/api/delegations/{delegation_id}/evidence")
    async def api_delegation_evidence(delegation_id: str):
        from core.delegations import DelegationStore
        try:
            detail = await run_in_threadpool(
                DelegationStore().get, delegation_id
            )
        except Exception as exc:
            raise _delegation_error(exc) from exc
        return {"items": detail["evidence"]}

    @app.post("/api/delegations/{delegation_id}/handoff", dependencies=_WRITE)
    async def api_delegation_handoff(delegation_id: str, request: Request):
        """Park a delegation in the desktop 接力区.

        REQ-120: "mobile" is no longer a creatable target — the old default
        (to_surface="mobile") would push nothing and land nowhere. Explicit
        mobile requests get the core validator's 400.
        """
        from core.continuity import create_handoff
        from core.delegations import DelegationStore
        data = await request.json()
        try:
            detail = await run_in_threadpool(
                DelegationStore().get, delegation_id
            )
            result = await run_in_threadpool(
                create_handoff,
                "delegation",
                delegation_id,
                from_surface=str(data.get("from_surface") or "mobile"),
                to_surface=str(data.get("to_surface") or "desktop"),
                title=detail["title"],
                matter_id=str(detail.get("matter_id") or ""),
                created_by=str(os.environ.get("USER_ID") or "owner"),
            )
        except Exception as exc:
            raise _delegation_error(exc) from exc
        return result

    # ── L3 iteration proposals ───────────────────────────────────────

    @app.get("/api/iteration/proposals")
    async def api_iteration_proposals(status: str = "", limit: int = 100):
        from core.iteration_loop import IterationStore
        try:
            items = await run_in_threadpool(
                IterationStore().list, status=status, limit=limit
            )
        except Exception as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"items": items}

    @app.get("/api/iteration/proposals/{proposal_id}")
    async def api_iteration_proposal_get(proposal_id: str):
        from core.iteration_loop import IterationStore
        try:
            return await run_in_threadpool(
                IterationStore().get, proposal_id
            )
        except Exception as exc:
            raise HTTPException(404, str(exc)) from exc

    # ── Health ───────────────────────────────────────────────────────

    @app.get("/api/provider-health")
    async def api_provider_health():
        from core.provider_health import snapshot
        return await run_in_threadpool(snapshot)

    @app.get("/api/health")
    async def api_health():
        """Health check — actually touches the DB instead of hardcoding."""
        try:
            get_db().execute("SELECT 1").fetchone()
            db_status = "connected"
        except Exception as e:
            db_status = f"error: {e}"
        return {
            "status": "ok" if db_status == "connected" else "degraded",
            "timestamp": now_local_str("%Y-%m-%dT%H:%M:%S"),
            "db": db_status,
        }
