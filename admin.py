#!/usr/bin/env python3
"""Jarvis Admin Console — memories, sessions, search, settings, skills.

Config-driven dashboard backed by jarvis.yaml. Run standalone:

    python3 admin.py

Or enable `admin.enabled: true` in jarvis.yaml (reserved for future auto-start).
"""

import fcntl
import http.server
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# Make core/ importable
ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

from core import search as core_search
from core.claude_projects import project_dir as _claude_project_dir
from core.config import Config
from core.richview import get_view, list_views
from core.session import NAMESPACE as SESSION_NAMESPACE

CONFIG = Config(ROOT / "jarvis.yaml")

# Global Claude Code locations (shared across all projects)
CLAUDE_DIR = Path.home() / ".claude"
SKILLS_DIR = CLAUDE_DIR / "skills"
PLUGINS_DIR = CLAUDE_DIR / "plugins/marketplaces"

# Project-specific: sessions live in the Claude project dir derived from work_dir
_work_slug = str(CONFIG.work_dir).replace("/", "-")
PROJECT_DIR = CLAUDE_DIR / "projects" / _work_slug
MEMORY_DIR = CONFIG.memory_dir
WORK_DIR = CONFIG.work_dir

HOST = CONFIG.admin.get("host", "127.0.0.1")
PORT = int(CONFIG.admin.get("port", 3456))

# Optional bearer-token auth. When unset, admin is reachable without auth
# (default — fine for 127.0.0.1 single-user use). Set jarvis.yaml:
#   admin:
#     token: "some-random-secret"
# and clients must send `X-Admin-Token: <value>`.
ADMIN_TOKEN = str(CONFIG.admin.get("token", "") or "")

# ── POST CSRF token (REQ-48 security trio) ──────────────────────────
# Every POST/DELETE must carry X-Admin-Token. The token is generated at
# startup (random, written to .admin_token mode 0600) and embedded into the
# served HTML as a <meta> tag, so the frontend reads it locally while a
# cross-origin attacker cannot: the custom header forces a CORS preflight
# that we never answer. Lazy + ROOT-keyed so tests can redirect ROOT.
_post_token_cache: dict = {"path": None, "token": ""}

# Hosts a browser is allowed to address this admin as. Anything else (DNS
# rebinding, a LAN name, a public IP) is rejected before private reads or
# state-changing verbs. Health and sanitized RichView pages are the two
# deliberate public-read exceptions.
_ALLOWED_HOSTS = {"localhost", "127.0.0.1", "::1", ""}

# Trigger file consumed by core.heartbeat_loop — file CONTENT is the bare
# task name to force (REQ-37/47②). Module-level so tests can repoint it.
HEARTBEAT_TRIGGER_PATH = Path("/tmp/jarvis-heartbeat-trigger")

# Known info-level failure signatures: these lines log at INFO but represent
# real failures (REQ-48①) — /api/logs always flags them regardless of grep.
LOG_FAILURE_SIGNATURES = (
    "timed out (60s)",
    "JSON parse failed",
    "exited with code 1",
    "stderr:",
)

# EigenFlux template preferences (the old radio values). A save replacing a
# non-empty free-text preference with one of these needs confirm_overwrite.
EF_TEMPLATE_PREFS = {"Push everything", "Action-only", "Digest", "Silent"}


def _get_post_token() -> str:
    """Read (or create, 0600) ROOT/.admin_token. Empty string on I/O failure
    — callers fail CLOSED (no token file ⇒ no POSTs), never open."""
    path = Path(ROOT) / ".admin_token"
    key = str(path)
    if _post_token_cache["path"] == key and _post_token_cache["token"]:
        return _post_token_cache["token"]
    token = ""
    try:
        if path.exists():
            # Defensive self-heal: a previously-leaked file (created 0644 before
            # an earlier chmod, or chmod failed) tightens itself on next read.
            if path.stat().st_mode & 0o077:
                os.chmod(path, 0o600)
            token = path.read_text(encoding="utf-8").strip()
        if not token:
            import secrets
            token = secrets.token_hex(16)
            # Create atomically at 0600 — write_text()+chmod left a window at
            # 0644 (umask default), and if chmod ever failed the 0644 file
            # persisted (later reads skip the chmod). O_CREAT honours the mode.
            fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                os.write(fd, (token + "\n").encode("utf-8"))
            finally:
                os.close(fd)
    except OSError as e:
        print(f"[admin] cannot read/create .admin_token: {e}", file=sys.stderr)
        token = ""
    _post_token_cache.update(path=key, token=token)
    return token


