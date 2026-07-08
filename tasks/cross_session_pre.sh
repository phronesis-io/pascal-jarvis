#!/usr/bin/env bash
# Pre-hook: scan other Claude Code projects for recent session activity.
# Extracts user+assistant text turns from JSONL session files modified in the last 24h.
# Skips the jarvis project itself and tool_use/tool_result content blocks.
#
# High-water mark (2026-07-07): each run emits only turns NEWER than the last
# processed point per session file (watermark in
# $MEMORY_DIR/system/cross_session_seen.json). The old rolling-24h re-read fed
# the same stale morning turns to Claude up to 144x/day (10-min interval),
# which re-surfaced an already-merged "3 个 PR 等批" claim to Pascal 8 times in
# one evening. A 3-turn already-seen tail is kept per file (marked [context])
# so the digest prompt still has enough narrative to summarize coherently.
# The watermark advances at pre time: if the downstream Claude call fails,
# those turns are skipped rather than retried — a lost digest cycle beats
# re-digesting the same session. A hash of the emitted turns is kept in the
# SAME state file as a second guard against partial state damage — it does
# NOT survive a failed state write (sha and watermark are one json.dump), so
# a persistently unwritable state file WOULD re-digest every cycle. That
# condition is therefore reported loudly to stderr on every failed write
# (heartbeat captures script stderr into jarvis.log) instead of swallowed.

set -euo pipefail

PROJECTS_DIR="$HOME/.claude/projects"

if [ ! -d "$PROJECTS_DIR" ]; then
  exit 0
fi

# Watermark lives next to the digest the post-hook writes (same MEMORY_DIR
# default as tasks/cross_session_post.py).
export CROSS_SESSION_SEEN_FILE="${MEMORY_DIR:-$HOME/.jarvis/memory}/system/cross_session_seen.json"

# Find JSONL files modified in the last 24h, excluding jarvis project dirs
recent_files=$(find "$PROJECTS_DIR" -name '*.jsonl' -mtime -1 \
  -not -path '*-Users-pascal-Desktop-jarvis-repos-pascal-jarvis/*' \
  -not -path '*-Users-pascal-Desktop-jarvis/*' \
  2>/dev/null | sort -t/ -k6,6 || true)

if [ -z "$recent_files" ]; then
  exit 0
fi

# Extract turns via Python for robust JSON parsing
echo "$recent_files" | python3 -c "
import hashlib, json, sys, os
from datetime import datetime

MAX_TOTAL = 8000
CONTEXT_TAIL = 3  # already-seen turns re-shown per file, marked [context]
output_parts = []

# Load the per-file watermark. Tolerate first run / missing / corrupt state —
# worst case is a one-time full re-surface of the 24h window, then watermarked.
SEEN_FILE = os.environ.get('CROSS_SESSION_SEEN_FILE', '')
seen = {}
if SEEN_FILE and os.path.isfile(SEEN_FILE):
    try:
        with open(SEEN_FILE, encoding='utf-8') as f:
            loaded = json.load(f)
        if isinstance(loaded, dict):
            seen = loaded
    except (OSError, ValueError):
        seen = {}
files_state = seen.get('files') if isinstance(seen.get('files'), dict) else {}

