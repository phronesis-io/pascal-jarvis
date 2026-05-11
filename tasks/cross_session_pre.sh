#!/usr/bin/env bash
# Pre-hook: scan other Claude Code projects for recent session activity.
# Extracts user+assistant text turns from JSONL session files modified in the last 24h.
# Skips the jarvis project itself and tool_use/tool_result content blocks.

set -euo pipefail

PROJECTS_DIR="$HOME/.claude/projects"

if [ ! -d "$PROJECTS_DIR" ]; then
  exit 0
fi

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
import json, sys, os
from datetime import datetime

MAX_TOTAL = 4000
output_parts = []

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

    # Keep last 20 turns per file
    turns = turns[-20:]
    output_parts.extend(turns)

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

if combined.strip():
    print(combined)
else:
    # No meaningful turns found
    sys.exit(0)
"
