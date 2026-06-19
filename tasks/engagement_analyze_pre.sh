#!/usr/bin/env bash
# Pre-hook: read engagement_log.jsonl and compute engagement statistics.
# Outputs structured data for Claude to analyze.

JARVIS_DIR="${JARVIS_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
LOG_FILE="$JARVIS_DIR/engagement_log.jsonl"
export LOG_FILE

if [ ! -f "$LOG_FILE" ]; then
  exit 0
fi

# Count data points — need at least 10 to be meaningful
# Separator-agnostic: jq writes "type":"response" (compact) but python
# json.dumps writes "type": "response" (spaced) — the no-space grep counted
# 0 against a log with 251 spaced entries, short-circuiting the whole task
# with INSUFFICIENT_DATA.
# NOTE: no `|| echo 0` — grep -c PRINTS 0 and exits 1 on zero matches, so
# the fallback would append a second line and break the numeric comparison.
total=$(grep -cE '"type": ?"response"' "$LOG_FILE" 2>/dev/null)
[ -z "$total" ] && total=0
if [ "$total" -lt 10 ]; then
  # Output minimal info so Claude knows to reply HEARTBEAT_OK
  echo "INSUFFICIENT_DATA: only $total response data points (need 10+)"
  exit 0
fi

python3 - <<'PYEOF'
import json, sys, os
from collections import defaultdict
from datetime import datetime, timedelta

log_path = os.environ.get('ENGAGEMENT_LOG', os.environ.get('LOG_FILE', ''))

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
# Rate-eligible responses exclude 'conversation' rows (REQ-63): follow-on chat
# after a card is logged as type=response/reaction=conversation for volume, but
# it can NEVER be 'engaged', so counting it in a rate DENOMINATOR only deflates
# the rate and can flip the 7-day trend to a false ▼ that nudges auto-tuning to
# slow a healthy task. Time-of-day and trend use this filtered list.
rated_responses = [e for e in response_entries if e.get('reaction') != 'conversation']

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

# === Prompt experiment variants ===
experiment_sent = defaultdict(int)
experiment_engaged = defaultdict(int)
experiment_replied = defaultdict(int)
experiment_ignored = defaultdict(int)

for e in sent_entries:
    exp = e.get('prompt_experiment')
    var = e.get('prompt_variant')
    if exp and var:
        experiment_sent[(exp, var, e.get('source', 'unknown'))] += 1

for e in response_entries:
    exp = e.get('prompt_experiment')
    var = e.get('prompt_variant')
    if not exp or not var:
        continue
    key = (exp, var, e.get('source', 'unknown'))
    reaction = e.get('reaction')
    if reaction in ('engaged', 'late_reply'):
        experiment_replied[key] += 1
        if reaction == 'engaged':
            experiment_engaged[key] += 1
    elif reaction == 'ignored':
        experiment_ignored[key] += 1

if experiment_sent:
    print()
    print('=== PROMPT EXPERIMENT BREAKDOWN ===')
    for key in sorted(experiment_sent):
        exp, var, src = key
        sent = experiment_sent[key]
        replied = min(experiment_replied.get(key, 0), sent) if sent else experiment_replied.get(key, 0)
        engaged = experiment_engaged.get(key, 0)
        ignored = experiment_ignored.get(key, 0)
        reply_rate = (replied / sent * 100) if sent else 0
        engaged_rate = (engaged / sent * 100) if sent else 0
        print(f'{exp}/{var}/{src}: sent={sent}, replied={replied}, engaged={engaged}, ignored={ignored}, reply_rate={reply_rate:.0f}%, engaged_rate={engaged_rate:.0f}%')

# === Delivery-ack attribution (REQ-15: read receipts) ===
# Honest semantics: im.message.message_read_v1 arrives as BULK catch-up acks
# (opening the chat acks everything unread at once — real events carry 10+
# message_ids), so "acked" means "the chat was opened after this send", a
# DELIVERY/ATTENTION watermark — NOT "this content was seen and considered".
# Sends batched in one heartbeat cycle share the same message_ids
# (the cycle's send list), so per-source rows are cycle-granular.
#   never_acked high => the chat isn't being opened — delivery/timing problem;
#                       do NOT read it as content disinterest when tuning.
ack_ids = set()
reaction_ids = set()
for e in entries:
    if e.get('type') == 'read':
        ack_ids.update(e.get('message_ids') or [])
    elif e.get('type') == 'reaction' and e.get('message_id'):
        reaction_ids.add(e['message_id'])

src_tracked = defaultdict(int)
src_acked = defaultdict(int)
for e in sent_entries:
    mids = e.get('message_ids') or []
    if not mids:
        continue
    src = e.get('source', 'unknown')
    src_tracked[src] += 1
    if ack_ids.intersection(mids) or reaction_ids.intersection(mids):
        src_acked[src] += 1

if src_tracked:
    print()
    print('=== DELIVERY-ACK ATTRIBUTION (only sends carrying message_ids) ===')
    print('(acked = chat opened after send, a delivery watermark — NOT content-seen;')
    print(' never_acked high => delivery/timing problem, not content disinterest)')
    for src in sorted(src_tracked):
        n = src_tracked[src]
        r = src_acked.get(src, 0)
        print(f'{src}: tracked={n}, acked={r} ({100*r/n:.0f}%), never_acked={n-r}')

# === Time-of-day patterns ===
print()
print('=== TIME-OF-DAY ENGAGEMENT ===')
hour_engaged = defaultdict(int)
hour_total = defaultdict(int)

for e in rated_responses:
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
    day_responses = [e for e in rated_responses if e.get('ts', '').startswith(day)]
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
PYEOF
# (stderr intentionally NOT silenced: a python traceback here must surface
#  in the heartbeat log, not vanish — a silent truncation hid the entire
#  DELIVERY-ACK section once already)