for filepath in sys.stdin:
    filepath = filepath.strip()
    if not filepath or not os.path.isfile(filepath):
        continue

    # Derive project name from directory
    # Path: ~/.claude/projects/<project-slug>/<session>.jsonl
    parts = filepath.split('/')
    try:
        proj_idx = parts.index('projects') + 1
        project_name = parts[proj_idx]
        # Clean up the slug: -Users-pascal-Desktop-X -> X (last component)
        segments = project_name.split('-')
        # Find meaningful name: everything after the path prefix
        # e.g. -Users-pascal-Desktop-jarvis-repos-eigenflux -> eigenflux
        # e.g. -Users-pascal-Desktop-eigenflux-whitepaper -> eigenflux-whitepaper
        # Strategy: drop leading empty + Users + pascal + Desktop, rejoin rest
        cleaned = []
        skip = True
        for s in segments:
            if skip:
                if s.lower() in ('', 'users', 'pascal', 'desktop', 'jarvis', 'repos'):
                    continue
                skip = False
            cleaned.append(s)
        project_name = '-'.join(cleaned) if cleaned else project_name
    except (ValueError, IndexError):
        project_name = os.path.basename(os.path.dirname(filepath))

    turns = []
    try:
        with open(filepath, encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue

                msg_type = obj.get('type', '')
                if msg_type not in ('user', 'assistant'):
                    continue

                message = obj.get('message', {})
                content = message.get('content', '')
                timestamp = obj.get('timestamp', '')

                # Extract text from content
                text = ''
                if isinstance(content, str):
                    text = content.strip()
                elif isinstance(content, list):
                    text_parts = []
                    has_only_tools = True
                    for block in content:
                        btype = block.get('type', '')
                        if btype == 'text':
                            t = block.get('text', '').strip()
                            if t:
                                text_parts.append(t)
                                has_only_tools = False
                        elif btype in ('tool_use', 'tool_result', 'thinking'):
                            pass  # skip
                        else:
                            has_only_tools = False
                    if has_only_tools and not text_parts:
                        continue  # skip pure tool turns
                    text = ' '.join(text_parts)

                if not text:
                    continue

                # Truncate individual turn text
                if len(text) > 300:
                    text = text[:300] + '...'

                ts_short = ''
                if timestamp:
                    try:
                        dt = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
                        ts_short = dt.strftime('%m-%d %H:%M')
                    except Exception:
                        ts_short = timestamp[:16]

                role = 'user' if msg_type == 'user' else 'assistant'
                turns.append(f'[{project_name}] [{ts_short}] {role}: {text}')
    except Exception:
        continue

    try:
        size = os.path.getsize(filepath)
    except OSError:
        size = 0
    prev = files_state.get(filepath)
    prev_turns = prev.get('turns', 0) if isinstance(prev, dict) else 0
    prev_size = prev.get('size', 0) if isinstance(prev, dict) else 0
    # File shrank or has fewer turns than recorded = truncated/rotated session
    # — reset the watermark rather than silently skip everything forever.
    if size < prev_size or prev_turns > len(turns):
        prev_turns = 0
    new_turns = turns[prev_turns:]
    files_state[filepath] = {'turns': len(turns), 'size': size}
    if not new_turns:
        continue  # nothing new in this session — do not re-surface it

    output_parts.extend(
        '[context] ' + t for t in turns[max(0, prev_turns - CONTEXT_TAIL):prev_turns])
    # Keep last 20 NEW turns per file
    output_parts.extend(new_turns[-20:])

# Drop watermarks for session files that no longer exist on disk
files_state = {p: v for p, v in files_state.items() if os.path.isfile(p)}

# Combine all, newest project entries last, limit total chars
combined = '\n'.join(output_parts)
if len(combined) > MAX_TOTAL:
    # Trim from the beginning (older entries)
    while len(combined) > MAX_TOTAL:
        idx = combined.find('\n')
        if idx == -1:
            combined = combined[:MAX_TOTAL]
            break
        combined = combined[idx + 1:]

# Source-identity dedup: if the watermark write failed last run, this run
# re-derives the exact same turns — suppress instead of re-digesting them
# (2026-07-07: unchanged morning session was re-digested 5x in 40 minutes).
emitted_sha = hashlib.sha256(combined.encode('utf-8')).hexdigest() if combined.strip() else ''
if emitted_sha and emitted_sha == seen.get('last_emitted_sha', ''):
    combined = ''

new_state = {'files': files_state}
if emitted_sha:
    new_state['last_emitted_sha'] = emitted_sha
elif seen.get('last_emitted_sha'):
    new_state['last_emitted_sha'] = seen['last_emitted_sha']

if SEEN_FILE:
    try:
        os.makedirs(os.path.dirname(SEEN_FILE), exist_ok=True)
        tmp = SEEN_FILE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(new_state, f, ensure_ascii=False)
        os.replace(tmp, SEEN_FILE)
    except OSError as e:
        # Loud on purpose: the emission sha lives in this same file, so a
        # failed write loses BOTH guards — every cycle re-digests the same
        # turns until someone fixes the path (the 2026-07-07 8-pushes loop).
        # Heartbeat captures script stderr into jarvis.log; degrade, don't die.
        print(f'[cross-session] WATERMARK UNWRITABLE at {SEEN_FILE}: {e} — '
              f're-digest loop risk every cycle until fixed', file=sys.stderr)

if combined.strip():
    print(combined)
else:
    # No new turns anywhere -> empty pre output; heartbeat records a healthy
    # empty_pre and skips the task this cycle.
    sys.exit(0)
"
