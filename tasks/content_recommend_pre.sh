#!/usr/bin/env bash
# Pre-hook: search YouTube + Bilibili for content Pascal might enjoy.
#
# Design: rotate through interest categories, search for candidates,
# return titles + URLs for Claude to curate. This is NOT a subscription
# model — it's discovery-based, bypassing platform recommendation algorithms.
#
# REQ-75 (event-gate low-value proactive sources): as a STANDALONE proactive
# card this source earned 0% engagement (0/7 — pure unanswered push). Per
# Pascal's principle, every proactive message must earn its interruption, so
# the standalone push is gated OFF by default: this pre-script exits silently
# (empty output → heartbeat skips the task) unless explicitly opted in.
#
# TARGET (follow-up, not this change): batch these discoveries into the daily
# plan / daily digest instead of an immediate per-item card. The discovery
# machinery below is intentionally preserved so the digest can invoke it.
# To run it (digest pipeline or manual/testing), set CONTENT_RECOMMEND_PUSH=1.
#
# Requires: yt-dlp (brew install yt-dlp)

set -euo pipefail

JARVIS_DIR="${JARVIS_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
MEMORY_DIR="${MEMORY_DIR:-$HOME/.jarvis/memory}"
YT_DLP="${YT_DLP:-$(command -v yt-dlp 2>/dev/null || echo /opt/homebrew/bin/yt-dlp)}"
LOG_FILE="$MEMORY_DIR/system/content_recommend_log.jsonl"

# ── REQ-75 event gate: defer the standalone push unless explicitly enabled ──
# Default behavior is now "OK unless noteworthy": stay silent so the heartbeat
# skips this task. Discovery is meant to be batched into the daily digest.
if [ "${CONTENT_RECOMMEND_PUSH:-0}" != "1" ]; then
  exit 0
fi

# Only during waking hours. CONTENT_RECOMMEND_TEST_HOUR keeps tests
# deterministic without changing production behavior.
hour="${CONTENT_RECOMMEND_TEST_HOUR:-$(date +%H)}"
if [ "$hour" -lt 9 ] || [ "$hour" -ge 23 ]; then
  exit 0
fi

if [ ! -x "$YT_DLP" ]; then
  echo "yt-dlp not found" >&2
  exit 0
fi

# ── Interest categories with search queries ──
# Personal data = config, not code (2026-07-13 ruling): the query roster IS
# the user's interest profile, so it lives in the gitignored
# data/content_queries_personal.txt — one "category|platform|query" per line
# (platform: yt = YouTube, bili = Bilibili; lines starting with # ignored).
# Absent/empty file = this optional feature is unconfigured → silent no-op.
QUERIES_FILE="$JARVIS_DIR/data/content_queries_personal.txt"
QUERIES=()
if [ -f "$QUERIES_FILE" ]; then
  while IFS= read -r line; do
    case "$line" in
      ""|\#*) continue ;;
    esac
    QUERIES+=("$line")
  done < "$QUERIES_FILE"
fi
if [ ${#QUERIES[@]} -eq 0 ]; then
  # Generic starter set — deliberately impersonal so a fresh install works
  # out of the box; personalize via data/content_queries_personal.txt.
  QUERIES=(
    "science|yt|science documentary explained"
    "tech|yt|software engineering deep dive"
    "ai|yt|AI research overview 2026"
    "culture|yt|film analysis essay"
    "science|bili|科普 深度"
  )
fi

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

# ── Load taste context from memory ───────────────────────────────────────
# This keeps curation calibrated to Pascal's current memory instead of a stale
# hardcoded taste list in HEARTBEAT.md. Only safe, user-profile style files are
# considered; private/secret/inbox buffers are deliberately skipped because this
# task can produce outbound-facing recommendation copy.
taste_context=$(MEMORY_DIR="$MEMORY_DIR" python3 - <<'PY' 2>/dev/null || true
import os
from pathlib import Path

memory = Path(os.environ.get("MEMORY_DIR", "")).expanduser()
if not memory.exists():
    raise SystemExit

allowed_exact = {
    "interests.md",
    "user_profile.md",
    "health_fitness.md",
    "feedback_idle_suggestions.md",
    "feedback_canon_plus_search_lens.md",
    "feedback_philosophy_depth.md",
}
allowed_prefixes = (
    "feedback_content",
    "feedback_watch",
    "feedback_recommend",
    "taste",
    "interests",
    "preference",
)
blocked_name_parts = ("secret", "private", "inbox", "credential", "token")

def allowed(path: Path) -> bool:
    name = path.name.lower()
    if any(part in name for part in blocked_name_parts):
        return False
    if name in allowed_exact:
        return True
    return any(name.startswith(prefix) for prefix in allowed_prefixes)

files = []
for tier in ("hot", "warm"):
    root = memory / tier
    if not root.is_dir():
        continue
    for path in sorted(root.glob("*.md")):
        if allowed(path):
            files.append(path)

chars = 0
out = []
for path in files[:8]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        continue
    if not text:
        continue
    text = "\n".join(text.splitlines()[:40]).strip()
    if not text:
        continue
    block = f"## {path.relative_to(memory)}\n{text}"
    remaining = 5000 - chars
    if remaining <= 0:
        break
    if len(block) > remaining:
        block = block[:remaining].rstrip() + "\n[truncated]"
    out.append(block)
    chars += len(block)

print("\n\n".join(out))
PY
)

cat <<EOF
Current time: $(date '+%H:%M %A %Y-%m-%d')

Memory-derived taste context:
${taste_context:-"(none found; use the fallback curation criteria in the task prompt)"}

Past recommendations (DO NOT repeat these):
$past_titles

Candidate videos:
$candidates
EOF
