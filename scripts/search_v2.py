#!/usr/bin/env python3
"""Search past Claude Code sessions — used by the session-memory skill.
v2: added --all (cross-project), active command."""

import json
import re
import sys
import time
from pathlib import Path

CLAUDE_DIR = Path.home() / ".claude"


def _find_project_dir() -> Path:
    cwd = Path.cwd().resolve()
    encoded = str(cwd).replace("/", "-")
    candidate = CLAUDE_DIR / "projects" / encoded
    if candidate.is_dir():
        return candidate
    projects = CLAUDE_DIR / "projects"
    if projects.is_dir():
        for d in sorted(projects.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if d.is_dir() and list(d.glob("*.jsonl")):
                return d
    return candidate


def _all_project_dirs() -> list[tuple[str, Path]]:
    projects = CLAUDE_DIR / "projects"
    if not projects.is_dir():
        return []
    result = []
    for d in sorted(projects.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if not d.is_dir():
            continue
        if not list(d.glob("*.jsonl")):
            continue
        name = _clean_project_name(d.name)
        result.append((name, d))
    return result


def _clean_project_name(slug: str) -> str:
    segments = slug.split("-")
    cleaned = []
    skip = True
    for s in segments:
        if skip:
            if s.lower() in ("", "users", "pascal", "desktop"):
                continue
            skip = False
        cleaned.append(s)
    return "-".join(cleaned) if cleaned else slug


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


def _load_session_messages(path: Path) -> list[dict]:
    messages = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
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
        messages.append({"role": role, "text": text.strip(), "timestamp": ts})
    return messages


def cmd_search(query: str, max_results: int = 20, all_projects: bool = False):
    if all_projects:
        dirs = _all_project_dirs()
    else:
        dirs = [("current", _find_project_dir())]

    pattern = re.compile(re.escape(query), re.IGNORECASE)
    results = []

    for proj_name, project_dir in dirs:
        for f in sorted(project_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True):
            messages = _load_session_messages(f)
            sid = f.stem
            date_str = ""
            if messages and messages[0].get("timestamp"):
                date_str = messages[0]["timestamp"][:16].replace("T", " ")

            for i, msg in enumerate(messages):
                if pattern.search(msg["text"]):
                    ctx_before = messages[max(0, i - 1):i]
                    ctx_after = messages[i + 1:min(len(messages), i + 2)]
                    results.append({
                        "project": proj_name,
                        "session_id": sid,
                        "session_date": date_str,
                        "role": msg["role"],
                        "text": msg["text"][:500],
                        "timestamp": msg["timestamp"][:16].replace("T", " ") if msg["timestamp"] else "",
                        "context_before": [{"role": m["role"], "text": m["text"][:200]} for m in ctx_before],
                        "context_after": [{"role": m["role"], "text": m["text"][:200]} for m in ctx_after],
                    })
                    if len(results) >= max_results:
                        break
            if len(results) >= max_results:
                break
        if len(results) >= max_results:
            break

    print(json.dumps(results, indent=2, ensure_ascii=False))


def cmd_list(all_projects: bool = False):
    if all_projects:
        dirs = _all_project_dirs()
    else:
        dirs = [("current", _find_project_dir())]

    sessions = []
    for proj_name, project_dir in dirs:
        for f in sorted(project_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True):
            messages = _load_session_messages(f)
            if not messages:
                continue
            user_msgs = [m for m in messages if m["role"] == "user"]
            first_prompt = user_msgs[0]["text"][:100] if user_msgs else ""
            date_str = messages[0]["timestamp"][:16].replace("T", " ") if messages[0].get("timestamp") else ""
            sessions.append({
                "project": proj_name,
                "id": f.stem[:8],
                "full_id": f.stem,
                "date": date_str,
                "messages": len(messages),
                "first_prompt": first_prompt,
            })

    print(json.dumps(sessions, indent=2, ensure_ascii=False))


def cmd_active(minutes: int = 30):
    """Show sessions with activity in the last N minutes, across all projects."""
    cutoff = time.time() - (minutes * 60)
    dirs = _all_project_dirs()
    active = []

    for proj_name, project_dir in dirs:
        for f in project_dir.glob("*.jsonl"):
            if f.stat().st_mtime < cutoff:
                continue
            raw = f.read_bytes()
            tail = raw[-10000:] if len(raw) > 10000 else raw
            lines = tail.decode("utf-8", errors="ignore").splitlines()

            last_ts = ""
            last_user_text = ""
            last_assistant_text = ""
            for line in reversed(lines):
                try:
                    obj = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if obj.get("type") not in ("user", "assistant"):
                    continue
                msg = obj.get("message", {})
                text = _extract_text(msg.get("content", ""))
                if not text.strip():
                    continue
                ts = obj.get("timestamp", "")
                if not last_ts:
                    last_ts = ts
                if obj["type"] == "user" and not last_user_text:
                    last_user_text = text.strip()[:200]
                if obj["type"] == "assistant" and not last_assistant_text:
                    last_assistant_text = text.strip()[:200]
                if last_user_text and last_assistant_text:
                    break

            active.append({
                "project": proj_name,
                "session_id": f.stem[:8],
                "last_activity": last_ts,
                "last_user": last_user_text,
                "last_assistant": last_assistant_text,
            })

    active.sort(key=lambda x: x["last_activity"], reverse=True)
    print(json.dumps(active, indent=2, ensure_ascii=False))


def cmd_read(session_prefix: str, all_projects: bool = False):
    if all_projects:
        dirs = _all_project_dirs()
    else:
        dirs = [("current", _find_project_dir())]

    for proj_name, project_dir in dirs:
        for f in project_dir.glob("*.jsonl"):
            if f.stem.startswith(session_prefix):
                messages = _load_session_messages(f)
                output = []
                for m in messages:
                    ts = m["timestamp"][:16].replace("T", " ") if m.get("timestamp") else ""
                    output.append({
                        "role": m["role"],
                        "text": m["text"][:2000],
                        "timestamp": ts,
                    })
                print(json.dumps(output, indent=2, ensure_ascii=False))
                return

    print(json.dumps({"error": f"No session found matching '{session_prefix}'"}))


def main():
    if len(sys.argv) < 2:
        print("Usage:")
        print("  search.py search <query>              Search current project sessions")
        print("  search.py search --all <query>         Search ALL project sessions")
        print("  search.py list                         List current project sessions")
        print("  search.py list --all                   List ALL project sessions")
        print("  search.py active [minutes]             Show active sessions across all projects (default: 30min)")
        print("  search.py read <session-prefix>        Read a session (current project)")
        print("  search.py read --all <session-prefix>  Read a session (search all projects)")
        sys.exit(1)

    cmd = sys.argv[1]
    args = sys.argv[2:]

    all_flag = "--all" in args
    if all_flag:
        args = [a for a in args if a != "--all"]

    if cmd == "search" and args:
        cmd_search(" ".join(args), all_projects=all_flag)
    elif cmd == "list":
        cmd_list(all_projects=all_flag)
    elif cmd == "active":
        minutes = int(args[0]) if args else 30
        cmd_active(minutes)
    elif cmd == "read" and args:
        cmd_read(args[0], all_projects=all_flag)
    else:
        print(f"Unknown command or missing args: {cmd}")
        sys.exit(1)


if __name__ == "__main__":
    main()
