#!/usr/bin/env bash
# Pre-hook: read engagement_log.jsonl and compute engagement statistics.
# Outputs structured data for Claude to analyze.

JARVIS_DIR="${JARVIS_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
LOG_FILE="$JARVIS_DIR/engagement_log.jsonl"

if [ ! -f "$LOG_FILE" ]; then
  exit 0
fi

# Count data points — need at least 10 to be meaningful
total=$(grep -c '"type":"response"' "$LOG_FILE" 2>/dev/null || echo 0)
if [ "$total" -lt 10 ]; then
  # Output minimal info so Claude knows to reply HEARTBEAT_OK
  echo "INSUFFICIENT_DATA: only $total response data points (need 10+)"
  exit 0
fi

python3 -c "
import json, sys, os
from collections import defaultdict
from datetime import datetime, timedelta

log_path = os.environ.get('ENGAGEMENT_LOG', '$LOG_FILE')

entries = []
with open(log_path, encoding='utf-8') as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue

# Separate sent and response entries
sent_entries = [e for e in entries if e.get('type') == 'sent']
response_entries = [e for e in entries if e.get('type') == 'response']

# === Per-source engagement rate ===
source_sent = defaultdict(int)
source_engaged = defaultdict(int)
source_ignored = defaultdict(int)

for e in sent_entries:
    source_sent[e.get('source', 'unknown')] += 1

for e in response_entries:
    src = e.get('source', 'unknown')
    reaction = e.get('reaction', '')
    if reaction == 'engaged':
        source_engaged[src] += 1
    elif reaction == 'ignored':
        source_ignored[src] += 1

print('=== PER-SOURCE ENGAGEMENT ===')
all_sources = set(list(source_sent.keys()) + list(source_engaged.keys()) + list(source_ignored.keys()))
for src in sorted(all_sources):
    sent = source_sent.get(src, 0)
    engaged = source_engaged.get(src, 0)
    ignored = source_ignored.get(src, 0)
    total_responses = engaged + ignored
    rate = (engaged / total_responses * 100) if total_responses > 0 else 0
    print(f'{src}: sent={sent}, engaged={engaged}, ignored={ignored}, rate={rate:.0f}%')

# === Time-of-day patterns ===
print()
print('=== TIME-OF-DAY ENGAGEMENT ===')
hour_engaged = defaultdict(int)
hour_total = defaultdict(int)

for e in response_entries:
    ts = e.get('ts', '')
    try:
        hour = int(ts.split(' ')[1].split(':')[0])
    except (IndexError, ValueError):
        continue
    hour_total[hour] += 1
    if e.get('reaction') == 'engaged':
        hour_engaged[hour] += 1

for h in sorted(hour_total.keys()):
    total = hour_total[h]
    engaged = hour_engaged.get(h, 0)
    rate = (engaged / total * 100) if total > 0 else 0
    print(f'{h:02d}:00 — responses={total}, engaged={engaged}, rate={rate:.0f}%')

# === 7-day trend ===
print()
print('=== 7-DAY TREND ===')
now = datetime.now()
for days_ago in range(6, -1, -1):
    day = (now - timedelta(days=days_ago)).strftime('%Y-%m-%d')
    day_responses = [e for e in response_entries if e.get('ts', '').startswith(day)]
    day_engaged = sum(1 for e in day_responses if e.get('reaction') == 'engaged')
    day_total = len(day_responses)
    rate = (day_engaged / day_total * 100) if day_total > 0 else 0
    indicator = '▲' if rate >= 50 else '▼' if day_total > 0 else '-'
    print(f'{day}: total={day_total}, engaged={day_engaged}, rate={rate:.0f}% {indicator}')

# === Per-mode engagement (checkin subtypes) ===
print()
print('=== CHECKIN MODE BREAKDOWN ===')
# Checkin mode is inferred from content keywords
for e in response_entries:
    src = e.get('source', '')
    if src != 'checkin':
        continue
    content = e.get('content_head', '')
    # Simple heuristic: wellbeing keywords
    wellbeing_kw = ['身体', '感受', '状态', '休息', '睡', '累', '压力', '健康']
    is_wellbeing = any(kw in content for kw in wellbeing_kw) or '?' in content or '？' in content
    e['_mode'] = 'wellbeing' if is_wellbeing else 'connection'

mode_engaged = defaultdict(int)
mode_total = defaultdict(int)
for e in response_entries:
    mode = e.get('_mode')
    if not mode:
        continue
    mode_total[mode] += 1
    if e.get('reaction') == 'engaged':
        mode_engaged[mode] += 1

for mode in sorted(mode_total.keys()):
    total = mode_total[mode]
    engaged = mode_engaged.get(mode, 0)
    rate = (engaged / total * 100) if total > 0 else 0
    print(f'{mode}: total={total}, engaged={engaged}, rate={rate:.0f}%')

# === Raw recent entries (last 20) for Claude to inspect ===
print()
print('=== RECENT RAW ENTRIES (last 20) ===')
for e in entries[-20:]:
    print(json.dumps(e, ensure_ascii=False))
" 2>/dev/null