def _pid_dead(pid: int) -> bool:
    """True when `pid` is gone OR is a zombie (dead, unreaped by its parent).

    kill(pid, 0) succeeding is NOT proof of life: a SIGKILLed process whose
    parent hasn't wait()ed yet still answers signal 0. stop_task must not
    refuse to clear a lock (or claim the kill failed) over a corpse.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except OSError:
        return False  # exists but unreachable (e.g. not ours) — alive
    try:
        out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=2)
        if out.returncode != 0:
            return True  # ps can't see it — gone between the two checks
        return out.stdout.strip().upper().startswith("Z")
    except Exception:
        return False

# Session tracker (maps Lark conv_key → current session_id + counter)
SESSION_TRACKER = ROOT / "active_sessions.json"

# Project dirs to search when looking up historical session files.
# Primary = PROJECT_DIR (derived from current work_dir).
# Also check legacy paths so history from before a work_dir change is visible.
SESSION_SEARCH_PATHS = [
    PROJECT_DIR,
    _claude_project_dir(ROOT),
]

# ── Caches ───────────────────────────────────────────────────────────
# Admin is read-only and called on every page navigation; cache heavy
# session-meta scans by (path, newest-mtime) tuple.
_CACHE_TTL_SECONDS = 5
_cache_lock = threading.Lock()
_sessions_meta_cache: dict = {"key": None, "data": [], "time": 0.0}
_lark_chats_cache: dict = {"key": None, "data": [], "time": 0.0}


def _dir_fingerprint(path: Path) -> tuple:
    """Cheap cache key: newest mtime + file count in a directory."""
    if not path.is_dir():
        return (0.0, 0)
    mtimes = [p.stat().st_mtime for p in path.glob("*.jsonl")]
    return (max(mtimes) if mtimes else 0.0, len(mtimes))

# HTML template loaded from static/admin.html (extracted for maintainability)
_HTML_PATH = ROOT / "static" / "admin.html"
HTML = _HTML_PATH.read_text(encoding="utf-8") if _HTML_PATH.exists() else "<!DOCTYPE html><html><body><h1>admin.html not found</h1></body></html>"


# ── Data loaders ─────────────────────────────────────────────────────

def parse_memory(path: Path) -> dict | None:
    text = path.read_text(encoding="utf-8").strip()
    if not text.startswith("---"):
        return None
    parts = text.split("---", 2)
    if len(parts) < 3:
        return None
    meta = {}
    for line in parts[1].strip().splitlines():
        m = re.match(r"(\w+):\s*(.+)", line)
        if m:
            meta[m.group(1)] = m.group(2).strip().strip('"').strip("'")
    return {
        "name": meta.get("name", path.stem),
        "description": meta.get("description", ""),
        "type": meta.get("type", "unknown"),
        "body": parts[2].strip(),
        "file": str(path.relative_to(MEMORY_DIR)) if path.is_relative_to(MEMORY_DIR) else path.name,
    }


def _load_session_messages(path: Path) -> list[dict]:
    """Thin wrapper for core.search.load_session_messages (kept for compatibility)."""
    return core_search.load_session_messages(path)


def sessions_meta() -> list:
    """Session metadata for the primary project dir — cached by (mtime, count)."""
    key = _dir_fingerprint(PROJECT_DIR)
    now = time.time()
    with _cache_lock:
        if (_sessions_meta_cache["key"] == key
                and now - _sessions_meta_cache["time"] < _CACHE_TTL_SECONDS):
            return _sessions_meta_cache["data"]
    data = core_search.sessions_meta(PROJECT_DIR)
    with _cache_lock:
        _sessions_meta_cache.update(key=key, data=data, time=now)
    return data


def load_full_session(session_id: str) -> list:
    """Load messages for a session, searching primary + legacy project dirs."""
    path = _find_session_file(session_id)
    if not path:
        return []
    return core_search.load_chat_messages(path, verbose=True)


def search_sessions(query: str, session_id: str = "", max_results: int = 40) -> list:
    return core_search.search(PROJECT_DIR, query,
                              max_results=max_results,
                              session_id=session_id or None)


def _find_session_file(session_id: str) -> Path | None:
    """Search known project dirs for a session file. Returns first match."""
    if not session_id or ".." in session_id or "/" in session_id or "\\" in session_id:
        return None
    for base in SESSION_SEARCH_PATHS:
        candidate = base / f"{session_id}.jsonl"
        if not candidate.resolve().is_relative_to(base.resolve()):
            continue
        if candidate.exists():
            return candidate
    return None


def _session_info(session_id: str) -> dict:
    """Lookup a session file across search paths, return metadata (empty dict if missing)."""
    path = _find_session_file(session_id)
    if not path:
        return {"exists": False}
    try:
        msgs = core_search.load_session_messages(path)
    except Exception:
        return {"exists": True, "error": "failed to parse"}
    user_msgs = [m for m in msgs if m["role"] == "user"]
    return {
        "exists": True,
        "path": str(path.parent),
        "size_bytes": path.stat().st_size,
        "message_count": len(msgs),
        "first_ts": msgs[0]["timestamp"] if msgs else "",
        "last_ts": msgs[-1]["timestamp"] if msgs else "",
        "first_prompt": user_msgs[0]["text"][:120] if user_msgs else "",
    }


def lark_chats() -> list:
    """For each Lark conv_key in active_sessions.json, return all sessions (counter 1 → N)
    in chronological order, with metadata showing which is currently active.

    Cached by tracker mtime + session dir fingerprint so repeat requests are cheap.
    """
    if not SESSION_TRACKER.exists():
        return []

    key = (
        SESSION_TRACKER.stat().st_mtime,
        _dir_fingerprint(PROJECT_DIR),
    )
    now = time.time()
    with _cache_lock:
        if (_lark_chats_cache["key"] == key
                and now - _lark_chats_cache["time"] < _CACHE_TTL_SECONDS):
            return _lark_chats_cache["data"]

    try:
        tracker = json.loads(SESSION_TRACKER.read_text())
    except Exception:
        return []

    chats = []
    for conv_key, entry in tracker.items():
        active_sid = entry.get("session_id", "")
        counter = entry.get("counter", 0)
        kind = "p2p" if conv_key.startswith("ou_") else ("group" if conv_key.startswith("oc_") else "unknown")

        sessions = []
        # Find first counter that actually has a file (skip pre-rotation history)
        first_existing = None
        for c in range(1, counter + 1):
            sid = str(uuid.uuid5(SESSION_NAMESPACE, f"{conv_key}-{c}"))
            info = _session_info(sid)
            if info.get("exists") and first_existing is None:
                first_existing = c
            # Only include from first existing onward (skip leading missing)
            if first_existing is not None:
                sessions.append({
                    "counter": c,
                    "session_id": sid,
                    "is_active": (sid == active_sid),
                    **info,
                })

        # Order by counter (earliest first) — the rotation order
        sessions.sort(key=lambda s: s["counter"])

        # Sum stats that are derivable
        total_msgs = sum(s.get("message_count", 0) for s in sessions)

        chats.append({
            "conv_key": conv_key,
            "kind": kind,
            "current_counter": counter,
            "active_session_id": active_sid,
            "total_messages": total_msgs,
            "sessions": sessions,
        })

    # Put the p2p chats first, then groups — users usually care about 1-on-1
    chats.sort(key=lambda c: (c["kind"] != "p2p", c["conv_key"]))

    with _cache_lock:
        _lark_chats_cache.update(key=key, data=chats, time=now)
    return chats


def live_chat() -> dict:
    """Return the most recently active Lark conversation's chat messages.

    Finds the active session with the newest last_ts across all tracked
    Lark conversations, then returns its messages via the chat-only loader
    (no tool_use / tool_result noise, no truncation).
    """
    if not SESSION_TRACKER.exists():
        return {"session_id": "", "conv_key": "", "messages": []}
    try:
        tracker = json.loads(SESSION_TRACKER.read_text())
    except Exception:
        return {"session_id": "", "conv_key": "", "messages": []}

    best_sid = ""
    best_key = ""
    best_mtime = 0.0
    for conv_key, entry in tracker.items():
        sid = entry.get("session_id", "")
        path = _find_session_file(sid)
        if path and path.exists():
            mt = path.stat().st_mtime
            if mt > best_mtime:
                best_mtime = mt
                best_sid = sid
                best_key = conv_key

    if not best_sid:
        return {"session_id": "", "conv_key": "", "messages": []}

    path = _find_session_file(best_sid)
    msgs = core_search.load_chat_messages(path, verbose=True) if path else []
    return {
        "session_id": best_sid,
        "conv_key": best_key,
        "messages": msgs,
    }


def bot_status() -> dict:
    """Return the bot's current status: alive/dead, working/idle, last activity."""
    import glob as _glob
    import subprocess as _sp

    # 1. Process alive?
    try:
        result = _sp.run(["pgrep", "-f", "bash.*bot\\.sh"], capture_output=True, text=True, timeout=3)
        alive = bool(result.stdout.strip())
    except Exception:
        alive = False

    # 2. Currently working? (session lock files exist)
    locks = _glob.glob(str(ROOT / ".session_lock_*"))
    working = len(locks) > 0

    # 3. Last log activity — read tail of log, find newest timestamped line
    log_file = ROOT / "jarvis.log"
    last_log_line = ""
    last_log_ts = ""
    if log_file.exists():
        try:
            size = log_file.stat().st_size
            with open(log_file, "rb") as f:
                f.seek(max(0, size - 4096))
                tail = f.read().decode("utf-8", errors="ignore")
            for line in reversed(tail.splitlines()):
                line = line.strip()
                if not line:
                    continue
                m = re.match(r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]", line)
                if m:
                    last_log_ts = m.group(1)
                    last_log_line = line[:200]
                    break
        except Exception:
            pass

    # 4. Seconds since last activity
    last_activity_ago = None
    if last_log_ts:
        try:
            from core.timeutil import now_local
            from datetime import datetime
            dt = datetime.strptime(last_log_ts, "%Y-%m-%d %H:%M:%S")
            last_activity_ago = int((now_local().replace(tzinfo=None) - dt).total_seconds())
        except Exception:
            pass

    # Determine overall state
    if not alive:
        state = "dead"
    elif working:
        state = "working"
    else:
        state = "idle"

    return {
        "state": state,
        "alive": alive,
        "working": working,
        "active_locks": len(locks),
        "last_log_ts": last_log_ts,
        "last_log_line": last_log_line,
        "last_activity_ago": last_activity_ago,
    }


def load_skills() -> list:
    skills = []
    if SKILLS_DIR.is_dir():
        for d in sorted(SKILLS_DIR.iterdir()):
            if not d.is_dir():
                continue
            skill_file = d / "SKILL.md"
            desc = ""
            if skill_file.exists():
                text = skill_file.read_text(encoding="utf-8")
                m = re.search(r'description:\s*["\|]([^"]+)', text)
                if m:
                    desc = m.group(1).strip()[:120]
            skills.append({"name": d.name, "description": desc})
    if PLUGINS_DIR.is_dir():
        for pj in PLUGINS_DIR.rglob("plugin.json"):
            try:
                data = json.loads(pj.read_text())
                skills.append({
                    "name": data.get("name", pj.parent.parent.name),
                    "description": data.get("description", "")[:120],
                })
            except Exception:
                continue
    return skills


