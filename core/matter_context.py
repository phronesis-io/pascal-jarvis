"""Bounded, provider-neutral context bundles for durable Matters."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from pathlib import Path
from typing import Any

from core.matters import get_matter

DEFAULT_EVENT_LIMIT = 12
DEFAULT_CHAR_LIMIT = 12000

_SAFE_METADATA = {
    "session": {
        "model", "started_at", "updated_at", "workspace", "conv_key", "status",
    },
    "artifact": {
        "workspace", "exists", "source", "job_id", "status", "sha256", "size",
    },
    "intent": {"status", "closure_status"},
    "memorial": {"source", "status", "decision"},
    "job": {"status", "output_file"},
}


def _clip(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    return text if len(text) <= limit else text[:limit].rstrip() + "..."


def _safe_metadata(entity_type: str, value: Any) -> dict:
    """Keep executor-useful pointers without forwarding arbitrary source data."""
    if not isinstance(value, dict):
        return {}
    allowed = set(_SAFE_METADATA.get(entity_type, set()))
    if value.get("pointer_type") == "memory" or value.get("memory_pointer"):
        allowed.update({"pointer_type", "memory_pointer"})
    return {key: _clip(value[key], 500) for key in allowed if value.get(key) is not None}


def _decision_events(events: list[dict]) -> list[dict]:
    decisions = []
    for event in reversed(events):
        kind = str(event.get("event_type", "")).lower()
        if ("decision" in kind or "closure" in kind
                or kind in {"memorial_decided", "matter_closed_with_followups"}):
            decisions.append({
                "at": event.get("created_at", ""),
                "summary": _clip(event.get("summary", ""), 600),
                "source_ref": f"matter_event:{event.get('id', '')}",
            })
    return decisions[-8:]


def build_context_bundle(matter_id: str, event_limit: int = DEFAULT_EVENT_LIMIT,
                         char_limit: int = DEFAULT_CHAR_LIMIT,
                         run: dict | None = None) -> dict:
    """Build a safe handoff bundle without copying transcripts or memory bodies."""
    matter = get_matter(matter_id)
    if matter is None:
        raise KeyError(f"matter not found: {matter_id}")
    if run and str(run.get("matter_id") or "") != str(matter_id):
        raise ValueError("run belongs to another Matter")
    links = matter.get("links", [])
    sessions, artifacts, memory_pointers, related = [], [], [], []
    for link in links:
        entity_type = link.get("entity_type", "")
        item = {
            "provider": link.get("provider", ""),
            "id": _clip(link.get("entity_id", ""), 500),
            "title": _clip(link.get("title", ""), 300),
            "metadata": _safe_metadata(entity_type, link.get("metadata")),
            "source_ref": f"matter_link:{link.get('id', '')}",
        }
        if entity_type == "session":
            sessions.append(item)
        elif entity_type == "artifact":
            artifacts.append(item)
        elif (item["metadata"].get("pointer_type") == "memory"
              or item["metadata"].get("memory_pointer")):
            memory_pointers.append(item)
        elif entity_type != "conversation":
            related.append({"type": entity_type, **item})

    # Conversation turns are durable audit events, but reset means "do not put
    # the old short-term dialog back into the prompt".  Keep all decisions and
    # domain events while showing conversation events only from the currently
    # active reset generation.  Legacy events are generation 0.
    from core.conversation_context import current_context_generation
    generation = current_context_generation(f"matter:{matter_id}")
    if run and int(run.get("context_generation", -1)) != generation:
        raise ValueError("run was acquired under a stale context generation")
    visible_events = []
    for event in matter.get("events", []):
        # A handoff preview is operational bookkeeping, not durable task
        # context. Including it would make an otherwise identical preview
        # change its own packet digest every time the owner tapped handoff.
        if str(event.get("event_type") or "") == "handoff_prepared":
            continue
        if str(event.get("event_type") or "").startswith("conversation_"):
            event_generation = int((event.get("payload") or {}).get(
                "context_generation", 0) or 0)
            if event_generation != generation:
                continue
        visible_events.append(event)
    events = list(reversed(visible_events[:max(1, event_limit)]))
    authority = dict((run or {}).get("authority") or {
        "may_complete_matter": False,
        "may_self_attest_external_effects": False,
    })
    from core.memory_compiler import context_records
    compiled_memory = context_records(matter_id)
    bundle = {
        "schema": "jarvis.context-packet.v2",
        "context_generation": generation,
        "run": {
            "id": str((run or {}).get("id") or ""),
            "executor": str((run or {}).get("executor") or ""),
            "run_sequence": int((run or {}).get("run_sequence") or 0),
            "task": _clip((run or {}).get("task", ""), 4000),
            "workspace": str((run or {}).get("workspace") or ""),
            "lease_expires_epoch": (run or {}).get("lease_expires_epoch"),
        },
        "authority": authority,
        "receipt_contract": {
            "schema": "jarvis.result-receipt.v1",
            "artifact_verification": "workspace_file_hash_required",
            "external_effects": "authoritative_evidence_reference_required",
            "model_narrative": "unverified",
            "matter_completion": "separate_verified_transition_required",
        },
        "matter": {
            "id": matter["id"],
            "title": matter["title"],
            "kind": matter.get("kind", "project"),
            "status": matter.get("status", "active"),
            "priority": matter.get("priority", 5),
            "summary": _clip(matter.get("summary", ""), 2400),
            "next_action": _clip(matter.get("next_action", ""), 1600),
            "outcome": _clip(matter.get("outcome", ""), 2400),
            "updated_at": matter.get("updated_at", ""),
        },
        "confirmed_decisions": _decision_events(matter.get("events", [])),
        "recent_timeline": [{
            "at": event.get("created_at", ""),
            "type": event.get("event_type", ""),
            "actor": event.get("actor", ""),
            "summary": _clip(event.get("summary", ""), 800),
            "source_ref": f"matter_event:{event.get('id', '')}",
        } for event in events],
        "sessions": sessions[-8:],
        "artifacts": artifacts[-20:],
        "memory_pointers": memory_pointers[-8:],
        "compiled_memory": compiled_memory["claims"],
        "memory_conflicts": compiled_memory["conflicts"],
        "related": related[-20:],
        "privacy": (
            "This bundle contains summaries and pointers only. Do not infer or load "
            "unrelated private memory or raw transcripts."
        ),
    }
    # Preserve the current state while enforcing the requested hard limit.
    # Lower-value history is removed before durable pointers and decisions.
    hard_limit = max(1000, int(char_limit))
    bundle["packet_id"] = "ctx_" + ("0" * 20)
    trim_order = (
        "recent_timeline", "related", "artifacts", "sessions",
        "compiled_memory", "memory_conflicts", "confirmed_decisions",
        "memory_pointers",
    )
    while len(render_context_markdown(bundle)) > hard_limit:
        changed = False
        for key in trim_order:
            if bundle[key]:
                bundle[key].pop(0)
                changed = True
                break
        if not changed:
            break
    for key in ("summary", "outcome", "next_action"):
        while len(render_context_markdown(bundle)) > hard_limit:
            current = str(bundle["matter"].get(key) or "")
            if not current:
                break
            overflow = len(render_context_markdown(bundle)) - hard_limit
            keep = max(0, len(current) - max(overflow, 40))
            bundle["matter"][key] = _clip(current, keep) if keep else ""
    bundle.pop("packet_id", None)
    identity = hashlib.sha256(
        json.dumps(
            bundle, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()
    bundle["packet_id"] = f"ctx_{identity[:20]}"
    bundle["digest"] = f"sha256:{identity}"
    return bundle


def render_context_markdown(bundle: dict) -> str:
    matter = bundle["matter"]
    lines = [
        f"# Matter: {matter['title']}",
        "",
        f"- Context Packet: `{bundle.get('packet_id', '')}`",
        f"- Context generation: `{bundle.get('context_generation', 0)}`",
        f"- Matter ID: `{matter['id']}`",
        f"- Status: `{matter['status']}`",
        f"- Priority: `{matter['priority']}`",
        f"- Updated: `{matter.get('updated_at', '')}`",
        "",
        "## Current consensus",
        matter.get("summary") or "No consensus recorded yet.",
        "",
        "## Next action",
        matter.get("next_action") or "No next action recorded yet.",
    ]
    run = bundle.get("run") or {}
    if run.get("id"):
        lines.extend([
            "",
            "## Execution boundary",
            f"- Run ID: `{run.get('id', '')}`",
            f"- Executor: `{run.get('executor', '')}`",
            f"- Run sequence: `{run.get('run_sequence', 0)}`",
            "- Releasing this run does not complete the Matter.",
            "- Model prose is not evidence of artifacts or external effects.",
        ])
    if matter.get("outcome"):
        lines.extend(["", "## Outcome", matter["outcome"]])
    if bundle.get("confirmed_decisions"):
        lines.extend(["", "## Confirmed decisions"])
        for item in bundle["confirmed_decisions"]:
            lines.append(f"- {item.get('at', '')}: {item.get('summary', '')}")
    if bundle.get("compiled_memory"):
        lines.extend(["", "## Compiled memory"])
        for item in bundle["compiled_memory"]:
            refs = ", ".join(item.get("source_refs", []))
            lines.append(
                f"- [{item.get('kind', '')}] {item.get('content', '')} "
                f"(claim `{item.get('id', '')}`; source `{refs}`)"
            )
    if bundle.get("memory_conflicts"):
        lines.extend(["", "## Unresolved memory conflicts"])
        for item in bundle["memory_conflicts"]:
            lines.append(
                f"- `{item.get('claim_key', '')}` needs review "
                f"(conflict `{item.get('id', '')}`)"
            )
    if bundle.get("recent_timeline"):
        lines.extend(["", "## Recent timeline"])
        for item in bundle["recent_timeline"]:
            lines.append(
                f"- {item.get('at', '')} [{item.get('actor', '')}] "
                f"{item.get('summary', '')}"
            )
    for heading, key in (("Sessions", "sessions"), ("Artifacts", "artifacts"),
                         ("Memory pointers", "memory_pointers"),
                         ("Related records", "related")):
        if bundle.get(key):
            lines.extend(["", f"## {heading}"])
            for item in bundle[key]:
                kind = f"{item.get('provider', '')}:{item.get('id', '')}"
                lines.append(f"- `{kind}` {item.get('title', '')}".rstrip())
    lines.extend(["", "## Privacy boundary", bundle["privacy"], ""])
    return "\n".join(lines)


def _write_private(path: Path, content: str) -> None:
    """Atomically write one handoff file with owner-only permissions."""
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        descriptor = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
        os.chmod(path, 0o600)
    finally:
        temporary.unlink(missing_ok=True)


def write_context_bundle(matter_id: str, output: str | Path | None = None,
                         run: dict | None = None) -> Path:
    bundle = build_context_bundle(matter_id, run=run)
    if output is None:
        # Follow the active runtime DB override. This keeps tests and alternate
        # installations from writing private packets into the repository root.
        from core.db import _db_path
        root = _db_path().parent / "matter_context"
        if run and run.get("id"):
            output = root / matter_id / f"{run['id']}.md"
        else:
            output = root / f"{matter_id}.md"
    path = Path(output).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    _write_private(path, render_context_markdown(bundle))
    json_path = path.with_suffix(".json")
    _write_private(
        json_path, json.dumps(bundle, ensure_ascii=False, indent=2) + "\n"
    )
    return path
