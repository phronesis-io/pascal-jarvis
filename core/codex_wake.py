"""Owner-triggered Lark-to-Codex task preparation without model execution."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

from core.codex_app_server import CodexAppServerClient, CodexAppServerError
from core.codex_frontstage import continuation_prompt
from core.matters import add_event, get_matter, link_entity


CONNECTOR_VERSION = "codex-wake.v1"
_SPACE_RE = re.compile(r"\s+")


class CodexWakeError(RuntimeError):
    """The requested Codex task could not be prepared and verified."""

    def __init__(
        self, message: str, *, wake_id: str = "", orphan_thread_id: str = "",
    ) -> None:
        super().__init__(message)
        self.wake_id = str(wake_id or "")
        self.orphan_thread_id = str(orphan_thread_id or "")


@contextmanager
def _wake_lock(matter_id: str) -> Iterator[None]:
    root = Path(tempfile.gettempdir()) / "jarvis-codex-wake-locks"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        root.chmod(0o700)
    except OSError:
        pass
    lock_path = root / f"{str(matter_id).replace('/', '_')}.lock"
    with lock_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _task_name(title: str) -> str:
    compact = _SPACE_RE.sub(" ", str(title or "").strip())
    compact = "".join(char for char in compact if char.isprintable())
    if len(compact) > 42:
        compact = compact[:41].rstrip() + "…"
    return f"继续：{compact or 'Jarvis 事项'}"


def _workspace_for(matter: dict[str, Any], workspace: str | Path | None) -> Path:
    candidates: list[str | Path] = []
    if workspace:
        candidates.append(workspace)
    for link in matter.get("links", []):
        metadata = link.get("metadata") or {}
        if link.get("entity_type") == "session" and metadata.get("workspace"):
            candidates.append(str(metadata["workspace"]))
    if os.environ.get("JARVIS_DIR"):
        candidates.append(str(os.environ["JARVIS_DIR"]))
    candidates.append(os.getcwd())
    for candidate in candidates:
        try:
            path = Path(candidate).expanduser().resolve()
        except (OSError, RuntimeError):
            continue
        if path.is_dir():
            return path
    raise CodexWakeError("No readable workspace is available for this Matter")


def _prepared_links(matter: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        link for link in matter.get("links", [])
        if link.get("entity_type") == "session"
        and link.get("provider") == "codex"
        and (link.get("metadata") or {}).get("source") == "codex_wake"
        and (link.get("metadata") or {}).get("wake_status") == "prepared"
    ]


def _read_unused_thread(
    client: Any, thread_id: str, *, expected_workspace: Path | None = None,
    expected_name: str = "",
) -> dict[str, Any] | None:
    result = client.request(
        "thread/read", {"threadId": thread_id, "includeTurns": True}
    )
    thread = (result or {}).get("thread") if isinstance(result, dict) else None
    if not isinstance(thread, dict):
        raise CodexWakeError("Codex returned an invalid task read-back")
    if thread.get("id") != thread_id:
        raise CodexWakeError("Codex returned a different task identifier")
    if thread.get("turns") or []:
        return None
    if expected_name and str(thread.get("name") or "") != expected_name:
        raise CodexWakeError("Codex task name was not verified")
    if expected_workspace is not None:
        try:
            actual_workspace = Path(str(thread.get("cwd") or "")).resolve()
        except (OSError, RuntimeError):
            return None
        if actual_workspace != expected_workspace.resolve():
            return None
    return thread


def _delete_unlinked_thread(client: Any, thread_id: str, matter_id: str) -> bool:
    try:
        client.request("thread/delete", {"threadId": thread_id})
        return True
    except Exception as exc:
        from core.log import log
        log(
            "codex-wake",
            "orphan_cleanup_failed",
            level="error",
            matter_id=matter_id,
            thread_id=thread_id,
            error_type=type(exc).__name__,
        )
        return False


def _developer_instructions(matter_id: str, wake_id: str) -> str:
    return (
        "This task was prepared by an explicit owner action through Jarvis "
        f"wake receipt {wake_id} for Matter {matter_id}. No model turn or Matter "
        "lease has started. Wait for Pascal's message. When he asks to continue "
        "this outcome, call jarvis_matter_continue with this exact matter_id, the "
        "current workspace, a concise task, and the actual desktop/mobile surface. "
        "Omit task_ref because Jarvis already linked this Codex task in the wake "
        "receipt. Follow the returned Context Packet and release or abort exactly "
        "once. Do not infer Matter completion."
    )


def _project_id(client: Any, workspace: Path) -> str:
    """Return the narrowest saved Codex project containing the workspace."""
    try:
        result = client.request("project/list", {"limit": 100})
    except CodexAppServerError as exc:
        from core.log import log
        log(
            "codex-wake",
            "project_discovery_failed",
            level="warn",
            error_type=type(exc).__name__,
        )
        return ""
    matches: list[tuple[int, str]] = []
    for project in (result or {}).get("data", []) if isinstance(result, dict) else []:
        for root in project.get("roots", []):
            try:
                root_path = Path(root.get("path", "")).expanduser().resolve()
                workspace.relative_to(root_path)
            except (KeyError, OSError, RuntimeError, ValueError):
                continue
            matches.append((len(root_path.parts), str(project.get("id") or "")))
    return max(matches, default=(0, ""))[1]


def _verified_result(
    *, matter: dict[str, Any], thread: dict[str, Any], workspace: Path,
    wake_id: str, user_agent: str, created: bool,
) -> dict[str, Any]:
    return {
        "schema": "jarvis.codex-wake.v1",
        "status": "prepared" if created else "reused",
        "created": bool(created),
        "executed": False,
        "matter_id": matter["id"],
        "thread_id": str(thread["id"]),
        "task_name": str(thread.get("name") or _task_name(matter["title"])),
        "workspace": str(workspace),
        "continuation_prompt": continuation_prompt(matter),
        "wake_receipt": {
            "id": wake_id,
            "connector_version": CONNECTOR_VERSION,
            "protocol": "codex-app-server",
            "user_agent": user_agent,
            "thread_verified": True,
            "turn_count": 0,
            "matter_lease_started": False,
            "mobile_visibility": "pending_owner_acceptance",
        },
    }


def _record_event_best_effort(
    matter_id: str, event_type: str, summary: str, *, actor: str,
    payload: dict[str, Any],
) -> None:
    """Keep a verified task usable when the secondary audit event fails.

    The Matter link is already a transactional durable receipt. This event is
    an additional timeline rendering, so failure is observable but must not
    turn a proven task into a false "not created" response.
    """
    try:
        add_event(
            matter_id, event_type, summary, actor=actor, payload=payload,
        )
    except Exception as exc:
        from core.log import log
        log(
            "codex-wake",
            "timeline_event_failed",
            level="warn",
            matter_id=matter_id,
            event_type=event_type,
            error_type=type(exc).__name__,
        )


def create_codex_wake_task(
    matter_id: str,
    *,
    workspace: str | Path | None = None,
    source: str = "lark",
    source_ref: str = "",
    actor: str = "owner",
    client_factory: Callable[..., Any] = CodexAppServerClient,
    now_epoch: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """Prepare one visible, empty Codex task and record a durable Matter link.

    Repeated owner actions reuse a still-empty task. The first actual Codex
    message performs Matter continuation and acquires the execution lease.
    """
    matter = get_matter(matter_id)
    if matter is None:
        raise KeyError(f"matter not found: {matter_id}")
    if matter.get("status") in {"done", "archived"}:
        raise CodexWakeError("Closed Matters cannot create a continuation task")
    resolved_workspace = _workspace_for(matter, workspace)

    wake_id = ""
    with _wake_lock(matter_id):
        matter = get_matter(matter_id) or matter
        if matter.get("status") in {"done", "archived"}:
            raise CodexWakeError(
                "Matter closed before the Codex task could be prepared"
            )
        try:
            with client_factory(
                timeout=3,
                client_name="jarvis-codex-wake",
                client_version=CONNECTOR_VERSION,
                experimental_api=True,
            ) as client:
                for link in _prepared_links(matter):
                    thread = _read_unused_thread(
                        client,
                        str(link["entity_id"]),
                        expected_workspace=resolved_workspace,
                        expected_name=str(link.get("title") or ""),
                    )
                    if thread is None:
                        continue
                    metadata = link.get("metadata") or {}
                    result = _verified_result(
                        matter=matter,
                        thread=thread,
                        workspace=Path(thread.get("cwd") or resolved_workspace),
                        wake_id=str(metadata.get("wake_id") or ""),
                        user_agent=str(metadata.get("user_agent") or client.user_agent),
                        created=False,
                    )
                    _record_event_best_effort(
                        matter_id,
                        "codex_wake_reused",
                        "复用尚未开始的 Codex 任务",
                        actor=actor,
                        payload={
                            "wake_id": result["wake_receipt"]["id"],
                            "thread_id": result["thread_id"],
                            "source": source,
                            "source_ref": str(source_ref or ""),
                        },
                    )
                    return result

                wake_id = f"wake_{uuid.uuid4().hex[:12]}"
                add_event(
                    matter_id,
                    "codex_wake_requested",
                    "主人请求在 Codex 继续事项",
                    actor=actor,
                    payload={
                        "wake_id": wake_id,
                        "connector_version": CONNECTOR_VERSION,
                        "source": str(source or ""),
                        "source_ref": str(source_ref or ""),
                        "workspace": str(resolved_workspace),
                        "requested_epoch": float(now_epoch()),
                        "executed": False,
                        "matter_lease_started": False,
                    },
                )
                params: dict[str, Any] = {
                    "cwd": str(resolved_workspace),
                    "runtimeWorkspaceRoots": [str(resolved_workspace)],
                    "ephemeral": False,
                    "developerInstructions": _developer_instructions(
                        matter_id, wake_id
                    ),
                }
                project_id = _project_id(client, resolved_workspace)
                if project_id:
                    params["projectId"] = project_id
                started = client.request("thread/start", params)
                thread = (started or {}).get("thread") if isinstance(started, dict) else None
                if not isinstance(thread, dict) or not thread.get("id"):
                    raise CodexWakeError(
                        "Codex did not return a task identifier", wake_id=wake_id,
                    )
                thread_id = str(thread["id"])
                linked = False
                try:
                    add_event(
                        matter_id,
                        "codex_wake_task_created",
                        "Codex 已返回空任务，等待命名、核验和链接",
                        actor=actor,
                        payload={
                            "wake_id": wake_id,
                            "thread_id": thread_id,
                            "connector_version": CONNECTOR_VERSION,
                            "workspace": str(resolved_workspace),
                            "user_agent": client.user_agent,
                            "created_epoch": float(now_epoch()),
                            "executed": False,
                            "matter_lease_started": False,
                        },
                    )
                    client.request(
                        "thread/name/set",
                        {"threadId": thread_id, "name": _task_name(matter["title"])},
                    )
                    verified = _read_unused_thread(
                        client,
                        thread_id,
                        expected_workspace=resolved_workspace,
                        expected_name=_task_name(matter["title"]),
                    )
                    if verified is None:
                        raise CodexWakeError(
                            "Codex task verification did not prove an empty task"
                        )
                    link_entity(
                        matter_id,
                        "session",
                        thread_id,
                        provider="codex",
                        title=str(verified.get("name") or _task_name(matter["title"])),
                        metadata={
                            "source": "codex_wake",
                            "wake_status": "prepared",
                            "wake_id": wake_id,
                            "connector_version": CONNECTOR_VERSION,
                            "workspace": str(resolved_workspace),
                            "created_epoch": float(now_epoch()),
                            "user_agent": client.user_agent,
                            "source_surface": str(source or ""),
                            "source_ref": str(source_ref or ""),
                            "turn_count": 0,
                            "matter_lease_started": False,
                        },
                        actor=actor,
                    )
                    linked = True
                    result = _verified_result(
                        matter=matter,
                        thread=verified,
                        workspace=resolved_workspace,
                        wake_id=wake_id,
                        user_agent=client.user_agent,
                        created=True,
                    )
                    _record_event_best_effort(
                        matter_id,
                        "codex_wake_prepared",
                        "已准备空的 Codex 任务，尚未开始执行",
                        actor=actor,
                        payload={
                            "wake_id": wake_id,
                            "thread_id": thread_id,
                            "connector_version": CONNECTOR_VERSION,
                            "source": str(source or ""),
                            "source_ref": str(source_ref or ""),
                            "workspace": str(resolved_workspace),
                            "executed": False,
                            "matter_lease_started": False,
                        },
                    )
                    return result
                except Exception:
                    if not linked:
                        cleaned = _delete_unlinked_thread(
                            client, thread_id, matter_id
                        )
                        if not cleaned:
                            raise CodexWakeError(
                                "Codex task cleanup could not be verified",
                                wake_id=wake_id,
                                orphan_thread_id=thread_id,
                            )
                    raise
        except CodexWakeError as exc:
            if wake_id and not exc.wake_id:
                exc.wake_id = wake_id
            raise
        except CodexAppServerError as exc:
            raise CodexWakeError(
                "Codex task preparation is unavailable", wake_id=wake_id,
            ) from exc
        except Exception as exc:
            raise CodexWakeError(
                "Codex wake receipt could not be committed", wake_id=wake_id,
            ) from exc


def prepare_codex_wake(
    matter_id: str, **kwargs: Any,
) -> dict[str, Any]:
    """Fail closed to a stable phrase; never claim a task was created."""
    matter = get_matter(matter_id)
    if matter is None:
        raise KeyError(f"matter not found: {matter_id}")
    try:
        return create_codex_wake_task(matter_id, **kwargs)
    except Exception as exc:
        orphan_thread_id = str(
            getattr(exc, "orphan_thread_id", "") or ""
        )
        wake_id = str(getattr(exc, "wake_id", "") or "")
        _record_event_best_effort(
            matter_id,
            "codex_wake_failed",
            "Codex 任务未创建，保留手动续接语句",
            actor=str(kwargs.get("actor") or "owner"),
            payload={
                "error_type": type(exc).__name__,
                "wake_id": wake_id,
                "source": str(kwargs.get("source") or "lark"),
                "source_ref": str(kwargs.get("source_ref") or ""),
                "failed_epoch": float(time.time()),
                "executed": False,
                "matter_lease_started": False,
                "orphan_cleanup_required": bool(orphan_thread_id),
                "orphan_thread_id": orphan_thread_id,
            },
        )
        return {
            "schema": "jarvis.codex-wake.v1",
            "status": "manual_fallback",
            "created": False,
            "executed": False,
            "matter_id": matter_id,
            "thread_id": "",
            "task_name": "",
            "workspace": "",
            "continuation_prompt": continuation_prompt(matter),
            "wake_receipt": {
                "connector_version": CONNECTOR_VERSION,
                "id": wake_id,
                "thread_verified": False,
                "turn_count": 0,
                "matter_lease_started": False,
                "mobile_visibility": "not_created",
                "error_type": type(exc).__name__,
                "orphan_cleanup_required": bool(orphan_thread_id),
            },
        }


def audit_codex_wakes(
    *, now_epoch: float | None = None, stale_seconds: int = 300,
) -> dict[str, Any]:
    """Find wake sagas that lack a committed link or terminal failure."""
    from core.db import get_db

    now_value = float(time.time() if now_epoch is None else now_epoch)
    threshold = max(30, int(stale_seconds))
    db = get_db()
    event_rows = db.execute(
        "SELECT matter_id,event_type,payload,created_at FROM matter_events "
        "WHERE event_type IN "
        "('codex_wake_requested','codex_wake_task_created','codex_wake_failed') "
        "ORDER BY id"
    ).fetchall()
    requests: dict[str, dict[str, Any]] = {}
    created: dict[str, dict[str, Any]] = {}
    failed: dict[str, dict[str, Any]] = {}
    cleanup_required: list[dict[str, Any]] = []
    for row in event_rows:
        try:
            payload = json.loads(row["payload"] or "{}")
        except (json.JSONDecodeError, TypeError, ValueError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        wake_id = str(payload.get("wake_id") or "")
        if not wake_id:
            continue
        item = {
            "wake_id": wake_id,
            "matter_id": str(row["matter_id"]),
            "thread_id": str(
                payload.get("thread_id")
                or payload.get("orphan_thread_id")
                or ""
            ),
            "epoch": float(
                payload.get("created_epoch")
                or payload.get("requested_epoch")
                or payload.get("failed_epoch")
                or 0
            ),
        }
        if row["event_type"] == "codex_wake_requested":
            requests[wake_id] = item
        elif row["event_type"] == "codex_wake_task_created":
            created[wake_id] = item
        else:
            failed[wake_id] = item
            if payload.get("orphan_cleanup_required"):
                cleanup_required.append(item)

    linked: set[str] = set()
    link_rows = db.execute(
        "SELECT metadata FROM matter_links WHERE entity_type='session' "
        "AND provider='codex'"
    ).fetchall()
    for row in link_rows:
        try:
            metadata = json.loads(row["metadata"] or "{}")
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        if isinstance(metadata, dict) and metadata.get("source") == "codex_wake":
            wake_id = str(metadata.get("wake_id") or "")
            if wake_id:
                linked.add(wake_id)

    issues: list[dict[str, Any]] = []
    for wake_id, requested in requests.items():
        if wake_id in linked or wake_id in failed:
            continue
        age = max(0.0, now_value - float(requested.get("epoch") or 0))
        if age < threshold:
            continue
        external = created.get(wake_id)
        issues.append({
            "code": (
                "external_task_unlinked" if external
                else "wake_request_unfinished"
            ),
            "wake_id": wake_id,
            "matter_id": requested["matter_id"],
            "thread_id": str((external or {}).get("thread_id") or ""),
            "age_seconds": int(age),
        })
    for item in cleanup_required:
        if item["wake_id"] in linked:
            continue
        issues.append({
            "code": "orphan_cleanup_required",
            "wake_id": item["wake_id"],
            "matter_id": item["matter_id"],
            "thread_id": item["thread_id"],
            "age_seconds": max(0, int(now_value - item["epoch"])),
        })
    return {
        "schema": "jarvis.codex-wake-audit.v1",
        "healthy": not issues,
        "stale_seconds": threshold,
        "requested": len(requests),
        "linked": len(linked),
        "failed": len(failed),
        "issues": issues,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m core.codex_wake")
    parser.add_argument("matter_id", nargs="?")
    parser.add_argument("--workspace", default="")
    parser.add_argument("--source", default="operator")
    parser.add_argument("--source-ref", default="")
    parser.add_argument("--audit", action="store_true")
    parser.add_argument("--stale-seconds", type=int, default=300)
    args = parser.parse_args(argv)
    if args.audit:
        result = audit_codex_wakes(stale_seconds=args.stale_seconds)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["healthy"] else 3
    if not args.matter_id:
        parser.error("matter_id is required unless --audit is used")
    result = prepare_codex_wake(
        args.matter_id,
        workspace=args.workspace or None,
        source=args.source,
        source_ref=args.source_ref,
        actor="operator",
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] in {"prepared", "reused"} else 3


if __name__ == "__main__":
    raise SystemExit(main())
