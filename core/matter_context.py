"""Bounded, provider-neutral context bundles for durable Matters."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core.matters import get_matter

DEFAULT_EVENT_LIMIT = 12
DEFAULT_CHAR_LIMIT = 12000

_SAFE_METADATA = {
    "session": {"model", "started_at", "updated_at", "workspace", "conv_key"},
    "artifact": {"workspace", "exists", "source", "job_id", "status"},
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
        payload = event.get("payload") or {}
        if ("decision" in kind or "closure" in kind
                or kind in {"memorial_decided", "matter_closed_with_followups"}):
            decisions.append({
                "at": event.get("created_at", ""),
                "summary": _clip(event.get("summary", ""), 600),
                "details": payload,
            })
    return decisions[-8:]


def build_context_bundle(matter_id: str, event_limit: int = DEFAULT_EVENT_LIMIT,
                         char_limit: int = DEFAULT_CHAR_LIMIT) -> dict:
    """Build a safe handoff bundle without copying transcripts or memory bodies."""
    matter = get_matter(matter_id)
    if matter is None:
        raise KeyError(f"matter not found: {matter_id}")
    links = matter.get("links", [])
    sessions, artifacts, memory_pointers, related = [], [], [], []
    for link in links:
        entity_type = link.get("entity_type", "")
        item = {
            "provider": link.get("provider", ""),
            "id": _clip(link.get("entity_id", ""), 500),
            "title": _clip(link.get("title", ""), 300),
            "metadata": _safe_metadata(entity_type, link.get("metadata")),
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

    events = list(reversed(matter.get("events", [])[:max(1, event_limit)]))
    bundle = {
        "schema": "jarvis.matter-context.v1",
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
        } for event in events],
        "sessions": sessions[-8:],
        "artifacts": artifacts[-20:],
        "memory_pointers": memory_pointers[-8:],
        "related": related[-20:],
        "privacy": (
            "This bundle contains summaries and pointers only. Do not infer or load "
            "unrelated private memory or raw transcripts."
        ),
    }
    # Preserve the current state while enforcing the requested hard limit.
    # Lower-value history is removed before durable pointers and decisions.
    trim_order = ("recent_timeline", "related", "artifacts", "sessions",
                  "confirmed_decisions", "memory_pointers")
    while len(render_context_markdown(bundle)) > max(1000, int(char_limit)):
        changed = False
        for key in trim_order:
            if bundle[key]:
                bundle[key].pop(0)
                changed = True
                break
        if not changed:
            break
    return bundle


def render_context_markdown(bundle: dict) -> str:
    matter = bundle["matter"]
    lines = [
        f"# Matter: {matter['title']}",
        "",
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
    if matter.get("outcome"):
        lines.extend(["", "## Outcome", matter["outcome"]])
    if bundle.get("confirmed_decisions"):
        lines.extend(["", "## Confirmed decisions"])
        for item in bundle["confirmed_decisions"]:
            lines.append(f"- {item.get('at', '')}: {item.get('summary', '')}")
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


def write_context_bundle(matter_id: str, output: str | Path | None = None) -> Path:
    bundle = build_context_bundle(matter_id)
    if output is None:
        from core.config import Config
        output = Config().jarvis_dir / "data" / "matter_context" / f"{matter_id}.md"
    path = Path(output).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_context_markdown(bundle), encoding="utf-8")
    json_path = path.with_suffix(".json")
    json_path.write_text(json.dumps(bundle, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