def load_settings() -> dict:
    result = {}
    gs = CLAUDE_DIR / "settings.json"
    if gs.exists():
        try:
            result["Global Settings"] = json.loads(gs.read_text())
        except Exception:
            result["Global Settings"] = {}
    ps = PROJECT_DIR / "settings.json"
    if ps.exists():
        try:
            result["Project Settings"] = json.loads(ps.read_text())
        except Exception:
            pass
    for label, path in [
        ("Global CLAUDE.md", CLAUDE_DIR / "CLAUDE.md"),
        ("Project CLAUDE.md", WORK_DIR / "CLAUDE.md"),
    ]:
        if path.exists():
            result[label] = {"content": path.read_text(encoding="utf-8")[:3000]}
    gss = CLAUDE_DIR / "sessions"
    if gss.is_dir():
        result["Global Sessions"] = {
            "count": len(list(gss.glob("*.json"))),
            "files": [f.name for f in sorted(gss.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:10]],
        }
    # Jarvis-specific paths (so user can verify admin reads the right dirs)
    result["Jarvis Paths"] = {
        "work_dir": str(WORK_DIR),
        "memory_dir": str(MEMORY_DIR),
        "project_dir": str(PROJECT_DIR),
    }
    return result


# ── Memory write/delete ──────────────────────────────────────────────

def save_memory(filename: str, name: str, description: str, mem_type: str, body: str) -> dict:
    """Write a memory file with frontmatter. Returns {"ok": True} or {"error": "..."}."""
    if not filename.endswith(".md"):
        return {"error": "filename must end with .md"}
    if "\\" in filename or ".." in filename:
        return {"error": "invalid filename — '..' not allowed"}
    if not name.strip():
        return {"error": "name is required"}
    content = f"---\nname: {name}\ndescription: {description}\ntype: {mem_type}\n---\n\n{body}"
    target = MEMORY_DIR / filename
    if not target.resolve().is_relative_to(MEMORY_DIR.resolve()):
        return {"error": "path escapes memory directory"}
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    try:
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, target)
        return {"ok": True}
    except Exception as e:
        return {"error": str(e)}


def delete_memory(filename: str) -> dict:
    """Delete a memory file. Returns {"ok": True} or {"error": "..."}."""
    if filename == "MEMORY.md":
        return {"error": "cannot delete MEMORY.md"}
    if "\\" in filename or ".." in filename:
        return {"error": "invalid filename — '..' not allowed"}
    target = MEMORY_DIR / filename
    if not target.resolve().is_relative_to(MEMORY_DIR.resolve()):
        return {"error": "path escapes memory directory"}
    if not target.exists():
        return {"error": "file not found"}
    try:
        target.unlink()
        return {"ok": True}
    except Exception as e:
        return {"error": str(e)}


# ── Heartbeat status ────────────────────────────────────────────────

def heartbeat_timeline() -> list:
    """Read heartbeat_outbox.jsonl + checkin/content-recommend logs, return combined timeline."""
    entries: list[dict] = []

    # 1) heartbeat_outbox.jsonl
    outbox = ROOT / "heartbeat_outbox.jsonl"
    if outbox.exists():
        try:
            for line in outbox.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    source = obj.get("source", "heartbeat")
                    entries.append({
                        "ts": obj.get("ts", ""),
                        "source": source,
                        "text": obj.get("text", ""),
                    })
                except json.JSONDecodeError:
                    continue
        except Exception:
            pass

    # 2) checkin_log.jsonl
    checkin_log = MEMORY_DIR / "system" / "checkin_log.jsonl"
    if checkin_log.exists():
        try:
            for line in checkin_log.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    entries.append({
                        "ts": obj.get("ts", ""),
                        "source": "checkin",
                        "text": obj.get("content", ""),
                    })
                except json.JSONDecodeError:
                    continue
        except Exception:
            pass

    # 3) content_recommend_log.jsonl
    recommend_log = MEMORY_DIR / "system" / "content_recommend_log.jsonl"
    if recommend_log.exists():
        try:
            for line in recommend_log.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    text = obj.get("title", "")
                    if obj.get("url"):
                        text += "\n" + obj["url"]
                    if obj.get("category"):
                        text += f"\n[{obj['category']}]"
                    entries.append({
                        "ts": obj.get("ts", ""),
                        "source": "content-recommend",
                        "text": text,
                    })
                except json.JSONDecodeError:
                    continue
        except Exception:
            pass

    # De-duplicate: outbox may contain checkin/recommend entries too.
    # Use (ts, first-50-chars-of-text) as dedup key.
    seen: set[tuple] = set()
    unique: list[dict] = []
    for e in entries:
        key = (e["ts"], e["text"][:50])
        if key not in seen:
            seen.add(key)
            unique.append(e)

    # Sort by timestamp descending (newest first)
    unique.sort(key=lambda e: e["ts"], reverse=True)
    return unique


def heartbeat_status() -> dict:
    """Read heartbeat_state.json + HEARTBEAT.md, return structured task status."""
    from core.heartbeat import parse_heartbeat, parse_interval

    heartbeat_path = ROOT / "HEARTBEAT.md"
    state_path = ROOT / "heartbeat_state.json"

    if not heartbeat_path.exists():
        return {"error": "HEARTBEAT.md not found"}

    tasks = parse_heartbeat(heartbeat_path)
    state = {}
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    now = int(time.time())
    result = []
    for task in tasks:
        last_run = state.get(task["name"], {}).get("last_run", 0)
        interval = task["interval"]
        seconds_until_due = max(0, interval - (now - last_run)) if last_run else 0
        # Human-readable interval string
        if interval >= 86400:
            interval_str = f"{interval // 86400}d"
        elif interval >= 3600:
            interval_str = f"{interval // 3600}h"
        elif interval >= 60:
            interval_str = f"{interval // 60}m"
        else:
            interval_str = f"{interval}s"

        result.append({
            "name": task["name"],
            "interval": interval,
            "interval_str": interval_str,
            "last_run": last_run,
            "seconds_until_due": seconds_until_due,
            "pre": task.get("pre", ""),
            "post": task.get("post", ""),
            "prompt": task.get("prompt", "").strip(),
        })
    # mtime: the concurrent-edit token. The client echoes it back on save and
    # the server 409s if the file changed underneath (REQ-47⑤).
    return {"tasks": result, "mtime": heartbeat_path.stat().st_mtime}


def save_heartbeat_task(name: str, interval_str: str, pre: str, post: str,
                        prompt: str, expected_mtime=None) -> dict:
    """Update a single task in HEARTBEAT.md. Rewrites the file preserving all other tasks.

    REQ-47⑤: validates the interval (same grammar core.heartbeat.parse_interval
    accepts, but strict — parse_interval silently defaults nonsense to 600s,
    which is exactly the silent-drop class this guards against) and rejects the
    write with {conflict: True} when the on-disk mtime differs from the one the
    client loaded (concurrent edit by another process/editor).
    """
    heartbeat_path = ROOT / "HEARTBEAT.md"
    if not heartbeat_path.exists():
        return {"error": "HEARTBEAT.md not found"}

    m = re.fullmatch(r"(\d+)\s*([smhd])", interval_str.strip())
    if not m or int(m.group(1)) <= 0:
        return {"error": f"invalid interval {interval_str!r} — expected <N><s|m|h|d>, e.g. 90s / 10m / 2h / 1d"}

    lock_path = heartbeat_path.with_suffix(".md.lock")
    with open(lock_path, "a") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)

        if expected_mtime is not None:
            try:
                expected = float(expected_mtime)
            except (TypeError, ValueError):
                return {"error": f"invalid mtime {expected_mtime!r}"}
            disk_mtime = heartbeat_path.stat().st_mtime
            if abs(disk_mtime - expected) > 1e-4:
                return {"error": "HEARTBEAT.md changed on disk since you loaded it — reload before saving",
                        "conflict": True, "disk_mtime": disk_mtime}

        text = heartbeat_path.read_text(encoding="utf-8")
        lines = text.splitlines()

        # Find the task section by "### <name>"
        header = f"### {name}"
        start_idx = None
        end_idx = None
        for i, line in enumerate(lines):
            if line.strip() == header:
                start_idx = i
            elif start_idx is not None and line.startswith("### ") and i > start_idx:
                end_idx = i
                break
        if start_idx is None:
            return {"error": f"task '{name}' not found in HEARTBEAT.md"}
        if end_idx is None:
            end_idx = len(lines)

        # Build the new task block
        new_block = [header]
        new_block.append(f"- interval: {interval_str}")
        if pre:
            new_block.append(f"- pre: {pre}")
        if post:
            new_block.append(f"- post: {post}")
        new_block.append("- prompt: |")
        for pline in prompt.strip().splitlines():
            new_block.append(f"    {pline}")
        new_block.append("")  # blank line after task

        # Replace the section
        new_lines = lines[:start_idx] + new_block + lines[end_idx:]
        new_text = "\n".join(new_lines)

        # Atomic write
        tmp = heartbeat_path.with_suffix(".md.tmp")
        tmp.write_text(new_text, encoding="utf-8")
        os.replace(tmp, heartbeat_path)

    return {"ok": True, "mtime": heartbeat_path.stat().st_mtime}


