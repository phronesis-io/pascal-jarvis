"""Search past Claude Code sessions — find conversations by keyword."""

import json
import re
from pathlib import Path


def _extract_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for c in content:
            if isinstance(c, dict):
                if c.get("type") == "text":
                    parts.append(c.get("text", ""))
                elif c.get("type") == "tool_use":
                    parts.append(f"[tool: {c.get('name', '')}]")
            elif isinstance(c, str):
                parts.append(c)
        return "\n".join(parts)
    return ""


def load_sessions(session_dir: str | Path) -> list[dict]:
    """Load all sessions with parsed messages."""
    session_dir = Path(session_dir)
    sessions = []
    for f in sorted(session_dir.glob("*.jsonl"),
                    key=lambda p: p.stat().st_mtime, reverse=True):
        messages = []
        first_ts = ""
        for line in f.read_text(encoding="utf-8", errors="ignore").splitlines():
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("type") not in ("user", "assistant"):
                continue
            msg = obj.get("message", {})
            role = msg.get("role", obj.get("type", ""))
            text = _extract_text(msg.get("content", ""))
            if not text.strip():
                continue
            ts = obj.get("timestamp", "")
            if ts and not first_ts:
                first_ts = ts
            messages.append({"role": role, "text": text, "timestamp": ts})
        if messages:
            sessions.append({
                "id": f.stem,
                "date": first_ts[:16].replace("T", " ") if first_ts else "",
                "messages": messages,
            })
    return sessions


def search(session_dir: str | Path, query: str,
           max_results: int = 20, role_filter: str | None = None) -> list[dict]:
    """Search all sessions for a query string with surrounding context."""
    sessions = load_sessions(session_dir)
    results = []
    pattern = re.compile(re.escape(query), re.IGNORECASE)

    for session in sessions:
        msgs = session["messages"]
        for i, msg in enumerate(msgs):
            if role_filter and msg["role"] != role_filter:
                continue
            if pattern.search(msg["text"]):
                ctx_before = msgs[max(0, i - 1):i]
                ctx_after = msgs[i + 1:min(len(msgs), i + 2)]
                results.append({
                    "session_id": session["id"],
                    "session_date": session["date"],
                    "role": msg["role"],
                    "text": msg["text"][:500],
                    "timestamp": msg["timestamp"][:16].replace("T", " ")
                                if msg["timestamp"] else "",
                    "context_before": [{"role": m["role"], "text": m["text"][:200]}
                                       for m in ctx_before],
                    "context_after": [{"role": m["role"], "text": m["text"][:200]}
                                      for m in ctx_after],
                })
                if len(results) >= max_results:
                    return results
    return results
