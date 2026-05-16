#!/usr/bin/env python3
"""Search past Claude Code sessions — CLI tool and library."""

import json
import re
import sys
from datetime import datetime
from pathlib import Path

SESSION_DIR = Path.home() / ".claude/projects/-Users-pascal-Desktop-jarvis"


def load_sessions() -> list[dict]:
    """Load all sessions with parsed messages."""
    sessions = []
    for f in sorted(SESSION_DIR.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True):
        messages = []
        session_id = f.stem
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
            content = msg.get("content", "")
            text = _extract_text(content)
            if not text.strip():
                continue
            ts = obj.get("timestamp", "")
            if ts and not first_ts:
                first_ts = ts
            messages.append({"role": role, "text": text, "timestamp": ts})
        if messages:
            sessions.append({
                "id": session_id,
                "date": first_ts[:16].replace("T", " ") if first_ts else "",
                "messages": messages,
            })
    return sessions


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
                    parts.append(f"[tool: {c.get('name', '')}({json.dumps(c.get('input', {}))[:100]})]")
            elif isinstance(c, str):
                parts.append(c)
        return "\n".join(parts)
    return ""


def search(query: str, max_results: int = 20, role_filter: str | None = None) -> list[dict]:
    """Search all sessions for a query string. Returns matching messages with context."""
    sessions = load_sessions()
    results = []
    pattern = re.compile(re.escape(query), re.IGNORECASE)

    for session in sessions:
        msgs = session["messages"]
        for i, msg in enumerate(msgs):
            if role_filter and msg["role"] != role_filter:
                continue
            if pattern.search(msg["text"]):
                # Include surrounding context
                context_before = msgs[max(0, i - 1):i]
                context_after = msgs[i + 1:min(len(msgs), i + 2)]
                results.append({
                    "session_id": session["id"],
                    "session_date": session["date"],
                    "role": msg["role"],
                    "text": msg["text"][:500],
                    "timestamp": msg["timestamp"][:16].replace("T", " ") if msg["timestamp"] else "",
                    "context_before": [{"role": m["role"], "text": m["text"][:200]} for m in context_before],
                    "context_after": [{"role": m["role"], "text": m["text"][:200]} for m in context_after],
                })
                if len(results) >= max_results:
                    return results
    return results


def summarize_sessions() -> list[dict]:
    """Get a summary of all sessions."""
    sessions = load_sessions()
    summaries = []
    for s in sessions:
        user_msgs = [m for m in s["messages"] if m["role"] == "user"]
        first_prompt = user_msgs[0]["text"][:100] if user_msgs else ""
        summaries.append({
            "id": s["id"][:8],
            "date": s["date"],
            "messages": len(s["messages"]),
            "first_prompt": first_prompt,
        })
    return summaries


def main():
    if len(sys.argv) < 2:
        print("Usage:")
        print("  session_search.py search <query>     Search all sessions")
        print("  session_search.py list               List all sessions")
        return

    cmd = sys.argv[1]

    if cmd == "search":
        query = " ".join(sys.argv[2:])
        if not query:
            print("Provide a search query")
            return
        results = search(query)
        if not results:
            print(f"No results for '{query}'")
            return
        for r in results:
            print(f"\n--- [{r['session_date']}] {r['role'].upper()} (session {r['session_id'][:8]})")
            if r["context_before"]:
                for c in r["context_before"]:
                    print(f"  [{c['role']}] {c['text'][:100]}")
            print(f"  >>> {r['text'][:300]}")
            if r["context_after"]:
                for c in r["context_after"]:
                    print(f"  [{c['role']}] {c['text'][:100]}")

    elif cmd == "list":
        for s in summarize_sessions():
            print(f"[{s['date']}] {s['id']}  ({s['messages']} msgs)  {s['first_prompt']}")

    else:
        print(f"Unknown command: {cmd}")


if __name__ == "__main__":
    main()
