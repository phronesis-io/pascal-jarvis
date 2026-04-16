"""Session history utilities — load, search, and summarize Claude Code sessions.

Also used by admin.py. Prefer this module over duplicating JSONL parsing.
"""

import json
import re
from pathlib import Path


def _extract_text(content) -> str:
    """Flatten a Claude Code message.content into a single string.

    Handles the three shapes produced by Claude Code sessions:
      - str (plain assistant text)
      - list of blocks (text / tool_use / tool_result)
      - anything else → empty string
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for c in content:
            if isinstance(c, dict):
                t = c.get("type")
                if t == "text":
                    parts.append(c.get("text", ""))
                elif t == "tool_use":
                    inp = json.dumps(c.get("input", {}))
                    if len(inp) > 120:
                        inp = inp[:120] + "..."
                    parts.append(f"[tool: {c.get('name', '')}({inp})]")
                elif t == "tool_result":
                    rc = c.get("content", "")
                    if isinstance(rc, list):
                        for r in rc:
                            if isinstance(r, dict) and r.get("type") == "text":
                                parts.append(f"[result: {r.get('text', '')[:200]}]")
                    elif isinstance(rc, str):
                        parts.append(f"[result: {rc[:200]}]")
            elif isinstance(c, str):
                parts.append(c)
        return "\n".join(parts)
    return ""


def load_session_messages(path: str | Path) -> list[dict]:
    """Parse a single session JSONL file into a list of {role, text, timestamp}."""
    path = Path(path)
    messages: list[dict] = []
    if not path.exists():
        return messages
    try:
        handle = path.open(encoding="utf-8", errors="ignore")
    except OSError:
        return messages
    with handle as fh:
        for line in fh:
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
            messages.append({
                "role": role,
                "text": text.strip(),
                "timestamp": ts[:19].replace("T", " ") if ts else "",
            })
    return messages


def load_sessions(session_dir: str | Path) -> list[dict]:
    """Load all sessions with metadata, newest first."""
    session_dir = Path(session_dir)
    sessions: list[dict] = []
    if not session_dir.is_dir():
        return sessions
    files = sorted(session_dir.glob("*.jsonl"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    for f in files:
        messages = load_session_messages(f)
        if not messages:
            continue
        first_ts = messages[0]["timestamp"]
        sessions.append({
            "id": f.stem,
            "date": first_ts[:16] if first_ts else "",
            "messages": messages,
        })
    return sessions


def session_meta(path: str | Path) -> dict | None:
    """Return lightweight metadata for a session file (no full message list)."""
    path = Path(path)
    messages = load_session_messages(path)
    if not messages:
        return None
    user_msgs = [m for m in messages if m["role"] == "user"]
    return {
        "id": path.stem,
        "date": messages[0]["timestamp"][:16] if messages[0]["timestamp"] else "",
        "message_count": len(messages),
        "first_prompt": user_msgs[0]["text"][:120] if user_msgs else "",
    }


def sessions_meta(session_dir: str | Path) -> list[dict]:
    """Return metadata for all sessions in `session_dir`, newest first."""
    session_dir = Path(session_dir)
    result: list[dict] = []
    if not session_dir.is_dir():
        return result
    files = sorted(session_dir.glob("*.jsonl"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    for f in files:
        meta = session_meta(f)
        if meta:
            result.append(meta)
    return result


def search(session_dir: str | Path, query: str,
           max_results: int = 40, session_id: str | None = None,
           role_filter: str | None = None) -> list[dict]:
    """Search sessions for a query (case-insensitive substring) with context.

    Returns a list of match records with surrounding turns, suitable for UI.
    """
    results: list[dict] = []
    if not query:
        return results
    session_dir = Path(session_dir)
    pattern = re.compile(re.escape(query), re.IGNORECASE)

    if session_id:
        files = [session_dir / f"{session_id}.jsonl"]
    else:
        files = sorted(
            [p for p in session_dir.glob("*.jsonl")],
            key=lambda p: p.stat().st_mtime, reverse=True,
        )

    for f in files:
        if not f.exists():
            continue
        messages = load_session_messages(f)
        if not messages:
            continue
        sid = f.stem
        date_str = messages[0]["timestamp"][:16] if messages[0]["timestamp"] else ""
        for i, msg in enumerate(messages):
            if role_filter and msg["role"] != role_filter:
                continue
            if not pattern.search(msg["text"]):
                continue
            ctx_before = messages[max(0, i - 3):i]
            ctx_after = messages[i + 1:min(len(messages), i + 4)]
            results.append({
                "session_id": sid,
                "session_date": date_str,
                "role": msg["role"],
                "text": msg["text"][:800],
                "timestamp": msg["timestamp"],
                "context_before": [
                    {"role": m["role"], "text": m["text"][:300]} for m in ctx_before
                ],
                "context_after": [
                    {"role": m["role"], "text": m["text"][:300]} for m in ctx_after
                ],
            })
            if len(results) >= max_results:
                return results
    return results
