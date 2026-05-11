#!/usr/bin/env bash
# Pre-hook: search YouTube + Bilibili for content Pascal might enjoy.
#
# Design: rotate through interest categories, search for candidates,
# return titles + URLs for Claude to curate. This is NOT a subscription
# model — it's discovery-based, bypassing platform recommendation algorithms.
#
# Requires: yt-dlp (brew install yt-dlp)

set -euo pipefail

JARVIS_DIR="${JARVIS_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
MEMORY_DIR="${MEMORY_DIR:-$HOME/.jarvis/memory}"
YT_DLP="${YT_DLP:-$(command -v yt-dlp 2>/dev/null || echo /opt/homebrew/bin/yt-dlp)}"
LOG_FILE="$MEMORY_DIR/system/content_recommend_log.jsonl"

# Only during waking hours
hour=$(date +%H)
if [ "$hour" -lt 9 ] || [ "$hour" -ge 23 ]; then
  exit 0
fi

if [ ! -x "$YT_DLP" ]; then
  echo "yt-dlp not found" >&2
  exit 0
fi

# ── Interest categories with search queries ──
# Each category has multiple query variants for diversity.
# Format: "category|platform|query"
# platform: yt = YouTube, bili = Bilibili
QUERIES=(
  # Philosophy
  "philosophy|yt|philosophy lecture deep dive"
  "philosophy|yt|existentialism phenomenology explained"
  "philosophy|yt|Heidegger Nietzsche lecture"
  "philosophy|yt|中国哲学 王德峰 王阳明"
  "philosophy|yt|Stoicism practical philosophy"
  "philosophy|yt|philosophy of mind consciousness"

  # AI & Agents
  "ai-agents|yt|AI agent framework multi-agent 2026"
  "ai-agents|yt|LLM agent architecture design"
  "ai-agents|yt|autonomous AI agents demo"
  "ai-agents|yt|agent-to-agent protocol communication"

  # Entrepreneurship & SaaS
  "startup|yt|SaaS startup founder advice"
  "startup|yt|AI startup product market fit"
  "startup|yt|YC startup school founder"
  "startup|yt|indie hacker bootstrapping"

  # Science & Math
  "science|yt|Veritasium science explained"
  "science|yt|3Blue1Brown math visual"
  "science|yt|physics documentary mind-blowing"
  "science|yt|complexity systems emergence"

  # Music & Vocal
  "music|yt|vocal technique singing lesson"
  "music|yt|music theory composition masterclass"
  "music|yt|classical music analysis"

  # Investment & Economics
  "investment|yt|value investing strategy analysis"
  "investment|yt|macroeconomics gold bond market 2026"
  "investment|yt|Ray Dalio economic principles"

  # Culture (film, literature, art)
  "culture|yt|film analysis cinema essay"
  "culture|yt|great books literature discussion"
  "culture|yt|art history documentary"

  # Sports / NBA
  "sports|yt|NBA Cavaliers analysis 2026"
  "sports|yt|basketball tactics breakdown film"

  # Bilibili picks (Chinese content)
  "philosophy|bili|哲学 讲座 深度"
  "ai-agents|bili|AI Agent 多智能体"
  "culture|bili|电影 深度解析"
  "science|bili|科普 数学 物理"
)

# Pick 2 random queries from different categories
n_queries=${#QUERIES[@]}
picks=()
used_categories=()

for attempt in $(seq 1 10); do
  idx=$(( RANDOM % n_queries ))
  entry="${QUERIES[$idx]}"
  cat=$(echo "$entry" | cut -d'|' -f1)

  # Skip if we already have this category
  skip=false
  for used in "${used_categories[@]+"${used_categories[@]}"}"; do
    if [ "$used" = "$cat" ]; then skip=true; break; fi
  done
  if $skip; then continue; fi

  picks+=("$entry")
  used_categories+=("$cat")
  if [ ${#picks[@]} -ge 2 ]; then break; fi
done

# ── Load past recommendations to tell Claude what was already sent ──
past_titles=""
if [ -f "$LOG_FILE" ]; then
  past_titles=$(python3 -c "
import json
entries = []
try:
    with open('$LOG_FILE', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line: continue
            try: entries.append(json.loads(line))
            except: continue
except: pass
for e in entries[-20:]:
    print(f\"[{e.get('ts','')}] {e.get('title','')[:60]}\")
" 2>/dev/null || true)
fi

# ── Search and collect candidates ──
candidates=""
for entry in "${picks[@]}"; do
  cat=$(echo "$entry" | cut -d'|' -f1)
  platform=$(echo "$entry" | cut -d'|' -f2)
  query=$(echo "$entry" | cut -d'|' -f3-)

  if [ "$platform" = "yt" ]; then
    results=$("$YT_DLP" "ytsearch8:$query" --flat-playlist \
      --print "%(title)s ||| %(webpage_url)s ||| %(view_count)s ||| %(duration_string)s" \
      2>/dev/null || true)
    if [ -n "$results" ]; then
      candidates+="
[$cat — YouTube — query: $query]
$results
"
    fi
  elif [ "$platform" = "bili" ]; then
    # Bilibili flat-playlist: get URLs + IDs, then batch extract titles
    results=$("$YT_DLP" "bilisearch5:$query" --flat-playlist -j 2>/dev/null | \
      python3 -c "
import json, sys, re
seen_bv = set()
for line in sys.stdin:
    try:
        item = json.loads(line)
        url = item.get('webpage_url', '')
        # Deduplicate by BV/av ID
        bv = re.search(r'(BV\w+|av\d+)', url)
        if bv:
            vid = bv.group(1)
            if vid in seen_bv: continue
            seen_bv.add(vid)
        title = item.get('title', url)
        print(f'{title} ||| {url} ||| NA ||| NA')
    except: continue
" 2>/dev/null || true)
    if [ -n "$results" ]; then
      candidates+="
[$cat — Bilibili — query: $query]
$results
"
    fi
  fi
done

if [ -z "$candidates" ]; then
  exit 0
fi

# ── Load interests for context ──
interests_file="$MEMORY_DIR/warm/interests.md"
interests=""
if [ -f "$interests_file" ]; then
  interests=$(cat "$interests_file" 2>/dev/null | head -30)
fi

cat <<EOF
Current time: $(date '+%H:%M %A %Y-%m-%d')

User interests:
$interests

Past recommendations (DO NOT repeat these):
$past_titles

Candidate videos:
$candidates
EOF
