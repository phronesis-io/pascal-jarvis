#!/usr/bin/env python3
"""Jarvis Admin Console — memories, sessions, search, settings, skills.

Config-driven dashboard backed by jarvis.yaml. Run standalone:

    python3 admin.py

Or enable `admin.enabled: true` in jarvis.yaml (reserved for future auto-start).
"""

import http.server
import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# Make core/ importable
ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

from core import search as core_search
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

# Session tracker (maps Lark conv_key → current session_id + counter)
SESSION_TRACKER = ROOT / "active_sessions.json"

# Project dirs to search when looking up historical session files.
# Primary = PROJECT_DIR (derived from current work_dir).
# Also check legacy paths so history from before a work_dir change is visible.
SESSION_SEARCH_PATHS = [
    PROJECT_DIR,
    CLAUDE_DIR / "projects" / "-Users-pascal-Desktop-jarvis-repos-pascal-jarvis",
]

# ── Caches ───────────────────────────────────────────────────────────
# Admin is read-only and called on every page navigation; cache heavy
# session-meta scans by (path, newest-mtime) tuple.
_CACHE_TTL_SECONDS = 5
_sessions_meta_cache: dict = {"key": None, "data": [], "time": 0.0}
_lark_chats_cache: dict = {"key": None, "data": [], "time": 0.0}


def _dir_fingerprint(path: Path) -> tuple:
    """Cheap cache key: newest mtime + file count in a directory."""
    if not path.is_dir():
        return (0.0, 0)
    mtimes = [p.stat().st_mtime for p in path.glob("*.jsonl")]
    return (max(mtimes) if mtimes else 0.0, len(mtimes))

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Jarvis Admin</title>
<style>
:root {
  --bg: #09090b; --surface: #111113; --surface2: #18181b; --surface3: #1c1c1f;
  --border: #27272a; --border2: #3f3f46;
  --text: #e4e4e7; --dim: #71717a; --muted: #a1a1aa;
  --accent: #a78bfa; --accent2: #818cf8; --accent-dim: rgba(167,139,250,0.15);
  --user: #7dd3fc; --user-dim: rgba(125,211,252,0.1);
  --feedback: #fdba74; --project: #86efac; --reference: #d8b4fe;
  --highlight: #fbbf24; --highlight-bg: rgba(251,191,36,0.15);
  --danger: #f87171; --green: #4ade80; --red: #f87171; --yellow: #facc15;
}
* { margin:0; padding:0; box-sizing:border-box; }
body { font-family: -apple-system,'SF Pro Text','Inter',system-ui,sans-serif;
  background: var(--bg); color: var(--text); -webkit-font-smoothing: antialiased;
  height: 100vh; overflow: hidden; }

.app { display: flex; height: 100vh; }
.sidebar { width: 200px; min-width: 200px; background: var(--surface);
  border-right: 1px solid var(--border); padding: 20px 0; display: flex; flex-direction: column; }
.sidebar .logo { font-size: 12px; font-weight: 700; letter-spacing: 0.12em;
  text-transform: uppercase; color: var(--accent); font-family: 'SF Mono','Fira Code',monospace;
  padding: 0 20px 20px; border-bottom: 1px solid var(--border); }
.sidebar .nav-item { display: flex; align-items: center; gap: 8px; padding: 10px 20px;
  font-size: 13px; font-weight: 500; color: var(--dim); cursor: pointer;
  transition: all 0.12s; border: none; background: none; width: 100%;
  text-align: left; font-family: inherit; }
.sidebar .nav-item:hover { color: var(--muted); background: var(--surface2); }
.sidebar .nav-item.active { color: var(--text); background: var(--surface2); border-right: 2px solid var(--accent); }
.main { flex: 1; overflow-y: auto; padding: 32px 40px; }

.section-header { display: flex; align-items: baseline; justify-content: space-between; margin-bottom: 20px; }
.section-header h2 { font-size: 18px; font-weight: 600; }
.section-header .count { font-size: 12px; color: var(--dim); }
section { display: none; }
section.active { display: block; }

.card { background: var(--surface); border: 1px solid var(--border); border-radius: 10px;
  padding: 18px 22px; margin-bottom: 10px; transition: border-color 0.15s; }
.card:hover { border-color: var(--border2); }
.card-head { display: flex; align-items: center; gap: 10px; margin-bottom: 8px; }
.card-name { font-size: 13px; font-weight: 600; }
.badge { font-size: 10px; font-weight: 600; letter-spacing: 0.06em;
  text-transform: uppercase; padding: 2px 8px; border-radius: 4px; }