# ── EigenFlux settings & status ─────────────────────────────────────

EIGENFLUX_DIR = ROOT / "eigenflux"


def eigenflux_settings() -> dict:
    """Read eigenflux/user_settings.json."""
    settings_path = EIGENFLUX_DIR / "user_settings.json"
    if not settings_path.exists():
        return {}
    try:
        return json.loads(settings_path.read_text(encoding="utf-8"))
    except Exception as e:
        return {"error": str(e)}


def save_eigenflux_settings(settings: dict) -> dict:
    """Write eigenflux/user_settings.json (atomic)."""
    settings_path = EIGENFLUX_DIR / "user_settings.json"
    tmp = settings_path.with_suffix(settings_path.suffix + ".tmp")
    try:
        EIGENFLUX_DIR.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(settings, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, settings_path)
        return {"ok": True}
    except Exception as e:
        return {"error": str(e)}


def eigenflux_status() -> dict:
    """Check if eigenflux CLI is authed by running `eigenflux profile show`."""
    env = os.environ.copy()
    env["PATH"] = str(Path.home() / ".local/bin") + ":" + env.get("PATH", "")
    try:
        result = subprocess.run(
            ["eigenflux", "profile", "show", "-f", "json"],
            capture_output=True, text=True, timeout=10, env=env,
        )
        if result.returncode == 0:
            try:
                data = json.loads(result.stdout)
                # CLI returns {"profile": {...}, "influence": {...}} — flatten
                profile = data.get("profile", {})
                influence = data.get("influence", {})
                return {
                    "authed": True,
                    "agent_name": profile.get("agent_name", ""),
                    "agent_id": profile.get("agent_id", ""),
                    "bio": profile.get("bio", ""),
                    "influence": influence,
                }
            except json.JSONDecodeError:
                return {"authed": True, "raw": result.stdout.strip()}
        return {"authed": False, "stderr": result.stderr.strip()}
    except FileNotFoundError:
        return {"authed": False, "error": "eigenflux CLI not found"}
    except subprocess.TimeoutExpired:
        return {"authed": False, "error": "timeout"}
    except Exception as e:
        return {"authed": False, "error": str(e)}


# 60s cache for the status subprocess (REQ-48③): the `eigenflux profile show`
# call can take seconds; without a cache every EigenFlux-tab visit blocks a
# server thread on it.
_EF_STATUS_TTL = 60
_ef_status_cache: dict = {"data": None, "time": 0.0}


def eigenflux_status_cached() -> dict:
    now = time.time()
    if (_ef_status_cache["data"] is not None
            and now - _ef_status_cache["time"] < _EF_STATUS_TTL):
        return _ef_status_cache["data"]
    data = eigenflux_status()
    _ef_status_cache.update(data=data, time=now)
    return data


# ── Ops depth: logs / sched events / queues / intents (REQ-48) ──────

def tail_log(file_key: str, lines: int = 200, grep: str = "") -> dict:
    """Tail jarvis.log or daemon.log with an optional substring filter.

    Every returned line carries a `flagged` bit when it matches a known
    info-level failure signature — failures that the log level hides.
    """
    files = {"jarvis": ROOT / "jarvis.log", "daemon": ROOT / "daemon.log"}
    path = files.get(file_key)
    if path is None:
        return {"error": f"unknown file {file_key!r} — use file=jarvis|daemon"}
    if not path.exists():
        return {"file": file_key, "lines": [], "flagged_count": 0, "missing": True}

    lines = max(1, min(int(lines), 2000))
    size = path.stat().st_size
    # Tail-read a bounded chunk: enough for `lines` full lines without ever
    # loading a multi-MB log into memory on a server thread.
    chunk = min(size, max(256 * 1024, lines * 500))
    with open(path, "rb") as f:
        f.seek(size - chunk)
        raw = f.read().decode("utf-8", errors="ignore")
    all_lines = raw.splitlines()
    if size > chunk and all_lines:
        all_lines = all_lines[1:]  # first line of a mid-file seek is partial
    if grep:
        all_lines = [ln for ln in all_lines if grep in ln]
    tail = all_lines[-lines:]
    # '"expected": true' = the emitter marked this a by-design event (elected
    # primary probe, warm-tier squeeze) — don't paint it red (REQ-96).
    out = [{"text": ln,
            "flagged": (any(sig in ln for sig in LOG_FAILURE_SIGNATURES)
                        and '"expected": true' not in ln)}
           for ln in tail]
    return {"file": file_key, "lines": out,
            "flagged_count": sum(1 for e in out if e["flagged"])}


def sched_events_tail(task: str = "", limit: int = 500) -> dict:
    """Tail of sched_events.jsonl (rotated gen included), optional task filter."""
    from core.sched_events import query as sched_query
    limit = max(1, min(int(limit), 2000))
    events = sched_query(ROOT, task=task)
    return {"events": events[-limit:], "total": len(events)}


def _read_jsonl(path: Path, limit: int = 500) -> list[dict]:
    out: list[dict] = []
    if not path.exists():
        return out
    try:
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines()[-limit:]:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except OSError:
        pass
    return out


