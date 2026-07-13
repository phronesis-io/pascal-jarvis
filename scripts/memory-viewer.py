#!/usr/bin/env python3
"""Jarvis Debug Dashboard v2 — memories, sessions, search, settings, skills."""

import http.server
import json
import re
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.claude_projects import auto_memory_dir, projects_root

CLAUDE_DIR = Path.home() / ".claude"
PROJECT_DIR = auto_memory_dir().parent
MEMORY_DIR = auto_memory_dir()
SKILLS_DIR = CLAUDE_DIR / "skills"
PLUGINS_DIR = CLAUDE_DIR / "plugins/marketplaces"
PORT = 3456

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Jarvis Dashboard</title>
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

/* Layout */
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

/* Section header */
.section-header {
  display: flex; align-items: baseline; justify-content: space-between;
  margin-bottom: 20px;
}
.section-header h2 { font-size: 18px; font-weight: 600; }
.section-header .count { font-size: 12px; color: var(--dim); }
section { display: none; }
section.active { display: block; }

/* Cards */
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

/* Search */
.search-bar {
  display: flex; gap: 8px; margin-bottom: 20px;
}
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

/* Search results */
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

/* Sessions */
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

/* Session viewer */
.sv-header {
  display: flex; align-items: center; justify-content: space-between;
  padding-bottom: 16px; border-bottom: 1px solid var(--border);
  margin-bottom: 16px;
}
.sv-header h3 { font-size: 14px; font-weight: 600; }
.sv-search {
  display: flex; gap: 6px;
}
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

/* Grid */
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 10px; }
.grid-item {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 8px; padding: 16px 18px; transition: border-color 0.15s;
}
.grid-item:hover { border-color: var(--border2); }
.grid-item .gi-name { font-size: 13px; font-weight: 600; margin-bottom: 6px; }
.grid-item .gi-desc { font-size: 11px; color: var(--dim); line-height: 1.5; }

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
  <button class="nav-item active" data-tab="memories">Memories</button>
  <button class="nav-item" data-tab="search">Search</button>
  <button class="nav-item" data-tab="sessions">Sessions</button>
  <button class="nav-item" data-tab="skills">Skills</button>
  <button class="nav-item" data-tab="settings">Settings</button>
</div>
<div class="main">
<section id="memories" class="active"></section>
<section id="search"></section>
<section id="sessions"></section>
<section id="skills"></section>
<section id="settings"></section>
</div>
</div>
<script>
// Nav
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

// ── Memories ──
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

// ── Search ──
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

// ── Sessions ──
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
  // If tail>0 and no filter, show only last N messages with a "load earlier" button
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
  // Switch to sessions tab and select this session
  document.querySelectorAll('.nav-item').forEach(b=>b.classList.remove('active'));
  document.querySelectorAll('section').forEach(s=>s.classList.remove('active'));
  document.querySelector('[data-tab="sessions"]').classList.add('active');
  document.getElementById('sessions').classList.add('active');
  const item=document.querySelector(`.sl-item[data-id="${id}"]`);
  selectSession(id,item);
  if(item)item.scrollIntoView({block:'center'});
}

// ── Skills ──
fetch('/api/skills').then(r=>r.json()).then(data=>{
  const el=document.getElementById('skills');
  el.innerHTML=`<div class="section-header"><h2>Skills</h2><span class="count">${data.length}</span></div>
    <div class="grid">${data.map(s=>`<div class="grid-item">
      <div class="gi-name">${esc(s.name)}</div>
      <div class="gi-desc">${esc(s.description||'')}</div>
    </div>`).join('')}</div>`;
});

// ── Settings ──
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
                    inp = json.dumps(c.get("input", {}))
                    if len(inp) > 120:
                        inp = inp[:120] + "..."
                    parts.append(f"[tool: {c.get('name', '')}({inp})]")
                elif c.get("type") == "tool_result":
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


def _load_session_messages(path: Path) -> list[dict]:
    messages = []
    with open(path, encoding="utf-8", errors="ignore") as fh:
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


def sessions_meta() -> list:
    result = []
    if not PROJECT_DIR.is_dir():
        return result
    for f in sorted(PROJECT_DIR.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            msgs = _load_session_messages(f)
            if not msgs:
                continue
            user_msgs = [m for m in msgs if m["role"] == "user"]
            result.append({
                "id": f.stem,
                "date": msgs[0]["timestamp"][:16] if msgs[0]["timestamp"] else "",
                "message_count": len(msgs),
                "first_prompt": user_msgs[0]["text"][:120] if user_msgs else "",
            })
        except Exception:
            continue
    return result


def load_full_session(session_id: str) -> list:
    path = PROJECT_DIR / f"{session_id}.jsonl"
    if not path.exists():
        return []
    return _load_session_messages(path)


def search_sessions(query: str, session_id: str = "", max_results: int = 40) -> list:
    results = []
    if not query:
        return results
    pattern = re.compile(re.escape(query), re.IGNORECASE)
    files = PROJECT_DIR.glob("*.jsonl") if not session_id else [PROJECT_DIR / f"{session_id}.jsonl"]
    for f in sorted(
        [ff for ff in files if ff.exists()],
        key=lambda p: p.stat().st_mtime, reverse=True
    ):
        try:
            messages = _load_session_messages(f)
            sid = f.stem
            date_str = messages[0]["timestamp"][:16] if messages and messages[0]["timestamp"] else ""

            for i, msg in enumerate(messages):
                if pattern.search(msg["text"]):
                    ctx_before = messages[max(0, i - 3):i]
                    ctx_after = messages[i + 1:min(len(messages), i + 4)]
                    results.append({
                        "session_id": sid,
                        "session_date": date_str,
                        "role": msg["role"],
                        "text": msg["text"][:800],
                        "timestamp": msg["timestamp"],
                        "context_before": [{"role": m["role"], "text": m["text"][:300]} for m in ctx_before],
                        "context_after": [{"role": m["role"], "text": m["text"][:300]} for m in ctx_after],
                    })
                    if len(results) >= max_results:
                        return results
        except Exception:
            continue
    return results


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
        ("Project CLAUDE.md", Path("/Users/pascal/Desktop/jarvis/CLAUDE.md")),
    ]:
        if path.exists():
            result[label] = {"content": path.read_text(encoding="utf-8")[:3000]}
    gss = CLAUDE_DIR / "sessions"
    if gss.is_dir():
        result["Global Sessions"] = {
            "count": len(list(gss.glob("*.json"))),
            "files": [f.name for f in sorted(gss.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:10]],
        }
    return result


# ── Server ───────────────────────────────────────────────────────────

class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
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


if __name__ == "__main__":
    server = http.server.HTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Jarvis Dashboard: http://localhost:{PORT}")
    server.serve_forever()
