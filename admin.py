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
  --danger: #f87171;
}
* { margin:0; padding:0; box-sizing:border-box; }
body {
  font-family: -apple-system, 'SF Pro Text', 'Inter', system-ui, sans-serif;
  background: var(--bg); color: var(--text);
  -webkit-font-smoothing: antialiased; height: 100vh; overflow: hidden;
}

.app { display: flex; height: 100vh; }
.sidebar {
  width: 200px; min-width: 200px; background: var(--surface);
  border-right: 1px solid var(--border); padding: 20px 0;
  display: flex; flex-direction: column;
}
.sidebar .logo {
  font-size: 12px; font-weight: 700; letter-spacing: 0.12em;
  text-transform: uppercase; color: var(--accent);
  font-family: 'SF Mono', 'Fira Code', monospace;
  padding: 0 20px 20px; border-bottom: 1px solid var(--border);
}
.sidebar .nav-item {
  display: flex; align-items: center; gap: 8px;
  padding: 10px 20px; font-size: 13px; font-weight: 500;
  color: var(--dim); cursor: pointer; transition: all 0.12s; border: none;
  background: none; width: 100%; text-align: left; font-family: inherit;
}
.sidebar .nav-item:hover { color: var(--muted); background: var(--surface2); }
.sidebar .nav-item.active { color: var(--text); background: var(--surface2); border-right: 2px solid var(--accent); }
.main { flex: 1; overflow-y: auto; padding: 32px 40px; }

.section-header {
  display: flex; align-items: baseline; justify-content: space-between;
  margin-bottom: 20px;
}
.section-header h2 { font-size: 18px; font-weight: 600; }
.section-header .count { font-size: 12px; color: var(--dim); }
section { display: none; }
section.active { display: block; }