def queues_overview() -> dict:
    """Surface the invisible queues: night queue, intent breach queue,
    running background jobs, Lark delivery state (REQ-48①)."""
    from datetime import datetime
    from core.timeutil import now_local
    now = now_local().replace(tzinfo=None)

    def _age_minutes(ts: str):
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S"):
            try:
                return round((now - datetime.strptime(ts, fmt)).total_seconds() / 60)
            except (ValueError, TypeError):
                continue
        return None

    night = [{
        "ts": e.get("ts", ""),
        "age_minutes": _age_minutes(str(e.get("ts", ""))),
        "text": str(e.get("text", ""))[:200],
    } for e in _read_jsonl(ROOT / "night_queue.jsonl")]

    from core.state_projection import breach_overview, delivery_overview
    breach = breach_overview(ROOT)
    if breach is None:
        breach = _read_jsonl(ROOT / "data" / ".intent_breach_queue.jsonl")

    jobs: dict = {"running": [], "counts": {}}
    reg_path = ROOT / "jobs" / "registry.json"
    if reg_path.exists():
        try:
            registry = json.loads(reg_path.read_text(encoding="utf-8"))
            for job_id, info in registry.items():
                status = info.get("status", "unknown")
                jobs["counts"][status] = jobs["counts"].get(status, 0) + 1
                if status == "running":
                    jobs["running"].append({
                        "id": job_id,
                        "description": str(info.get("description", ""))[:160],
                        "started_at": info.get("started_at", ""),
                        "age_minutes": _age_minutes(str(info.get("started_at", ""))),
                        "pid": info.get("pid"),
                    })
        except (json.JSONDecodeError, OSError) as e:
            jobs["error"] = str(e)

    delivery = delivery_overview(ROOT)
    ds_path = ROOT / ".delivery_state.json"
    if delivery is None and ds_path.exists():
        try:
            delivery = json.loads(ds_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            delivery = {"error": str(e)}
    if delivery is None:
        delivery = {}

    return {"night_queue": night, "breach_queue": breach,
            "jobs": jobs, "delivery_state": delivery}


def intents_overview(status: str = "") -> dict:
    """Intent list + lifecycle/closure stats via core.intentions (REQ-48①)."""
    from core import intentions
    rows = intentions.list_intents(status=status or None, limit=500)
    return {"intents": rows,
            "stats": intentions.intent_stats(),
            "closure": intentions.closure_stats()}


def rearm_intent(intent_id: str) -> dict:
    """Re-arm one still-expired intent through the shared lifecycle rule."""
    from core import intentions

    intent = intentions.get_intent(intent_id)
    if not intent:
        return {"error": "intent not found"}
    result = intentions.rearm_expired_intent(intent_id, actor="admin")
    if result is None:
        return {"error": "intent is no longer expired or schedule is invalid"}
    return {
        "ok": True,
        "id": intent_id,
        "next_fire": result["next_fire"],
    }


# ── RichView Renderer ────────────────────────────────────────────────

# Tag whitelist for model-authored 'html' sections (REQ-48⑤). Everything
# else — attributes included — is stripped; text content is escaped.
_HTML_ALLOWED_TAGS = {"b", "i", "em", "strong", "code", "pre", "br", "p",
                      "ul", "ol", "li", "h1", "h2", "h3", "h4", "a"}
_HTML_VOID_TAGS = {"br"}
_HTML_DROP_CONTENT_TAGS = {"script", "style"}


def _sanitize_html(content: str) -> str:
    """Sanitize model-authored HTML: escape everything by default, allow only
    a small tag whitelist, drop all attributes except http(s) hrefs on <a>.

    RichView pages are served unauthenticated on the SAME origin as the admin
    API — an unescaped model-authored section is stored XSS with a direct line
    to the destructive endpoints. Model output is DATA, not markup.
    """
    import html as html_mod
    from html.parser import HTMLParser

    class _Sanitizer(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.out: list[str] = []
            self._drop_depth = 0  # inside <script>/<style> — drop text too

        def _emit_start(self, tag, attrs):
            if tag in _HTML_DROP_CONTENT_TAGS:
                self._drop_depth += 1
                return
            if tag not in _HTML_ALLOWED_TAGS:
                return
            if tag == "a":
                href = next((v for k, v in attrs if k == "href"), "") or ""
                if href.startswith(("http://", "https://")):
                    self.out.append(
                        f'<a href="{html_mod.escape(href, quote=True)}" '
                        f'rel="noopener noreferrer" target="_blank">')
                else:
                    self.out.append("<a>")
            else:
                self.out.append(f"<{tag}>")  # attributes are never kept

        def handle_starttag(self, tag, attrs):
            self._emit_start(tag, attrs)

        def handle_startendtag(self, tag, attrs):
            if tag in _HTML_VOID_TAGS:
                self.out.append("<br>")

        def handle_endtag(self, tag):
            if tag in _HTML_DROP_CONTENT_TAGS:
                self._drop_depth = max(0, self._drop_depth - 1)
                return
            if tag in _HTML_ALLOWED_TAGS and tag not in _HTML_VOID_TAGS:
                self.out.append(f"</{tag}>")

        def handle_data(self, data):
            if not self._drop_depth:
                self.out.append(html_mod.escape(data))

        # comments / declarations / processing instructions: dropped

    s = _Sanitizer()
    try:
        s.feed(content or "")
        s.close()
    except Exception:
        # Parser blew up on hostile input — fall back to full escape.
        return html_mod.escape(content or "")
    return "".join(s.out)


def _render_richview(view: dict) -> str:
    """Render a view dict into a self-contained HTML page."""
    import html as html_mod
    from datetime import datetime

    title = html_mod.escape(view.get("title", "Jarvis View"))
    created = datetime.fromtimestamp(view["created_at"]).strftime("%Y-%m-%d %H:%M")
    meta = view.get("meta", {})
    source = html_mod.escape(meta.get("source", ""))

    # Render sections to HTML
    body_parts = []
    for sec in view.get("sections", []):
        stype = sec.get("type", "markdown")
        if stype == "markdown":
            # Simple markdown → HTML (basic conversion)
            body_parts.append(f'<div class="rv-section rv-md">{_md_to_html(sec.get("content", ""))}</div>')
        elif stype == "table":
            headers = sec.get("headers", [])
            rows = sec.get("rows", [])
            ths = "".join(f"<th>{html_mod.escape(str(h))}</th>" for h in headers)
            trs = ""
            for row in rows:
                tds = "".join(f"<td>{html_mod.escape(str(c))}</td>" for c in row)
                trs += f"<tr>{tds}</tr>"
            body_parts.append(f'<div class="rv-section"><table><thead><tr>{ths}</tr></thead><tbody>{trs}</tbody></table></div>')
        elif stype == "kv":
            items = sec.get("items", {})
            kvs = "".join(f'<div class="kv-row"><span class="kv-key">{html_mod.escape(str(k))}</span><span class="kv-val">{html_mod.escape(str(v))}</span></div>' for k, v in items.items())
            body_parts.append(f'<div class="rv-section rv-kv">{kvs}</div>')
        elif stype == "code":
            lang = html_mod.escape(sec.get("language", ""))
            code = html_mod.escape(sec.get("content", ""))
            body_parts.append(f'<div class="rv-section"><pre><code class="lang-{lang}">{code}</code></pre></div>')
        elif stype == "timeline":
            events = sec.get("events", [])
            evts = ""
            for ev in events:
                t = html_mod.escape(str(ev.get("time", "")))
                txt = html_mod.escape(str(ev.get("text", "")))
                evts += f'<div class="tl-item"><span class="tl-time">{t}</span><span class="tl-text">{txt}</span></div>'
            body_parts.append(f'<div class="rv-section rv-timeline">{evts}</div>')
        elif stype == "chart":
            # Embed a simple chart config for client-side rendering
            chart_json = html_mod.escape(json.dumps(sec.get("config", {})))
            body_parts.append(f'<div class="rv-section rv-chart" data-chart=\'{chart_json}\'><canvas></canvas></div>')
        elif stype == "html":
            # Model-authored HTML: sanitized, never trusted (REQ-48⑤).
            # views/*.json content is model output reachable by prompt
            # injection — raw passthrough here was stored XSS on the same
            # origin as the destructive admin API.
            body_parts.append(f'<div class="rv-section">{_sanitize_html(sec.get("content", ""))}</div>')
        else:
            body_parts.append(f'<div class="rv-section"><pre>{html_mod.escape(json.dumps(sec, indent=2))}</pre></div>')

    body_html = "\n".join(body_parts)
    source_badge = f'<span class="rv-source">{source}</span>' if source else ""

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
:root {{
  --bg: #0a0a0f; --surface: #12121a; --border: #2a2a3a;
  --text: #e8e8f0; --dim: #8888aa; --accent: #a78bfa;
  --green: #4ade80; --yellow: #facc15; --red: #f87171;
}}
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{
  font-family: -apple-system, 'SF Pro Text', 'Inter', system-ui, sans-serif;
  background: var(--bg); color: var(--text);
  padding: 2rem; max-width: 900px; margin: 0 auto;
  line-height: 1.6;
}}
.rv-header {{
  border-bottom: 1px solid var(--border); padding-bottom: 1rem; margin-bottom: 2rem;
}}
.rv-header h1 {{ font-size: 1.5rem; font-weight: 600; }}
.rv-meta {{ color: var(--dim); font-size: 0.85rem; margin-top: 0.5rem; }}
.rv-source {{
  background: var(--accent); color: #000; padding: 2px 8px;
  border-radius: 4px; font-size: 0.75rem; font-weight: 500;
}}
.rv-section {{ margin-bottom: 1.5rem; }}
.rv-md h1,.rv-md h2,.rv-md h3 {{ margin: 1em 0 0.5em; color: var(--accent); }}
.rv-md h2 {{ font-size: 1.2rem; }}
.rv-md p {{ margin: 0.5em 0; }}
.rv-md ul,.rv-md ol {{ padding-left: 1.5em; margin: 0.5em 0; }}
.rv-md li {{ margin: 0.25em 0; }}
.rv-md strong {{ color: #fff; }}
table {{
  width: 100%; border-collapse: collapse; font-size: 0.9rem;
}}
th, td {{
  padding: 0.6rem 1rem; text-align: left; border-bottom: 1px solid var(--border);
}}
th {{ color: var(--accent); font-weight: 500; font-size: 0.8rem; text-transform: uppercase; letter-spacing: 0.05em; }}
tr:hover td {{ background: var(--surface); }}
.rv-kv {{ background: var(--surface); border-radius: 8px; padding: 1rem; }}
.kv-row {{
  display: flex; justify-content: space-between; padding: 0.4rem 0;
  border-bottom: 1px solid var(--border);
}}
.kv-row:last-child {{ border-bottom: none; }}
.kv-key {{ color: var(--dim); }}
.kv-val {{ font-weight: 500; }}
pre {{
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 8px; padding: 1rem; overflow-x: auto;
  font-size: 0.85rem; line-height: 1.5;
}}
code {{ font-family: 'SF Mono', 'JetBrains Mono', monospace; }}
.rv-timeline {{ position: relative; padding-left: 1.5rem; border-left: 2px solid var(--border); }}
.tl-item {{ margin-bottom: 1rem; position: relative; }}
.tl-item::before {{
  content: ''; position: absolute; left: -1.75rem; top: 0.5rem;
  width: 10px; height: 10px; border-radius: 50%;
  background: var(--accent); border: 2px solid var(--bg);
}}
.tl-time {{ color: var(--accent); font-size: 0.8rem; font-weight: 500; display: block; }}
.tl-text {{ display: block; margin-top: 0.2rem; }}
@media (max-width: 600px) {{
  body {{ padding: 1rem; }}
  .kv-row {{ flex-direction: column; gap: 0.2rem; }}
}}
</style>
</head>
<body>
<div class="rv-header">
  <h1>{title}</h1>
  <div class="rv-meta">{source_badge} {created}</div>
</div>
{body_html}
<script>
// Chart.js lazy-load for chart sections
(function() {{
  const charts = document.querySelectorAll('.rv-chart');
  if (!charts.length) return;
  const s = document.createElement('script');
  s.src = 'https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js';
  s.onload = function() {{
    charts.forEach(el => {{
      const cfg = JSON.parse(el.dataset.chart || '{{}}');
      if (cfg.type) new Chart(el.querySelector('canvas'), cfg);
    }});
  }};
  document.body.appendChild(s);
}})();
</script>
</body>
</html>"""


def _md_to_html(md: str) -> str:
    """Minimal markdown to HTML (no external deps). Handles headers, bold, lists, paragraphs."""
    import html as html_mod
    import re

    lines = md.split("\n")
    out = []
    in_list = False

    for line in lines:
        stripped = line.strip()
        # Headers
        if m := re.match(r"^(#{1,3})\s+(.+)$", stripped):
            if in_list:
                out.append("</ul>")
                in_list = False
            level = len(m.group(1))
            out.append(f"<h{level}>{html_mod.escape(m.group(2))}</h{level}>")
        # List items
        elif re.match(r"^[-*]\s+", stripped):
            if not in_list:
                out.append("<ul>")
                in_list = True
            content = re.sub(r"^[-*]\s+", "", stripped)
            out.append(f"<li>{html_mod.escape(content)}</li>")
        # Empty line
        elif not stripped:
            if in_list:
                out.append("</ul>")
                in_list = False
        # Paragraph
        else:
            if in_list:
                out.append("</ul>")
                in_list = False
            out.append(f"<p>{html_mod.escape(stripped)}</p>")

    if in_list:
        out.append("</ul>")

    result = "\n".join(out)
    # Bold
    result = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", result)
    return result


# ── Server ───────────────────────────────────────────────────────────

class Handler(http.server.BaseHTTPRequestHandler):
    def _check_auth(self) -> bool:
        """If ADMIN_TOKEN is configured, require X-Admin-Token or ?token=... to match.
        Returns True when authorized (or when no token configured)."""
        if not ADMIN_TOKEN:
            return True
        provided = self.headers.get("X-Admin-Token") or ""
        if not provided:
            # Also accept ?token=... for convenience in browsers. Note query
            # tokens leak into access logs/history — header is preferred.
            parsed = urlparse(self.path)
            provided = parse_qs(parsed.query).get("token", [""])[0]
        # Constant-time compare: == leaks match length via timing.
        # Compare BYTES: compare_digest raises TypeError on non-ASCII str,
        # and `provided` is fully caller-controlled.
        import hmac
        return hmac.compare_digest(provided.encode("utf-8", "replace"),
                                   ADMIN_TOKEN.encode("utf-8", "replace"))

    # ── REQ-48 security trio (state-changing verbs) ──────────────────

    def _check_host(self) -> bool:
        """Host header must name localhost — blocks DNS-rebinding and any
        request addressed to a non-loopback name."""
        host = (self.headers.get("Host") or "").strip()
        if host.startswith("["):  # [::1]:3456
            hostname = host.split("]", 1)[0].lstrip("[")
        else:
            hostname = host.split(":", 1)[0]
        return hostname.lower() in _ALLOWED_HOSTS

    def _check_post_token(self) -> bool:
        """X-Admin-Token header must match the .admin_token file (or the
        configured admin.token). A custom header forces a CORS preflight,
        so cross-origin pages cannot fire state-changing requests."""
        import hmac
        provided = self.headers.get("X-Admin-Token") or ""
        if not provided:
            return False
        expected = _get_post_token()
        if expected and hmac.compare_digest(
                provided.encode("utf-8", "replace"),
                expected.encode("utf-8", "replace")):
            return True
        if ADMIN_TOKEN and hmac.compare_digest(
                provided.encode("utf-8", "replace"),
                ADMIN_TOKEN.encode("utf-8", "replace")):
            return True
        return False

    def _guard_mutation(self) -> bool:
        """Run the security trio shared by POST and DELETE. Sends the error
        response and returns False when the request must be rejected."""
        if not self._check_host():
            self._json({"error": "forbidden: Host must be localhost"}, status=403)
            return False
        if not self._check_post_token():
            self._json({"error": "forbidden: missing or invalid X-Admin-Token"}, status=403)
            return False
        if not self._check_auth():
            self._json({"error": "unauthorized"}, status=401)
            return False
        return True

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        # Health endpoint (no auth — used by monitoring)
        if path == "/health" or path == "/healthz":
            self._serve_health()
            return

        # RichView pages bypass auth (accessible from Lark card links)
        if path.startswith("/view/"):
            view_id = path.split("/view/")[1].split("/")[0].split("?")[0]
            self._serve_richview(view_id)
            return

        if not self._check_host():
            self._json({"error": "forbidden: Host must be localhost"}, status=403)
            return

        if not self._check_auth():
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error":"unauthorized"}')
            return

        params = parse_qs(parsed.query)

        if path == "/api/memories":
            memories = []
            if MEMORY_DIR.is_dir():
                for f in sorted(MEMORY_DIR.rglob("*.md")):
                    if f.name == "MEMORY.md":
                        continue
                    m = parse_memory(f)
                    if m:
                        memories.append(m)
            self._json(memories)
        elif path == "/api/sessions_meta":
            self._json(sessions_meta())
        elif path.startswith("/api/session/"):
            sid = path.split("/api/session/")[1]
            self._json(load_full_session(sid))
        elif path == "/api/search":
            query = params.get("q", [""])[0]
            sid = params.get("session_id", [""])[0]
            self._json(search_sessions(query, sid))
        elif path == "/api/skills":
            self._json(load_skills())
        elif path == "/api/live":
            self._json(live_chat())
        elif path == "/api/bot_status":
            self._json(bot_status())
        elif path == "/api/lark_chats":
            self._json(lark_chats())
        elif path == "/api/settings":
            self._json(load_settings())
        elif path == "/api/heartbeat-timeline":
            self._json(heartbeat_timeline())
        elif path == "/api/heartbeat/status":
            self._json(heartbeat_status())
        elif path == "/api/eigenflux/status":
            self._json(eigenflux_status_cached())
        elif path == "/api/eigenflux/settings":
            self._json(eigenflux_settings())
        elif path == "/api/views":
            self._json(list_views())
        elif path == "/api/logs":
            file_key = params.get("file", ["jarvis"])[0]
            try:
                lines = int(params.get("lines", ["200"])[0])
            except ValueError:
                self._json({"error": "lines must be an integer"}, status=400)
                return
            grep = params.get("grep", [""])[0]
            result = tail_log(file_key, lines=lines, grep=grep)
            self._json(result, status=200 if "error" not in result else 400)
        elif path == "/api/sched_events":
            task = params.get("task", [""])[0]
            try:
                limit = int(params.get("limit", ["500"])[0])
            except ValueError:
                self._json({"error": "limit must be an integer"}, status=400)
                return
            try:
                self._json(sched_events_tail(task=task, limit=limit))
            except Exception as e:
                self._json({"error": str(e)}, status=500)
        elif path == "/api/queues":
            try:
                self._json(queues_overview())
            except Exception as e:
                self._json({"error": str(e)}, status=500)
        elif path == "/api/intents":
            status_q = params.get("status", [""])[0]
            try:
                self._json(intents_overview(status=status_q))
            except Exception as e:
                self._json({"error": str(e)}, status=500)
        elif path.startswith("/api/"):
            # Strict 404 for unknown API paths (REQ-48④) — serving the SPA
            # here made every typo'd endpoint look like a 200.
            self._json({"error": "not found"}, status=404)
        else:
            # SPA shell. The POST token is injected into a <meta> tag so the
            # frontend can send X-Admin-Token on every state-changing call.
            html_out = HTML
            token = _get_post_token()
            if token:
                html_out = html_out.replace("__ADMIN_TOKEN__", token)
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(html_out.encode())

    def _serve_health(self):
        """Health check endpoint for monitoring. No auth required."""
        import time as _t
        health = {"status": "ok", "timestamp": _t.strftime("%Y-%m-%dT%H:%M:%S")}
        try:
            # Check heartbeat state
            state_file = ROOT / "heartbeat_state.json"
            if state_file.exists():
                state = json.loads(state_file.read_text())
                latest_run = max((v.get("last_run", 0) for v in state.values()), default=0)
                age = int(_t.time()) - latest_run
                health["heartbeat_age_seconds"] = age
                health["heartbeat_stale"] = age > 1200
            # Check bot.sh PID
            pid_file = ROOT / ".bot.pid"
            if pid_file.exists():
                pid = int(pid_file.read_text().strip().split()[0])
                try:
                    os.kill(pid, 0)
                    health["bot_alive"] = True
                except (ProcessLookupError, PermissionError):
                    health["bot_alive"] = False
                    health["status"] = "degraded"
            # Check circuit breakers
            tripped = []
            if state_file.exists():
                for name, data in state.items():
                    circuit = data.get("circuit", {})
                    if circuit.get("disabled_until", 0) > _t.time():
                        tripped.append(name)
            if tripped:
                health["circuits_open"] = tripped
                health["status"] = "degraded"
            # PRIORITY wedge check (6/15 brain-death postmortem): heartbeat_age
            # above is max(last_run) across ALL tasks, so any healthy task masks
            # a wedged PRIORITY one. empty_pre runs refresh last_success even
            # when the Claude call fails (core/brain_health.py detector 2), so
            # staleness alone lies — it only counts when last_status is itself
            # a failure; consecutive_failures>=3 covers the refreshed case.
            if state_file.exists():
                from core.brain_health import (FAILURE_STATUSES,
                                               STARVATION_FACTOR, _interval_for)
                from core.heartbeat import HeartbeatRunner, parse_heartbeat
                try:
                    defaults = {t["name"]: t["interval"]
                                for t in parse_heartbeat(ROOT / "HEARTBEAT.md")}
                except OSError:
                    defaults = {}
                try:
                    overrides = json.loads(
                        (ROOT / "interval_overrides.json").read_text())
                except (OSError, ValueError):
                    overrides = {}
                wedged = []
                now = _t.time()
                for name in sorted(HeartbeatRunner.PRIORITY_TASKS):
                    ts = state.get(name) or {}
                    if not ts:
                        continue
                    consec = (ts.get("circuit") or {}).get(
                        "consecutive_failures", 0) or 0
                    interval = _interval_for(
                        name, ts, defaults.get(name, 0), overrides)
                    last_run = ts.get("last_run", 0) or 0
                    last_success = ts.get("last_success", 0) or last_run
                    success_stale = (last_success > 0 and
                                     now - last_success > STARVATION_FACTOR * interval)
                    failing = ts.get("last_status", "") in FAILURE_STATUSES
                    if consec >= 3 or (success_stale and failing):
                        wedged.append(name)
                if wedged:
                    health["priority_wedged"] = wedged
                    health["status"] = "degraded"
        except Exception as e:
            health["status"] = "error"
            health["error"] = str(e)
        body = json.dumps(health, ensure_ascii=False).encode()
        # "degraded" (an open circuit on some heartbeat task) means the admin
        # process is alive and serving — it is NOT a web-server outage. Return
        # 200 so liveness probes pass; reserve 5xx for genuine endpoint errors.
        # Body still carries circuits_open/priority_wedged for body-aware consumers.
        status = 503 if health["status"] == "error" else 200
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def _serve_richview(self, view_id: str):
        """Render a RichView page. No auth required."""
        view = get_view(view_id)
        if not view:
            self.send_response(404)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<h1>View not found or expired</h1>")
            return
        html = _render_richview(view)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(html.encode())

    def _json(self, data, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode())

    def _drain_body(self) -> bytes:
        """Read the request body exactly once (bounded) and cache it.

        Responding while unread bytes sit on the socket resets the connection
        before the client sees the status — so EVERY mutation handler drains
        first, even paths that reject the request or take no body.
        """
        cached = getattr(self, "_body_cache", None)
        if cached is not None:
            return cached
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            length = 0
        length = max(0, min(length, 10 * 1024 * 1024))  # 10MB cap
        try:
            self._body_cache = self.rfile.read(length) if length else b""
        except OSError:
            self._body_cache = b""
        return self._body_cache

    def _read_json_body(self) -> dict | None:
        """Parse the (already drained) JSON request body. Enforces
        Content-Type: application/json (REQ-48 trio ③) and sends the error
        response itself, returning None so the caller just bails."""
        raw = self._drain_body()
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            self._json({"error": "Content-Type must be application/json"}, status=415)
            return None
        try:
            body = json.loads(raw) if raw else {}
            if not isinstance(body, dict):
                self._json({"error": "JSON body must be an object"}, status=400)
                return None
            return body
        except (json.JSONDecodeError, ValueError):
            self._json({"error": "invalid JSON body"}, status=400)
            return None

    def do_POST(self):
        self._drain_body()
        if not self._guard_mutation():
            return

        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/memory":
            body = self._read_json_body()
            if body is None:
                return
            required = ("filename", "name", "description", "type", "body")
            missing = [k for k in required if k not in body]
            if missing:
                self._json({"error": f"missing fields: {', '.join(missing)}"}, status=400)
                return
            result = save_memory(
                filename=body["filename"],
                name=body["name"],
                description=body["description"],
                mem_type=body["type"],
                body=body["body"],
            )
            self._json(result, status=200 if "ok" in result else 400)

        elif path == "/api/heartbeat/task":
            body = self._read_json_body()
            if body is None:
                return
            name = body.get("name", "")
            interval_str = body.get("interval_str", "")
            pre = body.get("pre", "")
            post = body.get("post", "")
            prompt = body.get("prompt", "")
            if not name or not interval_str or not prompt:
                self._json({"error": "name, interval_str, and prompt are required"}, status=400)
                return
            result = save_heartbeat_task(name, interval_str, pre, post, prompt,
                                         expected_mtime=body.get("mtime"))
            if "ok" in result:
                status = 200
            elif result.get("conflict"):
                status = 409
            else:
                status = 400
            self._json(result, status=status)

        elif path.startswith("/api/heartbeat/force/"):
            from urllib.parse import unquote
            task_name = unquote(path.split("/api/heartbeat/force/", 1)[1]).strip()
            if not task_name:
                self._json({"error": "task name required"}, status=400)
                return
            # Validate against HEARTBEAT.md (REQ-47②): an unknown name would
            # be written, consumed by heartbeat_loop, and silently no-op.
            try:
                from core.heartbeat import parse_heartbeat
                known = {t["name"] for t in parse_heartbeat(ROOT / "HEARTBEAT.md")}
            except (OSError, FileNotFoundError):
                known = set()
            if task_name not in known:
                self._json({"error": f"unknown task {task_name!r}",
                            "known_tasks": sorted(known)}, status=404)
                return
            # Bare task name, no trailing newline — heartbeat_loop reads the
            # CONTENT and scopes the forced cycle to run_cycle(only_task=).
            try:
                HEARTBEAT_TRIGGER_PATH.write_text(task_name, encoding="utf-8")
                self._json({"ok": True, "task": task_name, "scope": "single-task"})
            except Exception as e:
                self._json({"error": str(e)}, status=500)

        elif path == "/api/eigenflux/settings":
            body = self._read_json_body()
            if body is None:
                return
            confirm_overwrite = bool(body.pop("confirm_overwrite", False))
            current = eigenflux_settings()
            if "error" in current:
                current = {}
            # REQ-47④: refuse to stomp a hand-tuned free-text preference with
            # one of the fixed template values unless explicitly confirmed —
            # this exact path silently destroyed the curated routing rules.
            new_pref = body.get("feed_delivery_preference")
            cur_pref = str(current.get("feed_delivery_preference", "") or "")
            if (new_pref is not None and new_pref in EF_TEMPLATE_PREFS
                    and cur_pref.strip() and new_pref != cur_pref
                    and not confirm_overwrite):
                self._json({
                    "error": "refusing to replace a non-empty hand-tuned "
                             "feed_delivery_preference with a template value "
                             "— resend with confirm_overwrite: true to proceed",
                    "needs_confirm": True,
                    "current_preference": cur_pref,
                }, status=409)
                return
            current.update(body)
            result = save_eigenflux_settings(current)
            self._json(result, status=200 if "ok" in result else 500)

        elif path == "/api/bot/stop_task":
            self._json(self._stop_task())

        elif path == "/api/bot/restart":
            # Write restart trigger; heartbeat_loop is the single consumer —
            # it spawns restart.sh detached (REQ-42, ~15s round trip).
            trigger = ROOT / ".restart_trigger"
            trigger.write_text(str(int(time.time())))
            self._json({"ok": True, "message": "Restart triggered — heartbeat "
                                               "loop hands off to restart.sh (~15s)"})

        elif path == "/api/intents/cancel":
            body = self._read_json_body()
            if body is None:
                return
            intent_id = str(body.get("id", "")).strip()
            if not intent_id:
                self._json({"error": "id required"}, status=400)
                return
            try:
                from core import intentions
                ok = intentions.cancel_intent(intent_id, reason="cancelled via admin console")
            except Exception as e:
                self._json({"error": str(e)}, status=500)
                return
            if ok:
                self._json({"ok": True, "id": intent_id})
            else:
                self._json({"error": "intent not found"}, status=404)

        elif path == "/api/intents/rearm":
            body = self._read_json_body()
            if body is None:
                return
            intent_id = str(body.get("id", "")).strip()
            if not intent_id:
                self._json({"error": "id required"}, status=400)
                return
            try:
                result = rearm_intent(intent_id)
            except Exception as e:
                self._json({"error": str(e)}, status=500)
                return
            self._json(result, status=200 if "ok" in result else 404)

        else:
            self._json({"error": "not found"}, status=404)

    @staticmethod
    def _pid_is_claude(pid: int) -> bool:
        """True only when `pid`'s command line names a claude/handler process.

        Guards stop_task's SIGKILL against a recycled pid: a stale lock can hold
        a number the OS has since handed to an unrelated process, and killing
        that would be a murder we never want. `ps -o command= -p <pid>` prints
        the full command of a live pid (empty/non-zero for a gone pid); we
        require 'claude' in it (the handler spawns `claude -p --resume/...`).
        Any failure (ps error/timeout/no output) → not ours → False.
        """
        try:
            out = subprocess.run(["ps", "-o", "command=", "-p", str(pid)],
                                 capture_output=True, text=True, timeout=3)
        except Exception:
            return False
        if out.returncode != 0:
            return False
        return "claude" in out.stdout

    def _stop_task(self) -> dict:
        """Kill running session handlers honestly (REQ-47①).

        Lock format is '<pid-or-acquiring> <token>' (two fields — bot.sh).
        The old code int()'d the whole line, ALWAYS raised, never killed
        anything, then unconditionally deleted live locks — enabling
        concurrent --resume transcript corruption while reporting ok:true.

        Rules: take the FIRST whitespace field; skip 'acquiring' (holder is
        mid-acquire, no pid yet); kill only numeric pids; unlink ONLY after
        the holder is confirmed dead (kill -0 → ProcessLookupError).

        Identity check (red-team fix): a numeric pid is no proof the lock is
        still ours. If the lock is stale and the OS recycled the pid, blindly
        SIGKILLing it murders an innocent process — strictly worse than the
        pre-change behaviour (which never killed). Before kill, verify the
        process command line contains 'claude' (the handler runs
        `claude -p --resume/--session-id`). If it does NOT match, the lock is
        stale: do NOT kill — just unlink it and count it as `cleaned`.
        """
        import glob as _glob
        killed = 0   # actually SIGKILLed and confirmed dead
        cleaned = 0  # holder dead OR pid recycled to a non-claude process — stale lock removed
        skipped = 0  # acquiring / malformed / could-not-confirm-dead
        for lock in _glob.glob(str(ROOT / ".session_lock_*")):
            lock_path = Path(lock)
            try:
                content = lock_path.read_text(encoding="utf-8").strip()
            except OSError:
                skipped += 1
                continue
            first = content.split()[0] if content.split() else ""
            if first == "acquiring" or not first.isdigit():
                skipped += 1  # no pid to kill — and the lock is NOT ours to delete
                continue
            pid = int(first)
            # Identity check: only a live claude/handler process is ours to kill.
            # A recycled pid running something else means the lock is stale —
            # treat it as a cleanup, never a kill.
            if not self._pid_is_claude(pid):
                lock_path.unlink(missing_ok=True)
                cleaned += 1
                continue
            already_dead = False
            try:
                os.kill(pid, 9)
            except ProcessLookupError:
                already_dead = True
            except OSError:
                skipped += 1  # e.g. PermissionError — not our process, leave the lock
                continue
            if already_dead:
                lock_path.unlink(missing_ok=True)
                cleaned += 1
                continue
            # SIGKILL is async — confirm death before touching the lock.
            dead = False
            for _ in range(20):  # up to ~2s
                if _pid_dead(pid):
                    dead = True
                    break
                time.sleep(0.1)
            if dead:
                lock_path.unlink(missing_ok=True)
                killed += 1
            else:
                skipped += 1  # holder still alive — lock stays
        return {"ok": True, "killed": killed, "cleaned": cleaned, "skipped": skipped}

    def do_DELETE(self):
        self._drain_body()
        if not self._guard_mutation():
            return

        parsed = urlparse(self.path)
        path = parsed.path

        if path.startswith("/api/memory/"):
            filename = path.split("/api/memory/", 1)[1]
            if not filename:
                self._json({"error": "filename required"}, status=400)
                return
            result = delete_memory(filename)
            self._json(result, status=200 if "ok" in result else 400)
        else:
            self._json({"error": "not found"}, status=404)

    def log_message(self, fmt, *args):
        pass


def main():
    auth_line = "  auth:       token required" if ADMIN_TOKEN else "  auth:       (none — 127.0.0.1 only recommended)"
    print(f"Jarvis Admin: http://{HOST}:{PORT}")
    print(f"  work_dir:   {WORK_DIR}")
    print(f"  memory_dir: {MEMORY_DIR}")
    print(f"  sessions:   {PROJECT_DIR}")
    print(auth_line)
    if HOST not in ("127.0.0.1", "localhost") and not ADMIN_TOKEN:
        print("  ⚠ WARNING: admin bound to non-localhost with no token — set admin.token in jarvis.yaml")
    # Materialize the POST token at startup (creates .admin_token, 0600).
    _get_post_token()
    try:
        from core.deploy import register_runtime
        register_runtime("admin")
    except Exception as exc:
        print(f"  runtime registration failed: {exc}")
    # Threading (REQ-48③): the single-threaded HTTPServer let one slow request
    # (e.g. an eigenflux subprocess) freeze the console AND Lark /view/ links.
    server = http.server.ThreadingHTTPServer((HOST, PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