.badge-user { color: var(--user); background: #0c2d48; }
.badge-feedback { color: var(--feedback); background: #2e1d0a; }
.badge-project { color: var(--project); background: #0a2e1a; }
.badge-reference { color: var(--reference); background: #220a3e; }
.badge-unknown { color: var(--dim); background: var(--surface2); }
.card-desc { font-size: 11.5px; color: var(--dim); margin-bottom: 10px; font-style: italic; }
.card-body { font-size: 12.5px; line-height: 1.75; color: var(--muted);
  white-space: pre-wrap; word-wrap: break-word; }
.card-body strong { color: var(--accent); font-weight: 600; }
.card-meta { font-size: 10px; color: #333; margin-top: 10px; text-align: right; }
.card-actions { display: flex; gap: 6px; margin-left: auto; }
.filters { display: flex; gap: 6px; margin-bottom: 20px; flex-wrap: wrap; }
.fbtn { font-family: inherit; font-size: 11px; font-weight: 500; padding: 4px 12px;
  border-radius: 5px; border: 1px solid var(--border); background: transparent;
  color: var(--dim); cursor: pointer; transition: all 0.15s; }
.fbtn:hover, .fbtn.active { border-color: var(--accent); color: var(--accent); }

/* Buttons */
.btn { font-family: inherit; font-size: 11px; font-weight: 600; padding: 5px 14px;
  border-radius: 6px; border: 1px solid var(--border); cursor: pointer; transition: all 0.15s; }
.btn-primary { background: var(--accent); color: #000; border-color: var(--accent); }
.btn-primary:hover { opacity: 0.85; }
.btn-ghost { background: transparent; color: var(--muted); }
.btn-ghost:hover { border-color: var(--accent); color: var(--accent); }
.btn-danger { background: transparent; color: var(--danger); border-color: var(--danger); }
.btn-danger:hover { background: var(--danger); color: #000; }
.btn-sm { font-size: 10px; padding: 3px 10px; }

/* Form inputs */
.form-group { margin-bottom: 12px; }
.form-group label { display: block; font-size: 11px; font-weight: 600; color: var(--dim);
  margin-bottom: 4px; text-transform: uppercase; letter-spacing: 0.06em; }
.form-input { font-family: inherit; font-size: 13px; padding: 8px 12px; border-radius: 6px;
  border: 1px solid var(--border); background: var(--surface2); color: var(--text);
  outline: none; width: 100%; transition: border-color 0.15s; }
.form-input:focus { border-color: var(--accent); }
textarea.form-input { font-family: 'SF Mono','Fira Code',monospace; font-size: 12px;
  min-height: 200px; resize: vertical; line-height: 1.7; }
select.form-input { cursor: pointer; }

/* Editor card */
.editor-card { background: var(--surface2); border: 1px solid var(--accent);
  border-radius: 10px; padding: 20px; margin-bottom: 10px; }
.editor-actions { display: flex; gap: 8px; margin-top: 12px; }

/* Search bar */
.search-bar { display: flex; gap: 8px; margin-bottom: 20px; }
.search-bar input { flex: 1; font-family: inherit; font-size: 13px; padding: 10px 16px;
  border-radius: 8px; border: 1px solid var(--border); background: var(--surface);
  color: var(--text); outline: none; transition: border-color 0.15s; }
.search-bar input:focus { border-color: var(--accent); }
.search-bar input::placeholder { color: var(--dim); }
.search-bar select { font-family: inherit; font-size: 12px; padding: 8px 12px;
  border-radius: 8px; border: 1px solid var(--border); background: var(--surface);
  color: var(--muted); outline: none; cursor: pointer; }
.search-bar button { font-family: inherit; font-size: 13px; font-weight: 600;
  padding: 10px 24px; border-radius: 8px; border: none; background: var(--accent);
  color: #000; cursor: pointer; transition: opacity 0.15s; white-space: nowrap; }
.search-bar button:hover { opacity: 0.85; }

/* Search results */
.sr { background: var(--surface); border: 1px solid var(--border); border-radius: 10px;
  margin-bottom: 12px; overflow: hidden; }
.sr-header { display: flex; align-items: center; gap: 12px; padding: 12px 18px;
  font-size: 11px; color: var(--dim); border-bottom: 1px solid var(--border); background: var(--surface2); }
.sr-header .sr-sid { font-family: 'SF Mono',monospace; color: var(--accent2); cursor: pointer; }
.sr-header .sr-sid:hover { text-decoration: underline; }
.sr-body { padding: 14px 18px; }
.sr-ctx { font-size: 12px; line-height: 1.65; color: #4a4a52; padding: 4px 0 4px 14px;
  border-left: 2px solid var(--border); margin-bottom: 4px; }
.sr-match-wrap { background: var(--highlight-bg); border-radius: 6px; padding: 10px 14px; margin: 8px 0; }
.sr-match { font-size: 12.5px; line-height: 1.75; color: var(--text); white-space: pre-wrap; word-wrap: break-word; }
.sr-match mark { background: rgba(251,191,36,0.35); color: var(--highlight); padding: 1px 3px; border-radius: 2px; }
.sr-role { font-size: 9px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.08em;
  display: inline-block; padding: 1px 5px; border-radius: 3px; }
.sr-role.user { color: var(--user); background: var(--user-dim); }
.sr-role.assistant { color: var(--accent); background: var(--accent-dim); }

/* Sessions */
.sessions-layout { display: flex; gap: 0; height: calc(100vh - 120px); }
.session-list { width: 320px; min-width: 280px; overflow-y: auto; border-right: 1px solid var(--border); }
.session-viewer { flex: 1; overflow-y: auto; padding-left: 24px; }
.sl-item { padding: 12px 16px; cursor: pointer; border-bottom: 1px solid var(--border); transition: all 0.12s; }
.sl-item:hover { background: var(--surface); }
.sl-item.active { background: var(--surface2); border-left: 3px solid var(--accent); }
.sl-item .sl-date { font-size: 10px; color: var(--dim); margin-bottom: 2px; }
.sl-item .sl-prompt { font-size: 12px; color: var(--muted); line-height: 1.4; }
.sl-item .sl-meta { font-size: 10px; color: var(--dim); margin-top: 4px; }
.sl-item .sl-id { font-family: 'SF Mono',monospace; font-size: 10px; color: var(--accent2); }
.sv-header { display: flex; align-items: center; justify-content: space-between;
  padding-bottom: 16px; border-bottom: 1px solid var(--border); margin-bottom: 16px; }
.sv-header h3 { font-size: 14px; font-weight: 600; }
.sv-search { display: flex; gap: 6px; }
.sv-search input { font-family: inherit; font-size: 12px; padding: 6px 12px; border-radius: 6px;
  border: 1px solid var(--border); background: var(--surface); color: var(--text); outline: none; width: 200px; }
.sv-search input:focus { border-color: var(--accent); }
.sv-messages { padding-bottom: 40px; }
.sv-msg { padding: 12px 0; border-bottom: 1px solid #1a1a1d; }
.sv-msg:last-child { border-bottom: none; }
.sv-msg-head { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; }
.sv-msg-ts { font-size: 10px; color: var(--dim); }
.sv-msg-text { font-size: 13px; line-height: 1.8; color: var(--muted); white-space: pre-wrap; word-wrap: break-word; }
.sv-msg-text mark { background: rgba(251,191,36,0.35); color: var(--highlight); padding: 1px 3px; border-radius: 2px; }
/* Session viewer: tool blocks */
.sv-msg.sv-tool { border-bottom: none; padding: 6px 0 2px 0; }
.sv-msg.sv-tool .sv-msg-text { color: #c4b5fd; font-family: 'SF Mono','Fira Code',monospace;
  font-size: 12px; line-height: 1.5; background: rgba(139,92,246,0.06);
  border-left: 3px solid #8b5cf6; padding: 6px 12px; border-radius: 4px; }
.sv-msg.sv-tool .sv-msg-text pre { background: rgba(0,0,0,0.3); border: none;
  padding: 6px 10px; margin: 4px 0 2px 0; font-size: 11px; color: #e2e8f0;
  border-radius: 4px; overflow-x: auto; }
.sv-msg.sv-tool .sv-msg-text code { background: rgba(0,0,0,0.2); color: #e2e8f0;
  padding: 1px 4px; border-radius: 3px; font-size: 11px; }
.sv-msg.sv-tool .tool-name { color: #a78bfa; font-weight: 600; font-size: 11px;
  text-transform: uppercase; letter-spacing: 0.5px; }
.sv-msg.sv-tool-result { border-bottom: none; padding: 0 0 4px 0; }
.sv-msg.sv-tool-result .sv-msg-text { color: #9ca3af; font-family: 'SF Mono','Fira Code',monospace;
  font-size: 11px; line-height: 1.4; background: rgba(0,0,0,0.15);
  border-left: 3px solid #4b5563; padding: 4px 12px; border-radius: 0 0 4px 4px;
  max-height: 120px; overflow-y: auto; }
.sv-empty { text-align: center; padding: 80px 0; color: var(--dim); font-size: 13px; }

/* Lark chats */
.chat-group { margin-bottom: 36px; }
.chat-head { display: flex; align-items: center; gap: 10px; padding-bottom: 10px;
  margin-bottom: 12px; border-bottom: 1px solid var(--border); }
.chat-head .chat-kind { font-size: 9px; font-weight: 700; text-transform: uppercase;
  letter-spacing: 0.08em; padding: 2px 7px; border-radius: 4px; }
.chat-head .chat-kind.p2p { color: var(--user); background: var(--user-dim); }
.chat-head .chat-kind.group { color: var(--feedback); background: #2e1d0a; }
.chat-head .chat-key { font-family: 'SF Mono','Fira Code',monospace; font-size: 11px; color: var(--muted); }
.chat-head .chat-stats { margin-left: auto; font-size: 11px; color: var(--dim); }
.chat-timeline { display: flex; flex-direction: column; gap: 6px; }
.ct-item { display: grid; grid-template-columns: 28px 120px 100px 1fr auto; align-items: center;
  gap: 12px; padding: 10px 14px; border-radius: 8px; background: var(--surface);
  border: 1px solid var(--border); font-size: 12px; cursor: pointer;
  transition: border-color 0.12s, background 0.12s; }
.ct-item:hover { border-color: var(--border2); background: var(--surface2); }
.ct-item.active { border-color: var(--accent); background: var(--accent-dim); }
.ct-item.missing { opacity: 0.45; cursor: default; }
.ct-counter { font-family: 'SF Mono',monospace; font-size: 11px; color: var(--dim); text-align: right; }
.ct-counter.current { color: var(--accent); font-weight: 700; }
.ct-sid { font-family: 'SF Mono',monospace; font-size: 11px; color: var(--accent2); }
.ct-date { font-size: 11px; color: var(--muted); }
.ct-preview { color: var(--dim); font-size: 11.5px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.ct-meta { font-size: 10px; color: var(--dim); white-space: nowrap; }
.ct-badge-active { font-size: 9px; font-weight: 700; text-transform: uppercase;
  padding: 2px 7px; border-radius: 3px; color: #000; background: var(--accent); }

/* Heartbeat */
.hb-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 12px; }
.hb-card { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 18px; }
.hb-card-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 12px; }
.hb-card-name { font-size: 14px; font-weight: 600; }
.hb-row { display: flex; justify-content: space-between; font-size: 12px; padding: 4px 0; }
.hb-label { color: var(--dim); }
.hb-value { color: var(--muted); font-family: 'SF Mono',monospace; }
.status-dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 6px; }
.dot-green { background: var(--green); }
.dot-yellow { background: var(--yellow); }
.dot-gray { background: var(--dim); }

/* EigenFlux */
.ef-status-box { background: var(--surface); border: 1px solid var(--border); border-radius: 10px;
  padding: 20px; margin-bottom: 20px; }
.ef-form { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 20px; }
.radio-group { display: flex; flex-direction: column; gap: 8px; margin-top: 4px; }
.radio-group label { font-size: 13px; color: var(--muted); cursor: pointer;
  display: flex; align-items: center; gap: 8px; font-weight: 400; text-transform: none; letter-spacing: 0; }
.radio-group input[type="radio"] { accent-color: var(--accent); }

/* Heartbeat Timeline */
.hbt-entry { background: var(--surface); border: 1px solid var(--border); border-radius: 10px;
  padding: 14px 18px; margin-bottom: 8px; transition: border-color 0.15s; }
.hbt-entry:hover { border-color: var(--border2); }
.hbt-head { display: flex; align-items: center; gap: 10px; margin-bottom: 6px; }
.hbt-ts { font-size: 11px; color: var(--dim); font-family: 'SF Mono','Fira Code',monospace; }
.hbt-badge { font-size: 10px; font-weight: 600; letter-spacing: 0.06em;
  text-transform: uppercase; padding: 2px 8px; border-radius: 4px; }
.hbt-badge-checkin { color: #4ade80; background: #0a2e1a; }
.hbt-badge-content-recommend { color: #fdba74; background: #2e1d0a; }
.hbt-badge-calendar-sync { color: #7dd3fc; background: #0c2d48; }
.hbt-badge-eigenflux { color: #d8b4fe; background: #220a3e; }
.hbt-badge-eigenflux-stream { color: #d8b4fe; background: #220a3e; }
.hbt-badge-other { color: var(--dim); background: var(--surface2); }
.hbt-text { font-size: 12.5px; line-height: 1.75; color: var(--muted);
  white-space: pre-wrap; word-wrap: break-word; }
.hbt-tabs { display: flex; gap: 6px; margin-bottom: 16px; }
.hbt-tab { font-family: inherit; font-size: 12px; font-weight: 600; padding: 6px 16px;
  border-radius: 6px; border: 1px solid var(--border); background: transparent;
  color: var(--dim); cursor: pointer; transition: all 0.15s; }
.hbt-tab:hover, .hbt-tab.active { border-color: var(--accent); color: var(--accent); }

/* Toast */
.toast-container { position: fixed; bottom: 24px; right: 24px; z-index: 9999; display: flex; flex-direction: column; gap: 8px; }
.toast { background: var(--surface2); border: 1px solid var(--border2); border-radius: 8px;
  padding: 10px 18px; font-size: 12px; color: var(--text); animation: toast-in 0.2s ease-out; }
@keyframes toast-in { from { opacity: 0; transform: translateY(10px); } to { opacity: 1; transform: translateY(0); } }

/* Live chat */
.live-status { display: flex; align-items: center; gap: 16px; padding: 12px 18px;
  background: var(--surface); border: 1px solid var(--border); border-radius: 10px;
  margin-bottom: 16px; font-size: 12px; position: sticky; top: 0; z-index: 10; }
.live-status .status-label { font-weight: 600; display: flex; align-items: center; gap: 6px; }
.live-status .status-label .status-dot { width: 10px; height: 10px; }
.live-status .status-detail { color: var(--dim); margin-left: auto; }
.state-working .status-dot { background: var(--green); animation: live-pulse 1s infinite; }
.state-idle .status-dot { background: var(--yellow); }
.state-dead .status-dot { background: var(--red); }
.state-working .status-text { color: var(--green); }
.state-idle .status-text { color: var(--yellow); }
.state-dead .status-text { color: var(--red); }
.live-header { display: flex; align-items: center; gap: 12px; padding-bottom: 14px;
  margin-bottom: 14px; border-bottom: 1px solid var(--border); }
.live-header h2 { font-size: 18px; font-weight: 600; }
.live-meta { font-size: 11px; color: var(--dim); margin-left: auto; }
.live-messages { display: flex; flex-direction: column; gap: 6px; padding-bottom: 40px; }
.live-msg { padding: 14px 18px; border-radius: 10px; max-width: 85%; font-size: 13px;
  line-height: 1.7; word-wrap: break-word; }
.live-msg p { margin: 0 0 0.4em 0; } .live-msg p:last-child { margin-bottom: 0; }
.live-msg.user { align-self: flex-end; background: #0c3a5c; color: #e0f2fe;
  border-bottom-right-radius: 2px; }
.live-msg.assistant { align-self: flex-start; background: var(--surface2);
  border: 1px solid var(--border2); color: var(--text); border-bottom-left-radius: 2px; }
.live-msg .live-ts { font-size: 9px; color: var(--muted); margin-bottom: 4px; display: block; }
.live-msg pre { background: #0d1117; border: 1px solid var(--border2); border-radius: 6px;
  padding: 8px 12px; overflow-x: auto; font-size: 11px; line-height: 1.5;
  margin: 4px 0; font-family: 'SF Mono','Fira Code',monospace; }
.live-msg code { font-family: 'SF Mono','Fira Code',monospace; font-size: 11px;
  background: rgba(255,255,255,0.06); padding: 1px 4px; border-radius: 3px; }
/* Tool use: distinct left-bordered block, not a chat bubble */
.live-msg.tool { align-self: stretch; max-width: 100%; padding: 6px 12px 6px 14px;
  border-radius: 4px; background: rgba(139,92,246,0.06);
  border-left: 3px solid #8b5cf6; font-size: 12px; line-height: 1.5;
  font-family: 'SF Mono','Fira Code',monospace; color: #c4b5fd; }
.live-msg.tool .tool-name { color: #a78bfa; font-weight: 600; font-size: 11px;
  text-transform: uppercase; letter-spacing: 0.5px; }
.live-msg.tool pre { background: rgba(0,0,0,0.3); border: none; padding: 6px 10px;
  margin: 4px 0 2px 0; font-size: 11px; color: #e2e8f0; }
.live-msg.tool code { background: rgba(0,0,0,0.2); color: #e2e8f0; }
/* Tool result: indented, muted, collapsible */
.live-msg.tool_result { align-self: stretch; max-width: 100%; padding: 4px 12px 4px 20px;
  border-radius: 0 0 4px 4px; background: rgba(0,0,0,0.15);
  border-left: 3px solid #4b5563; margin-top: -4px;
  font-size: 11px; line-height: 1.4; color: #9ca3af;
  font-family: 'SF Mono','Fira Code',monospace;
  max-height: 120px; overflow-y: auto; white-space: pre-wrap; }

.empty-state { text-align: center; padding: 60px 0; color: var(--dim); font-size: 13px; }
::-webkit-scrollbar { width: 5px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: #27272a; border-radius: 3px; }
::-webkit-scrollbar-thumb:hover { background: #3f3f46; }
</style>
</head>
<body>
<div class="app">
<div class="sidebar">
  <div class="logo">Jarvis</div>
  <button class="nav-item active" data-tab="live">Live</button>
  <button class="nav-item" data-tab="lark">Lark</button>
  <button class="nav-item" data-tab="memories">Memories</button>
  <button class="nav-item" data-tab="heartbeat">Heartbeat</button>
  <button class="nav-item" data-tab="eigenflux">EigenFlux</button>
  <button class="nav-item" data-tab="search">Search</button>
  <button class="nav-item" data-tab="sessions">Sessions</button>
  <button class="nav-item" data-tab="settings">Settings</button>
</div>
<div class="main">
<section id="live" class="active"></section>
<section id="lark"></section>
<section id="memories"></section>
<section id="heartbeat"></section>
<section id="eigenflux"></section>
<section id="search"></section>
<section id="sessions"></section>
<section id="settings"></section>
</div>
</div>
<div class="toast-container" id="toasts"></div>
<script>
// ── Helpers ──
const esc = s => (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
const bold = s => esc(s).replace(/\*\*(.*?)\*\*/g,'<strong>$1</strong>');
function hl(text,q) {
  if(!q) return esc(text);
  return esc(text).replace(new RegExp('('+q.replace(/[.*+?^${}()|[\]\\]/g,'\\$&')+')','gi'),'<mark>$1</mark>');
}
function fmtParagraphs(s) {
  if(!s) return '';
  // First: extract fenced code blocks (```...```) and render as <pre>
  const codeRe = /```(\w*)\n([\s\S]*?)```/g;
  const tokens = []; let last = 0, m;
  while((m = codeRe.exec(s)) !== null) {
    if(m.index > last) tokens.push({type:'text', val:s.slice(last,m.index)});
    tokens.push({type:'code', lang:m[1], val:m[2]});
    last = m.index + m[0].length;
  }
  if(last < s.length) tokens.push({type:'text', val:s.slice(last)});

  return tokens.map(tok => {
    if(tok.type === 'code') return '<pre>' + esc(tok.val.trim()) + '</pre>';
    // Text block: split by double newlines into paragraphs
    return tok.val.split(/\n{2,}/).map(p => {
      const lines = p.split('\n').map(l => {
        if(/^\[.+\]$/.test(l.trim())) return '<span class="live-tool">'+esc(l.trim())+'</span>';
        if(/^⟶/.test(l.trim())) return '<span style="color:var(--dim);font-size:11px">'+esc(l)+'</span>';
        // Inline backticks
        let h = bold(l);
        h = h.replace(/`([^`]+)`/g, '<code>$1</code>');
        return h;
      });
      return '<p>' + lines.join('<br>') + '</p>';
    }).join('');
  }).join('');
}
function roleTag(r) { return `<span class="sr-role ${r}">${r}</span>`; }
function fmtBytes(n) { if(!n)return '-'; if(n<1024)return n+'B'; if(n<1048576)return (n/1024).toFixed(1)+'KB'; return (n/1048576).toFixed(2)+'MB'; }
function fmtTs(ts) { return (ts||'').slice(0,16).replace('T',' ')||'-'; }
function toast(msg) {
  const c=document.getElementById('toasts'), d=document.createElement('div');
  d.className='toast'; d.textContent=msg; c.appendChild(d);
  setTimeout(()=>d.remove(), 3000);
}
function timeAgo(ts) {
  if(!ts) return 'never';
  const s=Math.floor(Date.now()/1000)-ts;
  if(s<60) return s+'s ago'; if(s<3600) return Math.floor(s/60)+'m ago';
  if(s<86400) return Math.floor(s/3600)+'h ago'; return Math.floor(s/86400)+'d ago';
}
function fmtCountdown(sec) {
  if(sec<=0) return 'due now';
  if(sec<60) return sec+'s'; if(sec<3600) return Math.floor(sec/60)+'m '+sec%60+'s';
  return Math.floor(sec/3600)+'h '+Math.floor((sec%3600)/60)+'m';
}
function fmtInterval(sec) {
  if(sec<60) return sec+'s'; if(sec<3600) return (sec/60)+'m';
  if(sec<86400) return (sec/3600)+'h'; return (sec/86400)+'d';
}

// ── Nav ──
document.querySelectorAll('.nav-item').forEach(t=>{
  t.addEventListener('click',()=>{
    document.querySelectorAll('.nav-item').forEach(b=>b.classList.remove('active'));
    document.querySelectorAll('section').forEach(s=>s.classList.remove('active'));
    t.classList.add('active'); document.getElementById(t.dataset.tab).classList.add('active');
  });
});

// ── Live ──
let _liveCount=0, _liveAutoScroll=true;
function fmtAgo(sec){
  if(sec==null) return '?';
  if(sec<60) return sec+'s ago'; if(sec<3600) return Math.floor(sec/60)+'m ago';
  if(sec<86400) return Math.floor(sec/3600)+'h ago'; return Math.floor(sec/86400)+'d ago';
}
const stateLabels={working:'Working',idle:'Idle',dead:'Offline'};
function botAction(action){
  if(action==='restart'&&!confirm('Restart bot?')) return;
  fetch('/api/bot/'+action,{method:'POST'}).then(r=>r.json()).then(d=>{
    toast(d.message||action+' done'); loadBotStatus();
  }).catch(()=>toast('Failed'));
}
function loadBotStatus(){
  fetch('/api/bot_status').then(r=>r.json()).then(s=>{
    const el=document.getElementById('live-status');
    if(!el) return;
    el.className='live-status state-'+s.state;
    el.innerHTML=`
      <span class="status-label"><span class="status-dot"></span>
        <span class="status-text">${stateLabels[s.state]||s.state}</span></span>
      <span style="color:var(--muted);font-size:11px">Last: ${esc(s.last_log_ts||'never')} (${fmtAgo(s.last_activity_ago)})</span>
      ${s.active_locks?'<span style="color:var(--muted);font-size:11px">'+s.active_locks+' task(s) running</span>':''}
      <span style="margin-left:auto;display:flex;gap:6px">
        ${s.active_locks?'<button class="btn btn-danger btn-sm" onclick="botAction(\'stop_task\')">Stop Task</button>':''}
        <button class="btn btn-ghost btn-sm" onclick="botAction(\'restart\')">Restart</button>
      </span>`;
  }).catch(()=>{});
}
function loadLive(){
  loadBotStatus();
  fetch('/api/live').then(r=>r.json()).then(data=>{
    const el=document.getElementById('live');
    if(!data.session_id){
      el.innerHTML='<div id="live-status" class="live-status"></div><div class="empty-state">No active conversations</div>';
      loadBotStatus();
      return;
    }
    const msgs=data.messages;
    // Only re-render messages if count changed
    if(msgs.length===_liveCount) return;
    _liveCount=msgs.length;
    const sid=data.session_id.slice(0,8);
    const key=data.conv_key;
    el.innerHTML=`
      <div id="live-status" class="live-status"></div>
      <div class="live-header">
        <h2>Live Chat</h2>
        <span class="live-meta">${esc(key)} &middot; ${sid} &middot; ${msgs.length} msgs</span>
      </div>
      <div class="live-messages" id="live-msgs">
        ${msgs.map(m => {
          if(m.role === 'tool') {
            return `<div class="live-msg tool">
              <span class="tool-name">${esc(m.tool_name||'tool')}</span>
              ${fmtParagraphs(m.text)}</div>`;
          }
          if(m.role === 'tool_result') {
            return `<div class="live-msg tool_result">${esc(m.text)}</div>`;
          }
          return `<div class="live-msg ${m.role}">
            <span class="live-ts">${esc(m.timestamp)}</span>${fmtParagraphs(m.text)}</div>`;
        }).join('')}
      </div>`;
    loadBotStatus();
    if(_liveAutoScroll){
      const main=document.querySelector('.main');
      main.scrollTop=main.scrollHeight;
    }
  }).catch(()=>{});
}
loadLive();
setInterval(()=>{
  if(document.getElementById('live').classList.contains('active')) loadLive();
}, 3000);

// ── Lark ──
fetch('/api/lark_chats').then(r=>r.json()).then(data=>{
  const el=document.getElementById('lark');
  if(!data.length){el.innerHTML='<div class="empty-state">No Lark conversations tracked yet</div>';return;}
  const total=data.length, active=data.reduce((a,c)=>a+(c.active_session_id?1:0),0);
  el.innerHTML=`
    <div class="section-header"><h2>Lark Conversations</h2>
      <span class="count">${total} chat${total>1?'s':''} &middot; ${active} active</span></div>
    ${data.map(chat=>`<div class="chat-group"><div class="chat-head">
        <span class="chat-kind ${chat.kind}">${chat.kind}</span>
        <span class="chat-key">${esc(chat.conv_key)}</span>
        <span class="chat-stats">${chat.current_counter} rotation${chat.current_counter>1?'s':''} &middot; ${chat.total_messages} msgs</span>
      </div><div class="chat-timeline">
        ${chat.sessions.map(s=>`<div class="ct-item ${s.is_active?'active':''} ${s.exists?'':'missing'}"
             ${s.exists?`onclick="openSession('${s.session_id}')"`:''}>
            <span class="ct-counter ${s.is_active?'current':''}">#${s.counter}</span>
            <span class="ct-sid">${esc(s.session_id.slice(0,8))}</span>
            <span class="ct-date">${esc(fmtTs(s.first_ts))}</span>
            <span class="ct-preview">${s.exists?esc(s.first_prompt||'(no user messages)'):'<em>file missing</em>'}</span>
            <span class="ct-meta">${s.is_active?'<span class="ct-badge-active">current</span> ':''}${s.exists?s.message_count+' msgs &middot; '+fmtBytes(s.size_bytes):''}</span>
          </div>`).join('')}
      </div></div>`).join('')}`;
});

// ── Memories (editable) ──
let _memData=[], _memFilter=null, _memEditing=null;
function loadMemories(){
  fetch('/api/memories').then(r=>r.json()).then(data=>{ _memData=data; renderMemories(); });
}
function _memTier(m){ const p=m.file||''; if(p.startsWith('hot/')) return 'hot'; if(p.startsWith('warm/')) return 'warm'; if(p.startsWith('system/')) return 'system'; if(p.startsWith('timeline/')) return 'timeline'; return 'other'; }
const _tierMeta={hot:{icon:'\u{1f525}',label:'Hot — 每次加载',color:'#ef4444'},warm:{icon:'\u{1f4e6}',label:'Warm — 按需加载',color:'#f59e0b'},system:{icon:'\u2699\ufe0f',label:'System — 运行状态',color:'#6366f1'},timeline:{icon:'\u{1f4c5}',label:'Timeline — 时间线日志',color:'#06b6d4'},other:{icon:'\u{1f4c4}',label:'Other',color:'#9ca3af'}};
function renderMemories(){
  const el=document.getElementById('memories');
  const types=[...new Set(_memData.map(m=>m.type))];
  const tiers=['hot','warm','system','timeline','other'];
  const filterOpts=[{k:'all',label:'all'},...types.map(t=>({k:'type:'+t,label:t})),...tiers.filter(t=>_memData.some(m=>_memTier(m)===t)).map(t=>({k:'tier:'+t,label:_tierMeta[t].icon+' '+t}))];
  const f=_memData.filter(m=>{
    if(!_memFilter) return true;
    if(_memFilter.startsWith('type:')) return m.type===_memFilter.slice(5);
    if(_memFilter.startsWith('tier:')) return _memTier(m)===_memFilter.slice(5);
    return true;
  });
  const grouped={};
  f.forEach(m=>{ const t=_memTier(m); (grouped[t]=grouped[t]||[]).push(m); });
  let cardsHtml='';
  for(const t of tiers){
    const items=grouped[t]; if(!items) continue;
    const tm=_tierMeta[t];
    cardsHtml+=`<div class="tier-group" style="margin-top:20px">
      <div style="font-size:13px;font-weight:600;color:${tm.color};margin-bottom:8px;display:flex;align-items:center;gap:6px">
        <span>${tm.icon}</span>${tm.label}<span class="count" style="font-size:11px">${items.length}</span></div>
      ${items.map(m=>memCardHtml(m)).join('')}
    </div>`;
  }
  el.innerHTML=`
    <div class="section-header"><h2>Memories</h2>
      <div style="display:flex;align-items:center;gap:12px">
        <span class="count">${f.length}</span>
        <button class="btn btn-primary" onclick="editMemory(null)">+ New Memory</button>
      </div></div>
    <div class="filters">${filterOpts.map(o=>`<button class="fbtn${(o.k==='all'&&!_memFilter)||(o.k===_memFilter)?' active':''}" data-t="${o.k}">${o.label}</button>`).join('')}</div>
    <div id="mem-editor-slot"></div>
    <div id="mem-cards">${cardsHtml}</div>`;
  el.querySelectorAll('.fbtn').forEach(b=>b.addEventListener('click',()=>{
    _memFilter=b.dataset.t==='all'?null:b.dataset.t; renderMemories();
  }));
  if(_memEditing) showEditor(_memEditing);
}
function memCardHtml(m){
  const tier=_memTier(m), tm=_tierMeta[tier];
  return `<div class="card" id="mem-${m.file}">
    <div class="card-head">
      <span class="card-name">${esc(m.name)}</span><span class="badge badge-${m.type}">${m.type}</span><span class="badge" style="background:${tm.color}22;color:${tm.color};font-size:10px">${tm.icon} ${tier}</span>
      <div class="card-actions">
        <button class="btn btn-ghost btn-sm" onclick="editMemory('${esc(m.file)}')">Edit</button>
        <button class="btn btn-danger btn-sm" onclick="deleteMemory('${esc(m.file)}')">Delete</button>
      </div>
    </div>
    ${m.description?`<div class="card-desc">${esc(m.description)}</div>`:''}
    <div class="card-body">${bold(m.body)}</div>
    <div class="card-meta">${esc(m.file)}</div>
  </div>`;
}
function editMemory(file){
  const m = file ? _memData.find(x=>x.file===file) : {name:'',description:'',type:'user',body:'',file:''};
  if(!m) return;
  _memEditing={...m, isNew:!file};
  showEditor(_memEditing);
}
function showEditor(m){
  const slot=document.getElementById('mem-editor-slot');
  if(!slot) return;
  slot.innerHTML=`<div class="editor-card">
    <div style="font-size:14px;font-weight:600;margin-bottom:14px">${m.isNew?'New Memory':'Edit: '+esc(m.name)}</div>
    <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px">
      <div class="form-group"><label>Filename</label>
        <input class="form-input" id="ed-file" value="${esc(m.file)}" ${m.isNew?'':'readonly style="opacity:0.6"'} placeholder="my_note.md"></div>
      <div class="form-group"><label>Name</label>
        <input class="form-input" id="ed-name" value="${esc(m.name)}" placeholder="Display Name"></div>
      <div class="form-group"><label>Type</label>
        <select class="form-input" id="ed-type">
          ${['user','feedback','project','reference'].map(t=>`<option ${t===m.type?'selected':''}>${t}</option>`).join('')}
        </select></div>
    </div>
    <div class="form-group"><label>Description</label>
      <input class="form-input" id="ed-desc" value="${esc(m.description)}" placeholder="Short description"></div>
    <div class="form-group"><label>Body</label>
      <textarea class="form-input" id="ed-body" placeholder="Markdown content...">${esc(m.body)}</textarea></div>
    <div class="editor-actions">
      <button class="btn btn-primary" id="ed-save" onclick="saveMemory()">Save</button>
      <button class="btn btn-ghost" onclick="cancelEdit()">Cancel</button>
    </div>
  </div>`;
}
function cancelEdit(){ _memEditing=null; const s=document.getElementById('mem-editor-slot'); if(s) s.innerHTML=''; }
function saveMemory(){
  const btn=document.getElementById('ed-save');
  const payload={
    filename: document.getElementById('ed-file').value.trim(),
    name: document.getElementById('ed-name').value.trim(),
    description: document.getElementById('ed-desc').value.trim(),
    type: document.getElementById('ed-type').value,
    body: document.getElementById('ed-body').value
  };
  if(!payload.filename||!payload.name){toast('Filename and name are required');return;}
  if(!payload.filename.endsWith('.md')) payload.filename+='.md';
  btn.textContent='Saving...'; btn.disabled=true;
  fetch('/api/memory',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)})
    .then(r=>r.json()).then(res=>{
      if(res.ok){toast('Memory saved'); _memEditing=null; loadMemories();}
      else{toast('Error: '+(res.error||'unknown')); btn.textContent='Save'; btn.disabled=false;}
    }).catch(e=>{toast('Network error'); btn.textContent='Save'; btn.disabled=false;});
}
function deleteMemory(file){
  if(!confirm('Delete '+file+'?')) return;
  fetch('/api/memory/'+encodeURIComponent(file),{method:'DELETE'})
    .then(r=>r.json()).then(res=>{
      if(res.ok){toast('Deleted '+file); loadMemories();}
      else toast('Error: '+(res.error||'unknown'));
    });
}
loadMemories();

// ── Heartbeat ──
let _hbTasks=[];
function loadHeartbeat(){
  const el=document.getElementById('heartbeat');
  el.innerHTML='<div class="section-header"><h2>Heartbeat</h2></div><div class="empty-state">Loading...</div>';
  fetch('/api/heartbeat/status').then(r=>r.json()).then(data=>{
    if(data.error){el.innerHTML=`<div class="section-header"><h2>Heartbeat</h2></div><div class="empty-state">${esc(data.error)}</div>`;return;}
    _hbTasks=data.tasks||[];
    renderHeartbeat();
  });
}
function renderHeartbeat(){
  const el=document.getElementById('heartbeat');
  const tasks=_hbTasks;
  el.innerHTML=`<div class="section-header"><h2>Heartbeat</h2><span class="count">${tasks.length} tasks</span></div>
    <div style="margin-bottom:24px">
      <div style="font-size:15px;font-weight:600;margin-bottom:12px;color:var(--accent)">Output Timeline</div>
      <div id="hbt-panel"><div class="empty-state">Loading timeline...</div></div>
    </div>
    <div style="font-size:15px;font-weight:600;margin-bottom:12px;color:var(--accent)">Task Status</div>
    <div style="display:flex;flex-direction:column;gap:10px">${tasks.map((t,i)=>{
      const overdue=t.last_run && t.seconds_until_due<=0;
      const never=!t.last_run;
      const dotCls=never?'dot-gray':overdue?'dot-yellow':'dot-green';
      const statusTxt=never?'never run':overdue?'overdue':'healthy';
      return `<div class="card" id="hb-card-${i}">
        <div class="hb-card-head">
          <span class="hb-card-name"><span class="status-dot ${dotCls}"></span>${esc(t.name)}</span>
          <span style="display:flex;gap:6px">
            <button class="btn btn-ghost btn-sm" onclick="toggleHbEdit(${i})">Edit</button>
            <button class="btn btn-primary btn-sm" onclick="forceRun('${esc(t.name)}',this)">Run Now</button>
          </span>
        </div>
        <div class="hb-row"><span class="hb-label">Interval</span><span class="hb-value">${t.interval_str||fmtInterval(t.interval)}</span></div>
        <div class="hb-row"><span class="hb-label">Last run</span><span class="hb-value">${t.last_run?timeAgo(t.last_run):'never'}</span></div>
        <div class="hb-row"><span class="hb-label">Next due</span><span class="hb-value">${never?'-':fmtCountdown(t.seconds_until_due)}</span></div>
        <div class="hb-row"><span class="hb-label">Status</span><span class="hb-value">${statusTxt}</span></div>
        ${t.pre?`<div class="hb-row"><span class="hb-label">Pre-script</span><span class="hb-value" style="font-family:monospace;font-size:11px">${esc(t.pre)}</span></div>`:''}
        ${t.post?`<div class="hb-row"><span class="hb-label">Post-script</span><span class="hb-value" style="font-family:monospace;font-size:11px">${esc(t.post)}</span></div>`:''}
        <div class="hb-row" style="margin-top:8px"><span class="hb-label" style="align-self:flex-start">Prompt</span></div>
        <div style="background:var(--surface2);border-radius:6px;padding:10px 12px;font-size:11.5px;color:var(--muted);white-space:pre-wrap;word-wrap:break-word;line-height:1.6;max-height:200px;overflow-y:auto;margin-top:4px">${esc(t.prompt||'(none)')}</div>
        <div id="hb-edit-${i}" style="display:none;margin-top:12px;border-top:1px solid var(--border);padding-top:12px">
          <div style="display:grid;grid-template-columns:auto 1fr;gap:8px;align-items:center;margin-bottom:8px">
            <label style="font-size:11px;color:var(--dim)">Interval</label>
            <input id="hb-int-${i}" class="form-input" value="${esc(t.interval_str||'')}" style="width:80px" placeholder="10m" />
            <label style="font-size:11px;color:var(--dim)">Pre-script</label>
            <input id="hb-pre-${i}" class="form-input" value="${esc(t.pre||'')}" placeholder="tasks/xxx_pre.sh" />
            <label style="font-size:11px;color:var(--dim)">Post-script</label>
            <input id="hb-post-${i}" class="form-input" value="${esc(t.post||'')}" placeholder="tasks/xxx_post.py" />
          </div>
          <label style="font-size:11px;color:var(--dim);display:block;margin-bottom:4px">Prompt</label>
          <textarea id="hb-prompt-${i}" class="form-input" style="font-family:'SF Mono',monospace;font-size:11.5px;min-height:180px;width:100%;resize:vertical">${esc(t.prompt||'')}</textarea>
          <div style="display:flex;gap:8px;margin-top:8px;justify-content:flex-end">
            <button class="btn btn-ghost btn-sm" onclick="toggleHbEdit(${i})">Cancel</button>
            <button class="btn btn-primary btn-sm" id="hb-save-${i}" onclick="saveHbTask(${i})">Save</button>
          </div>
        </div>
      </div>`}).join('')}</div>`;
  loadHeartbeatTimeline();
}
function toggleHbEdit(i){
  const ed=document.getElementById('hb-edit-'+i);
  ed.style.display=ed.style.display==='none'?'block':'none';
}
function saveHbTask(i){
  const t=_hbTasks[i];
  const btn=document.getElementById('hb-save-'+i);
  btn.disabled=true; btn.textContent='Saving...';
  const body={
    name:t.name,
    interval_str:document.getElementById('hb-int-'+i).value,
    pre:document.getElementById('hb-pre-'+i).value,
    post:document.getElementById('hb-post-'+i).value,
    prompt:document.getElementById('hb-prompt-'+i).value,
  };
  fetch('/api/heartbeat/task',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})
    .then(r=>r.json()).then(res=>{
      if(res.ok){toast('Task "'+t.name+'" saved');loadHeartbeat();}
      else{toast('Error: '+(res.error||'unknown'));btn.textContent='Save';btn.disabled=false;}
    }).catch(()=>{toast('Network error');btn.textContent='Save';btn.disabled=false;});
}
function forceRun(name,btn){
  btn.disabled=true; btn.textContent='...';
  fetch('/api/heartbeat/force/'+encodeURIComponent(name),{method:'POST'})
    .then(r=>r.json()).then(res=>{
      if(res.ok){ btn.textContent='Triggered'; toast('Triggered: '+name); setTimeout(()=>{btn.textContent='Run Now';btn.disabled=false;},2000); }
      else{ btn.textContent='Run Now'; btn.disabled=false; toast('Error: '+(res.error||'unknown')); }
    }).catch(()=>{btn.textContent='Run Now';btn.disabled=false;toast('Network error');});
}
loadHeartbeat();
setInterval(()=>{
  if(document.getElementById('heartbeat').classList.contains('active')) loadHeartbeatTimeline();
}, 30000);

// ── Heartbeat Timeline ──
let _hbtData=[], _hbtFilter='all';
function hbtBadgeClass(src){
  if(src==='checkin') return 'hbt-badge-checkin';
  if(src==='content-recommend') return 'hbt-badge-content-recommend';
  if(src==='calendar-sync') return 'hbt-badge-calendar-sync';
  if(src==='eigenflux'||src==='eigenflux-stream') return 'hbt-badge-eigenflux';
  return 'hbt-badge-other';
}
function loadHeartbeatTimeline(){
  fetch('/api/heartbeat-timeline').then(r=>r.json()).then(data=>{
    _hbtData=data; renderHeartbeatTimeline();
  }).catch(()=>{});
}
function renderHeartbeatTimeline(){
  const el=document.getElementById('hbt-panel');
  if(!el) return;
  const sources=[...new Set(_hbtData.map(e=>e.source))];
  const filtered=_hbtFilter==='all'?_hbtData:_hbtData.filter(e=>e.source===_hbtFilter);
  el.innerHTML=`
    <div class="hbt-tabs">
      <button class="hbt-tab ${_hbtFilter==='all'?'active':''}" onclick="_hbtFilter='all';renderHeartbeatTimeline()">All (${_hbtData.length})</button>
      ${sources.map(s=>`<button class="hbt-tab ${_hbtFilter===s?'active':''}" onclick="_hbtFilter='${esc(s)}';renderHeartbeatTimeline()">${esc(s)} (${_hbtData.filter(e=>e.source===s).length})</button>`).join('')}
    </div>
    ${filtered.length?filtered.map(e=>`<div class="hbt-entry">
      <div class="hbt-head">
        <span class="hbt-ts">${esc(e.ts)}</span>
        <span class="hbt-badge ${hbtBadgeClass(e.source)}">${esc(e.source)}</span>
      </div>
      <div class="hbt-text">${fmtParagraphs(e.text)}</div>
    </div>`).join(''):'<div class="empty-state">No heartbeat outputs yet</div>'}`;
}

// ── EigenFlux ──
function loadEigenFlux(){
  const el=document.getElementById('eigenflux');
  el.innerHTML='<div class="section-header"><h2>EigenFlux</h2></div><div class="empty-state">Loading...</div>';
  Promise.all([
    fetch('/api/eigenflux/status').then(r=>r.json()),
    fetch('/api/eigenflux/settings').then(r=>r.json())
  ]).then(([status,settings])=>{
    const authed=status.authed;
    const profile=status.profile||{};
    const dotHtml=authed?'<span class="status-dot dot-green"></span>Connected':'<span class="status-dot dot-gray"></span>Not connected';
    const prefs=['Push everything','Action-only','Digest','Silent'];
    const curPref=settings.feed_delivery_preference||'Push everything';
    const curCool=settings.publish_cooldown_minutes||60;
    el.innerHTML=`
      <div class="section-header"><h2>EigenFlux</h2></div>
      <div class="ef-status-box">
        <div style="font-size:14px;font-weight:600;margin-bottom:10px">Connection Status</div>
        <div style="font-size:13px;color:var(--muted)">${dotHtml}</div>
        ${authed?`<div style="margin-top:8px;font-size:12px;color:var(--dim)">
          Agent: <span style="color:var(--text)">${esc(profile.agent_name||profile.name||'-')}</span>
          &middot; ID: <span style="color:var(--accent2);font-family:SF Mono,monospace">${esc(profile.agent_id||profile.id||'-')}</span>
          ${profile.influence?` &middot; Influence items: ${profile.influence.total_items||0}`:''}
        </div>`:`<div style="margin-top:8px;font-size:11px;color:var(--dim)">${esc(status.error||status.stderr||'')}</div>`}
      </div>
      <div class="ef-form">
        <div style="font-size:14px;font-weight:600;margin-bottom:14px">Settings</div>
        <div class="form-group"><label>Feed delivery preference</label>
          <div class="radio-group">
            ${prefs.map(p=>`<label><input type="radio" name="ef-pref" value="${p}" ${p===curPref?'checked':''}> ${p}</label>`).join('')}
          </div></div>
        <div class="form-group"><label>Publish cooldown (minutes)</label>
          <input class="form-input" type="number" id="ef-cool" value="${curCool}" min="0" style="width:120px"></div>
        <button class="btn btn-primary" id="ef-save" onclick="saveEFSettings()">Save</button>
      </div>`;
  });
}
function saveEFSettings(){
  const btn=document.getElementById('ef-save');
  const pref=document.querySelector('input[name="ef-pref"]:checked');
  const payload={
    feed_delivery_preference: pref?pref.value:'Push everything',
    publish_cooldown_minutes: parseInt(document.getElementById('ef-cool').value)||60
  };
  btn.textContent='Saving...'; btn.disabled=true;
  fetch('/api/eigenflux/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)})
    .then(r=>r.json()).then(res=>{
      btn.textContent='Save'; btn.disabled=false;
      if(res.ok) toast('EigenFlux settings saved');
      else toast('Error: '+(res.error||'unknown'));
    }).catch(()=>{btn.textContent='Save';btn.disabled=false;toast('Network error');});
}
loadEigenFlux();

// ── Search ──
let allSessionsMeta=[];
fetch('/api/sessions_meta').then(r=>r.json()).then(d=>{allSessionsMeta=d;initSearch();});
function initSearch(){
  const el=document.getElementById('search');
  el.innerHTML=`
    <div class="section-header"><h2>Search</h2><span class="count" id="s-count"></span></div>
    <div class="search-bar">
      <input type="text" id="s-input" placeholder="Search across conversations..." />
      <select id="s-scope"><option value="">All sessions</option>
        ${allSessionsMeta.map(s=>`<option value="${s.id}">${s.date} -- ${esc(s.first_prompt.slice(0,40))}</option>`).join('')}
      </select>
      <button id="s-btn">Search</button>
    </div><div id="s-results"></div>`;
  const input=document.getElementById('s-input'),scope=document.getElementById('s-scope'),
    btn=document.getElementById('s-btn'),results=document.getElementById('s-results'),
    countEl=document.getElementById('s-count');
  function doSearch(){
    const q=input.value.trim(); if(!q)return;
    results.innerHTML='<div class="empty-state">Searching...</div>';
    let url='/api/search?q='+encodeURIComponent(q);
    if(scope.value)url+='&session_id='+encodeURIComponent(scope.value);
    fetch(url).then(r=>r.json()).then(data=>{
      countEl.textContent=data.length+' results';
      if(!data.length){results.innerHTML='<div class="empty-state">No results</div>';return;}
      results.innerHTML=data.map(r=>`<div class="sr">
          <div class="sr-header">
            <span class="sr-sid" onclick="openSession('${r.session_id}')">${esc(r.session_id.slice(0,8))}</span>
            <span>${esc(r.timestamp)}</span>${roleTag(r.role)}
          </div><div class="sr-body">
            ${r.context_before.map(c=>`<div class="sr-ctx">${roleTag(c.role)} ${esc(c.text)}</div>`).join('')}
            <div class="sr-match-wrap"><div class="sr-match">${roleTag(r.role)} ${hl(r.text,q)}</div></div>
            ${r.context_after.map(c=>`<div class="sr-ctx">${roleTag(c.role)} ${esc(c.text)}</div>`).join('')}
          </div></div>`).join('');
    });
  }
  btn.addEventListener('click',doSearch);
  input.addEventListener('keydown',e=>{if(e.key==='Enter')doSearch();});
}

// ── Sessions ──
let sessionsData=[];
fetch('/api/sessions_meta').then(r=>r.json()).then(data=>{sessionsData=data;renderSessionsList();});
function renderSessionsList(){
  const el=document.getElementById('sessions');
  el.innerHTML=`<div class="sessions-layout">
    <div class="session-list" id="sess-list">
      ${sessionsData.map((s,i)=>`<div class="sl-item${i===0?' active':''}" data-id="${s.id}" onclick="selectSession('${s.id}',this)">
          <div class="sl-date">${esc(s.date)}</div>
          <div class="sl-prompt">${esc(s.first_prompt.slice(0,80))}${s.first_prompt.length>80?'...':''}</div>
          <div class="sl-meta"><span class="sl-id">${s.id.slice(0,8)}</span> &middot; ${s.message_count} msgs</div>
        </div>`).join('')}
    </div>
    <div class="session-viewer" id="sess-viewer"><div class="sv-empty">Select a session to view</div></div>
  </div>`;
  if(sessionsData.length)selectSession(sessionsData[0].id,document.querySelector('.sl-item'));
}
let _currentMsgs=[],_currentShown=30;
function selectSession(id,itemEl){
  document.querySelectorAll('.sl-item').forEach(e=>e.classList.remove('active'));
  if(itemEl)itemEl.classList.add('active');
  const viewer=document.getElementById('sess-viewer');
  viewer.innerHTML='<div class="sv-empty">Loading...</div>';
  fetch('/api/session/'+id).then(r=>r.json()).then(msgs=>{
    _currentMsgs=msgs; _currentShown=30;
    const s=sessionsData.find(x=>x.id===id)||{};
    viewer.innerHTML=`<div class="sv-header"><h3>${esc(s.date||'')} &middot; ${msgs.length} messages</h3>
      <div class="sv-search"><input type="text" id="sv-filter" placeholder="Filter this session..." /></div></div>
      <div class="sv-messages" id="sv-msgs">${renderMessages(msgs,'',_currentShown)}</div>`;
    document.getElementById('sv-filter').addEventListener('input',e=>{
      document.getElementById('sv-msgs').innerHTML=renderMessages(msgs,e.target.value,0);
    });
  });
}
function loadEarlier(){
  _currentShown=Math.min(_currentShown+50,_currentMsgs.length);
  document.getElementById('sv-msgs').innerHTML=renderMessages(_currentMsgs,'',_currentShown);
}
function renderMessages(msgs,filter,tail){
  const q=filter.trim().toLowerCase();
  let filtered=q?msgs.filter(m=>m.text.toLowerCase().includes(q)):msgs;
  if(!filtered.length)return '<div class="sv-empty">No messages match</div>';
  let loadBtn='';
  if(tail>0&&!q&&filtered.length>tail){
    const hidden=filtered.length-tail;
    loadBtn=`<div style="text-align:center;padding:12px 0;border-bottom:1px solid var(--border);margin-bottom:8px">
      <button onclick="loadEarlier()" style="font-family:inherit;font-size:12px;padding:6px 16px;border-radius:6px;border:1px solid var(--border);background:var(--surface2);color:var(--muted);cursor:pointer">Load earlier (${hidden} hidden)</button></div>`;
    filtered=filtered.slice(-tail);
  }
  return loadBtn+filtered.map(m=>{
    if(m.role==='tool'){
      return `<div class="sv-msg sv-tool"><div class="sv-msg-head">
        <span class="tool-name">${esc(m.tool_name||'tool')}</span>
        <span class="sv-msg-ts">${esc(m.timestamp)}</span></div>
        <div class="sv-msg-text">${fmtParagraphs(m.text)}</div></div>`;
    }
    if(m.role==='tool_result'){
      return `<div class="sv-msg sv-tool-result">
        <div class="sv-msg-text">${filter?hl(m.text,filter):esc(m.text)}</div></div>`;
    }
    return `<div class="sv-msg"><div class="sv-msg-head">${roleTag(m.role)}
      <span class="sv-msg-ts">${esc(m.timestamp)}</span></div>
      <div class="sv-msg-text">${filter?hl(m.text,filter):fmtParagraphs(m.text)}</div></div>`;
  }).join('');
}
function openSession(id){
  document.querySelectorAll('.nav-item').forEach(b=>b.classList.remove('active'));
  document.querySelectorAll('section').forEach(s=>s.classList.remove('active'));
  document.querySelector('[data-tab="sessions"]').classList.add('active');
  document.getElementById('sessions').classList.add('active');
  const item=document.querySelector(`.sl-item[data-id="${id}"]`);
  selectSession(id,item); if(item)item.scrollIntoView({block:'center'});
}

// ── Settings ──
fetch('/api/settings').then(r=>r.json()).then(data=>{
  const el=document.getElementById('settings');
  el.innerHTML=Object.entries(data).map(([title,obj])=>`
    <div class="section-header"><h2>${esc(title)}</h2></div>
    ${Object.keys(obj).length?`<div class="card"><div class="card-body">${esc(JSON.stringify(obj,null,2))}</div></div>`
    :'<div class="empty-state">Empty</div>'}`).join('');
});
</script>
</body>
</html>"""


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
    if (_sessions_meta_cache["key"] == key
            and now - _sessions_meta_cache["time"] < _CACHE_TTL_SECONDS):
        return _sessions_meta_cache["data"]
    data = core_search.sessions_meta(PROJECT_DIR)
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
    for base in SESSION_SEARCH_PATHS:
        candidate = base / f"{session_id}.jsonl"
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
            from datetime import datetime
            dt = datetime.strptime(last_log_ts, "%Y-%m-%d %H:%M:%S")
            last_activity_ago = int((datetime.now() - dt).total_seconds())
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
    return {"tasks": result}


def save_heartbeat_task(name: str, interval_str: str, pre: str, post: str,
                        prompt: str) -> dict:
    """Update a single task in HEARTBEAT.md. Rewrites the file preserving all other tasks."""
    heartbeat_path = ROOT / "HEARTBEAT.md"
    if not heartbeat_path.exists():
        return {"error": "HEARTBEAT.md not found"}

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

    return {"ok": True}


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


# ── RichView Renderer ────────────────────────────────────────────────

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
            # Raw HTML passthrough (trust internal callers)
            body_parts.append(f'<div class="rv-section">{sec.get("content", "")}</div>')
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
            # Also accept ?token=... for convenience in browsers
            parsed = urlparse(self.path)
            provided = parse_qs(parsed.query).get("token", [""])[0]
        return provided == ADMIN_TOKEN

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        # RichView pages bypass auth (accessible from Lark card links)
        if path.startswith("/view/"):
            view_id = path.split("/view/")[1].split("/")[0].split("?")[0]
            self._serve_richview(view_id)
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
            self._json(eigenflux_status())
        elif path == "/api/eigenflux/settings":
            self._json(eigenflux_settings())
        elif path == "/api/views":
            self._json(list_views())
        else:
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(HTML.encode())

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

    def _read_json_body(self) -> dict | None:
        """Read and parse JSON request body. Returns None on failure (sends 400)."""
        try:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length)
            return json.loads(raw) if raw else {}
        except (json.JSONDecodeError, ValueError):
            self._json({"error": "invalid JSON body"}, status=400)
            return None

    def do_POST(self):
        if not self._check_auth():
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error":"unauthorized"}')
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
            result = save_heartbeat_task(name, interval_str, pre, post, prompt)
            self._json(result, status=200 if "ok" in result else 400)

        elif path.startswith("/api/heartbeat/force/"):
            task_name = path.split("/api/heartbeat/force/", 1)[1]
            if not task_name:
                self._json({"error": "task name required"}, status=400)
                return
            # Write trigger file that bot.sh reads
            trigger_path = Path("/tmp/jarvis-heartbeat-trigger")
            try:
                trigger_path.write_text(task_name, encoding="utf-8")
                self._json({"ok": True, "task": task_name})
            except Exception as e:
                self._json({"error": str(e)}, status=500)

        elif path == "/api/eigenflux/settings":
            body = self._read_json_body()
            if body is None:
                return
            # Merge with existing settings so partial updates work
            current = eigenflux_settings()
            if "error" in current:
                current = {}
            current.update(body)
            result = save_eigenflux_settings(current)
            self._json(result, status=200 if "ok" in result else 500)

        elif path == "/api/bot/stop_task":
            # Kill the running Claude process for the most active session
            import glob as _glob
            locks = _glob.glob(str(ROOT / ".session_lock_*"))
            killed = 0
            for lock in locks:
                try:
                    pid = int(Path(lock).read_text().strip())
                    os.kill(pid, 9)
                    killed += 1
                except (ValueError, ProcessLookupError, OSError):
                    pass
                Path(lock).unlink(missing_ok=True)
            self._json({"ok": True, "killed": killed})

        elif path == "/api/bot/restart":
            # Write restart trigger file for bot.sh to pick up
            trigger = ROOT / ".restart_trigger"
            trigger.write_text(str(int(time.time())))
            self._json({"ok": True, "message": "Restart triggered"})

        else:
            self._json({"error": "not found"}, status=404)

    def do_DELETE(self):
        if not self._check_auth():
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error":"unauthorized"}')
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
    server = http.server.HTTPServer((HOST, PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
