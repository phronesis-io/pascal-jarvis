#!/usr/bin/env bash
# Pre-hook: gather all context needed for personal site update
JARVIS_DIR="${JARVIS_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
MEMORY_DIR="${MEMORY_DIR:-$HOME/.claude/projects/-Users-pascal-Desktop-jarvis-repos-pascal-jarvis/memory}"
WORK_DIR="${WORK_DIR:-$(cd "$JARVIS_DIR/.." 2>/dev/null && pwd || echo "$JARVIS_DIR")}"
SITE_DIR="$WORK_DIR/repos/huyongyi-cpu.github.io"

# Site repo must exist
[ -d "$SITE_DIR" ] || exit 0

# Pull latest
git -C "$SITE_DIR" pull --ff-only 2>/dev/null || true

echo "=== PERSONAL SITE UPDATE ==="
echo ""

# 1. Current site content
echo "--- Current index.html (relevant sections) ---"
python3 -c "
from pathlib import Path
html = Path('$SITE_DIR/index.html').read_text()
import re
titles = re.findall(r'<h[1-3][^>]*>(.*?)</h[1-3]>', html, re.DOTALL)
for t in titles:
    clean = re.sub(r'<[^>]+>', '', t).strip()
    if clean:
        print(f'  Section: {clean}')
" 2>/dev/null || echo "Could not parse index.html"

echo ""

# 2. EigenFlux stats
echo "--- EigenFlux Stats ---"
eigenflux profile show 2>/dev/null | python3 -c "
import sys, json
d = json.load(sys.stdin)
inf = d.get('influence', {})
print(f\"  Total items: {inf.get('total_items', '?')}\")
print(f\"  Total consumed: {inf.get('total_consumed', '?')}\")
" 2>/dev/null || echo "  Could not fetch"

# 3. GitHub stats
echo ""
echo "--- GitHub Stats ---"
for repo in proactive-eval pascal-jarvis; do
  stars=$(curl -s "https://api.github.com/repos/phronesis-io/$repo" 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin).get('stargazers_count','?'))" 2>/dev/null || echo "?")
  echo "  phronesis-io/$repo: $stars stars"
done

# 4. Memory context
echo ""
echo "--- Memory Summary ---"
cat "$MEMORY_DIR/hot/user_profile.md" 2>/dev/null | head -30
echo ""
echo "--- Recent Work (from daily log) ---"
tail -30 "$MEMORY_DIR/timeline/daily_log.md" 2>/dev/null || echo "No daily log"

# 5. Whitepaper / publications
echo ""
echo "--- Publications ---"
ls "$WORK_DIR/repos/eigenflux-whitepaper/blogs/"*.md 2>/dev/null | while read f; do
  head -1 "$f"
done