.card {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 10px; padding: 18px 22px;
  margin-bottom: 10px; transition: border-color 0.15s;
}
.card:hover { border-color: var(--border2); }
.card-head { display: flex; align-items: center; gap: 10px; margin-bottom: 8px; }
.card-name { font-size: 13px; font-weight: 600; }
.badge {
  font-size: 10px; font-weight: 600; letter-spacing: 0.06em;
  text-transform: uppercase; padding: 2px 8px; border-radius: 4px;
}
.badge-user { color: var(--user); background: #0c2d48; }
.badge-feedback { color: var(--feedback); background: #2e1d0a; }
.badge-project { color: var(--project); background: #0a2e1a; }
.badge-reference { color: var(--reference); background: #220a3e; }
.card-desc { font-size: 11.5px; color: var(--dim); margin-bottom: 10px; font-style: italic; }
.card-body {
  font-size: 12.5px; line-height: 1.75; color: var(--muted);
  white-space: pre-wrap; word-wrap: break-word;
}
.card-body strong { color: var(--accent); font-weight: 600; }
.card-meta { font-size: 10px; color: #333; margin-top: 10px; text-align: right; }
.filters { display: flex; gap: 6px; margin-bottom: 20px; flex-wrap: wrap; }
.fbtn {
  font-family: inherit; font-size: 11px; font-weight: 500;
  padding: 4px 12px; border-radius: 5px;
  border: 1px solid var(--border); background: transparent;
  color: var(--dim); cursor: pointer; transition: all 0.15s;
}
.fbtn:hover, .fbtn.active { border-color: var(--accent); color: var(--accent); }

.search-bar { display: flex; gap: 8px; margin-bottom: 20px; }
.search-bar input {
  flex: 1; font-family: inherit; font-size: 13px;
  padding: 10px 16px; border-radius: 8px;
  border: 1px solid var(--border); background: var(--surface);
  color: var(--text); outline: none; transition: border-color 0.15s;
}
.search-bar input:focus { border-color: var(--accent); }
.search-bar input::placeholder { color: var(--dim); }
.search-bar select {
  font-family: inherit; font-size: 12px; padding: 8px 12px;
  border-radius: 8px; border: 1px solid var(--border);
  background: var(--surface); color: var(--muted); outline: none;
  cursor: pointer;
}
.search-bar button {
  font-family: inherit; font-size: 13px; font-weight: 600;
  padding: 10px 24px; border-radius: 8px; border: none;
  background: var(--accent); color: #000; cursor: pointer;
  transition: opacity 0.15s; white-space: nowrap;
}
.search-bar button:hover { opacity: 0.85; }

.sr {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 10px; margin-bottom: 12px; overflow: hidden;
}
.sr-header {
  display: flex; align-items: center; gap: 12px;
  padding: 12px 18px; font-size: 11px; color: var(--dim);
  border-bottom: 1px solid var(--border); background: var(--surface2);
}
.sr-header .sr-sid { font-family: 'SF Mono', monospace; color: var(--accent2); cursor: pointer; }
.sr-header .sr-sid:hover { text-decoration: underline; }
.sr-body { padding: 14px 18px; }
.sr-ctx {
  font-size: 12px; line-height: 1.65; color: #4a4a52;
  padding: 4px 0 4px 14px; border-left: 2px solid var(--border);
  margin-bottom: 4px;
}
.sr-ctx .sr-role { margin-right: 6px; }
.sr-match-wrap {
  background: var(--highlight-bg); border-radius: 6px;
  padding: 10px 14px; margin: 8px 0;
}
.sr-match {
  font-size: 12.5px; line-height: 1.75; color: var(--text);
  white-space: pre-wrap; word-wrap: break-word;
}
.sr-match mark { background: rgba(251,191,36,0.35); color: var(--highlight); padding: 1px 3px; border-radius: 2px; }
.sr-role {
  font-size: 9px; font-weight: 700; text-transform: uppercase;
  letter-spacing: 0.08em; display: inline-block; padding: 1px 5px;
  border-radius: 3px;
}
.sr-role.user { color: var(--user); background: var(--user-dim); }
.sr-role.assistant { color: var(--accent); background: var(--accent-dim); }

.sessions-layout { display: flex; gap: 0; height: calc(100vh - 120px); }
.session-list {
  width: 320px; min-width: 280px; overflow-y: auto;
  border-right: 1px solid var(--border); padding-right: 0;
}
.session-viewer { flex: 1; overflow-y: auto; padding-left: 24px; }
.sl-item {
  padding: 12px 16px; cursor: pointer; border-bottom: 1px solid var(--border);
  transition: all 0.12s;
}
.sl-item:hover { background: var(--surface); }
.sl-item.active { background: var(--surface2); border-left: 3px solid var(--accent); }
.sl-item .sl-date { font-size: 10px; color: var(--dim); margin-bottom: 2px; }
.sl-item .sl-prompt { font-size: 12px; color: var(--muted); line-height: 1.4; }
.sl-item .sl-meta { font-size: 10px; color: var(--dim); margin-top: 4px; }
.sl-item .sl-id { font-family: 'SF Mono', monospace; font-size: 10px; color: var(--accent2); }

.sv-header {
  display: flex; align-items: center; justify-content: space-between;
  padding-bottom: 16px; border-bottom: 1px solid var(--border);
  margin-bottom: 16px;
}
.sv-header h3 { font-size: 14px; font-weight: 600; }
.sv-search { display: flex; gap: 6px; }
.sv-search input {
  font-family: inherit; font-size: 12px; padding: 6px 12px;
  border-radius: 6px; border: 1px solid var(--border);
  background: var(--surface); color: var(--text); outline: none; width: 200px;
}
.sv-search input:focus { border-color: var(--accent); }
.sv-messages { padding-bottom: 40px; }
.sv-msg { padding: 12px 0; border-bottom: 1px solid #1a1a1d; }
.sv-msg:last-child { border-bottom: none; }
.sv-msg-head { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; }
.sv-msg-ts { font-size: 10px; color: var(--dim); }
.sv-msg-text {
  font-size: 13px; line-height: 1.8; color: var(--muted);
  white-space: pre-wrap; word-wrap: break-word;
}
.sv-msg-text mark { background: rgba(251,191,36,0.35); color: var(--highlight); padding: 1px 3px; border-radius: 2px; }
.sv-empty { text-align: center; padding: 80px 0; color: var(--dim); font-size: 13px; }

.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 10px; }
.grid-item {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 8px; padding: 16px 18px; transition: border-color 0.15s;
}
.grid-item:hover { border-color: var(--border2); }
.grid-item .gi-name { font-size: 13px; font-weight: 600; margin-bottom: 6px; }
.grid-item .gi-desc { font-size: 11px; color: var(--dim); line-height: 1.5; }

.empty-state { text-align: center; padding: 60px 0; color: var(--dim); font-size: 13px; }

/* Lark chats view */
.chat-group { margin-bottom: 36px; }
.chat-head {
  display: flex; align-items: center; gap: 10px;
  padding-bottom: 10px; margin-bottom: 12px;
  border-bottom: 1px solid var(--border);
}
.chat-head .chat-kind {
  font-size: 9px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.08em;
  padding: 2px 7px; border-radius: 4px;
}
.chat-head .chat-kind.p2p { color: var(--user); background: var(--user-dim); }
.chat-head .chat-kind.group { color: var(--feedback); background: #2e1d0a; }
.chat-head .chat-key {
  font-family: 'SF Mono', 'Fira Code', monospace;
  font-size: 11px; color: var(--muted);
}
.chat-head .chat-stats { margin-left: auto; font-size: 11px; color: var(--dim); }
.chat-timeline { display: flex; flex-direction: column; gap: 6px; }
.ct-item {
  display: grid; grid-template-columns: 28px 120px 100px 1fr auto; align-items: center; gap: 12px;
  padding: 10px 14px; border-radius: 8px;
  background: var(--surface); border: 1px solid var(--border);
  font-size: 12px; cursor: pointer; transition: border-color 0.12s, background 0.12s;
}
.ct-item:hover { border-color: var(--border2); background: var(--surface2); }
.ct-item.active {
  border-color: var(--accent);
  background: var(--accent-dim);
}
.ct-item.missing { opacity: 0.45; cursor: default; }
.ct-counter {
  font-family: 'SF Mono', monospace; font-size: 11px; color: var(--dim);
  text-align: right;
}
.ct-counter.current { color: var(--accent); font-weight: 700; }
.ct-sid {
  font-family: 'SF Mono', monospace; font-size: 11px; color: var(--accent2);
}
.ct-date { font-size: 11px; color: var(--muted); }
.ct-preview {
  color: var(--dim); font-size: 11.5px;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.ct-meta { font-size: 10px; color: var(--dim); white-space: nowrap; }
.ct-badge-active {
  font-size: 9px; font-weight: 700; text-transform: uppercase;
  padding: 2px 7px; border-radius: 3px;
  color: #000; background: var(--accent);
}

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
  <button class="nav-item active" data-tab="lark">Lark</button>
  <button class="nav-item" data-tab="memories">Memories</button>
  <button class="nav-item" data-tab="search">Search</button>
  <button class="nav-item" data-tab="sessions">Sessions</button>
  <button class="nav-item" data-tab="skills">Skills</button>
  <button class="nav-item" data-tab="settings">Settings</button>
</div>
<div class="main">
<section id="lark" class="active"></section>
<section id="memories"></section>
<section id="search"></section>
<section id="sessions"></section>
<section id="skills"></section>
<section id="settings"></section>
</div>
</div>
<script>
document.querySelectorAll('.nav-item').forEach(t => {
  t.addEventListener('click', () => {
    document.querySelectorAll('.nav-item').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('section').forEach(s => s.classList.remove('active'));
    t.classList.add('active');
    document.getElementById(t.dataset.tab).classList.add('active');
  });
});
const esc = s => (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
const bold = s => esc(s).replace(/\*\*(.*?)\*\*/g,'<strong>$1</strong>');
function hl(text, q) {
  if (!q) return esc(text);
  const e = esc(text);
  return e.replace(new RegExp('('+q.replace(/[.*+?^${}()|[\]\\]/g,'\\$&')+')','gi'),'<mark>$1</mark>');
}
function roleTag(r) { return `<span class="sr-role ${r}">${r}</span>`; }

function fmtBytes(n) {
  if (!n) return '-';
  if (n < 1024) return n + 'B';
  if (n < 1024*1024) return (n/1024).toFixed(1) + 'KB';
  return (n/1024/1024).toFixed(2) + 'MB';
}
function fmtTs(ts) { return (ts||'').slice(0,16).replace('T',' ') || '-'; }

// ── Lark Chats ──
fetch('/api/lark_chats').then(r=>r.json()).then(data=>{
  const el=document.getElementById('lark');
  if(!data.length){el.innerHTML='<div class="empty-state">No Lark conversations tracked yet</div>';return;}

  const totalChats = data.length;
  const activeCount = data.reduce((a,c)=>a+(c.active_session_id?1:0),0);

  el.innerHTML=`
    <div class="section-header">
      <h2>Lark Conversations</h2>
      <span class="count">${totalChats} chat${totalChats>1?'s':''} · ${activeCount} active</span>
    </div>
    ${data.map(chat=>{
      // Session rotations, earliest → latest
      const activeIdx = chat.sessions.findIndex(s=>s.is_active);
      return `
        <div class="chat-group">
          <div class="chat-head">
            <span class="chat-kind ${chat.kind}">${chat.kind}</span>
            <span class="chat-key">${esc(chat.conv_key)}</span>
            <span class="chat-stats">${chat.current_counter} rotation${chat.current_counter>1?'s':''} · ${chat.total_messages} msgs</span>
          </div>
          <div class="chat-timeline">
            ${chat.sessions.map(s=>`
              <div class="ct-item ${s.is_active?'active':''} ${s.exists?'':'missing'}"
                   ${s.exists?`onclick="openSession('${s.session_id}')"`:''}>
                <span class="ct-counter ${s.is_active?'current':''}">#${s.counter}</span>
                <span class="ct-sid">${esc(s.session_id.slice(0,8))}</span>
                <span class="ct-date">${esc(fmtTs(s.first_ts))}</span>
                <span class="ct-preview">${s.exists ? esc(s.first_prompt || '(no user messages)') : '<em>file missing</em>'}</span>
                <span class="ct-meta">
                  ${s.is_active?'<span class="ct-badge-active">current</span> ':''}
                  ${s.exists?`${s.message_count||0} msgs · ${fmtBytes(s.size_bytes)}`:''}
                </span>
              </div>`).join('')}
          </div>
        </div>`;
    }).join('')}`;
});

fetch('/api/memories').then(r=>r.json()).then(data=>{
  const el=document.getElementById('memories');
  if(!data.length){el.innerHTML='<div class="empty-state">No memories yet</div>';return;}
  const types=[...new Set(data.map(m=>m.type))];
  let filter=null;
  function render(){
    const f=filter?data.filter(m=>m.type===filter):data;
    el.innerHTML=`
      <div class="section-header"><h2>Memories</h2><span class="count">${f.length}</span></div>
      <div class="filters">${['all',...types].map(t=>`<button class="fbtn${(t==='all'&&!filter)||(t===filter)?' active':''}" data-t="${t}">${t}</button>`).join('')}</div>
      ${f.map(m=>`<div class="card">
        <div class="card-head"><span class="card-name">${esc(m.name)}</span><span class="badge badge-${m.type}">${m.type}</span></div>
        ${m.description?`<div class="card-desc">${esc(m.description)}</div>`:''}
        <div class="card-body">${bold(m.body)}</div>
        <div class="card-meta">${esc(m.file)}</div>
      </div>`).join('')}`;
    el.querySelectorAll('.fbtn').forEach(b=>b.addEventListener('click',()=>{
      filter=b.dataset.t==='all'?null:b.dataset.t;render();
    }));
  }
  render();
});

let allSessionsMeta = [];
fetch('/api/sessions_meta').then(r=>r.json()).then(d=>{allSessionsMeta=d;initSearch();});
function initSearch(){
  const el=document.getElementById('search');
  el.innerHTML=`
    <div class="section-header"><h2>Search</h2><span class="count" id="s-count"></span></div>
    <div class="search-bar">
      <input type="text" id="s-input" placeholder="Search across conversations..." />
      <select id="s-scope">
        <option value="">All sessions</option>
        ${allSessionsMeta.map(s=>`<option value="${s.id}">${s.date} — ${esc(s.first_prompt.slice(0,40))}</option>`).join('')}
      </select>
      <button id="s-btn">Search</button>
    </div>
    <div id="s-results"></div>`;
  const input=document.getElementById('s-input');
  const scope=document.getElementById('s-scope');
  const btn=document.getElementById('s-btn');
  const results=document.getElementById('s-results');
  const countEl=document.getElementById('s-count');
  function doSearch(){
    const q=input.value.trim();
    if(!q)return;
    results.innerHTML='<div class="empty-state">Searching...</div>';
    const sid=scope.value;
    let url='/api/search?q='+encodeURIComponent(q);
    if(sid)url+='&session_id='+encodeURIComponent(sid);
    fetch(url).then(r=>r.json()).then(data=>{
      countEl.textContent=data.length+' results';
      if(!data.length){results.innerHTML='<div class="empty-state">No results</div>';return;}
      results.innerHTML=data.map(r=>`
        <div class="sr">
          <div class="sr-header">
            <span class="sr-sid" onclick="openSession('${r.session_id}')">${esc(r.session_id.slice(0,8))}</span>
            <span>${esc(r.timestamp)}</span>
            ${roleTag(r.role)}
          </div>
          <div class="sr-body">
            ${r.context_before.map(c=>`<div class="sr-ctx">${roleTag(c.role)} ${esc(c.text)}</div>`).join('')}
            <div class="sr-match-wrap"><div class="sr-match">${roleTag(r.role)} ${hl(r.text,q)}</div></div>
            ${r.context_after.map(c=>`<div class="sr-ctx">${roleTag(c.role)} ${esc(c.text)}</div>`).join('')}
          </div>
        </div>`).join('');
    });
  }
  btn.addEventListener('click',doSearch);
  input.addEventListener('keydown',e=>{if(e.key==='Enter')doSearch();});
}

let sessionsData=[];
fetch('/api/sessions_meta').then(r=>r.json()).then(data=>{
  sessionsData=data;
  renderSessionsList();
});
function renderSessionsList(){
  const el=document.getElementById('sessions');
  el.innerHTML=`
    <div class="sessions-layout">
      <div class="session-list" id="sess-list">
        ${sessionsData.map((s,i)=>`
          <div class="sl-item${i===0?' active':''}" data-id="${s.id}" onclick="selectSession('${s.id}',this)">
            <div class="sl-date">${esc(s.date)}</div>
            <div class="sl-prompt">${esc(s.first_prompt.slice(0,80))}${s.first_prompt.length>80?'...':''}</div>
            <div class="sl-meta"><span class="sl-id">${s.id.slice(0,8)}</span> · ${s.message_count} msgs</div>
          </div>`).join('')}
      </div>
      <div class="session-viewer" id="sess-viewer">
        <div class="sv-empty">Select a session to view</div>
      </div>
    </div>`;
  if(sessionsData.length)selectSession(sessionsData[0].id,document.querySelector('.sl-item'));
}
let _currentMsgs=[];
let _currentShown=30;
function selectSession(id,itemEl){
  document.querySelectorAll('.sl-item').forEach(e=>e.classList.remove('active'));
  if(itemEl)itemEl.classList.add('active');
  const viewer=document.getElementById('sess-viewer');
  viewer.innerHTML='<div class="sv-empty">Loading...</div>';
  fetch('/api/session/'+id).then(r=>r.json()).then(msgs=>{
    _currentMsgs=msgs;
    _currentShown=30;
    const s=sessionsData.find(x=>x.id===id)||{};
    viewer.innerHTML=`
      <div class="sv-header">
        <h3>${esc(s.date||'')} · ${msgs.length} messages</h3>
        <div class="sv-search">
          <input type="text" id="sv-filter" placeholder="Filter this session..." />
        </div>
      </div>
      <div class="sv-messages" id="sv-msgs">
        ${renderMessages(msgs,'',_currentShown)}
      </div>`;
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
  if(tail>0 && !q && filtered.length>tail){
    const hidden=filtered.length-tail;
    loadBtn=`<div style="text-align:center;padding:12px 0;border-bottom:1px solid var(--border);margin-bottom:8px">
      <button onclick="loadEarlier()" style="font-family:inherit;font-size:12px;padding:6px 16px;border-radius:6px;border:1px solid var(--border);background:var(--surface2);color:var(--muted);cursor:pointer">
        Load earlier messages (${hidden} hidden)
      </button>
    </div>`;
    filtered=filtered.slice(-tail);
  }
  return loadBtn+filtered.map(m=>`
    <div class="sv-msg">
      <div class="sv-msg-head">
        ${roleTag(m.role)}
        <span class="sv-msg-ts">${esc(m.timestamp)}</span>
      </div>
      <div class="sv-msg-text">${filter?hl(m.text,filter):esc(m.text)}</div>
    </div>`).join('');
}
function openSession(id){
  document.querySelectorAll('.nav-item').forEach(b=>b.classList.remove('active'));
  document.querySelectorAll('section').forEach(s=>s.classList.remove('active'));
  document.querySelector('[data-tab="sessions"]').classList.add('active');
  document.getElementById('sessions').classList.add('active');
  const item=document.querySelector(`.sl-item[data-id="${id}"]`);
  selectSession(id,item);
  if(item)item.scrollIntoView({block:'center'});
}

fetch('/api/skills').then(r=>r.json()).then(data=>{
  const el=document.getElementById('skills');
  el.innerHTML=`<div class="section-header"><h2>Skills</h2><span class="count">${data.length}</span></div>
    <div class="grid">${data.map(s=>`<div class="grid-item">
      <div class="gi-name">${esc(s.name)}</div>
      <div class="gi-desc">${esc(s.description||'')}</div>
    </div>`).join('')}</div>`;
});

fetch('/api/settings').then(r=>r.json()).then(data=>{
  const el=document.getElementById('settings');
  el.innerHTML=Object.entries(data).map(([title,obj])=>`
    <div class="section-header"><h2>${esc(title)}</h2></div>
    ${Object.keys(obj).length?`<div class="card"><div class="card-body">${esc(JSON.stringify(obj,null,2))}</div></div>`
    :'<div class="empty-state">Empty</div>'}
  `).join('');
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
        "file": path.name,
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
    return core_search.load_session_messages(path)


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
        for c in range(1, counter + 1):
            sid = str(uuid.uuid5(SESSION_NAMESPACE, f"{conv_key}-{c}"))
            info = _session_info(sid)
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
        if not self._check_auth():
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error":"unauthorized"}')
            return

        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)

        if path == "/api/memories":
            memories = []
            if MEMORY_DIR.is_dir():
                for f in sorted(MEMORY_DIR.glob("*.md")):
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
        elif path == "/api/lark_chats":
            self._json(lark_chats())
        elif path == "/api/settings":
            self._json(load_settings())
        else:
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(HTML.encode())

    def _json(self, data):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode())

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
