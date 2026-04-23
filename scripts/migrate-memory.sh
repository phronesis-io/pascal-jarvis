#!/usr/bin/env bash
# Migrate memory directory from flat layout to hot/warm/timeline/system structure.
#
# Usage:
#   ./scripts/migrate-memory.sh [MEMORY_DIR]
#
# If MEMORY_DIR is not specified, reads from jarvis.yaml data_dir + /memory.
# Safe to run multiple times — skips files that are already migrated.

set -euo pipefail

JARVIS_DIR="$(cd "$(dirname "$0")/.." && pwd)"

# Resolve MEMORY_DIR
if [ -n "${1:-}" ]; then
  MEMORY_DIR="$1"
else
  # Try to read from jarvis.yaml
  if [ -f "$JARVIS_DIR/jarvis.yaml" ]; then
    data_dir=$(python3 -c "
import yaml
with open('$JARVIS_DIR/jarvis.yaml') as f:
    c = yaml.safe_load(f)
print(c.get('data_dir', ''))
" 2>/dev/null || echo "")
    if [ -n "$data_dir" ]; then
      MEMORY_DIR="$data_dir/memory"
    fi
  fi
fi

if [ -z "${MEMORY_DIR:-}" ] || [ ! -d "$MEMORY_DIR" ]; then
  echo "ERROR: Could not find memory directory."
  echo "Usage: $0 [MEMORY_DIR]"
  echo "  e.g. $0 ~/.claude/projects/-Users-yourname-Desktop-jarvis/memory"
  exit 1
fi

echo "Migrating: $MEMORY_DIR"
echo ""

# Create subdirectories
mkdir -p "$MEMORY_DIR/hot" "$MEMORY_DIR/warm" "$MEMORY_DIR/timeline" "$MEMORY_DIR/system"

# ── Helper: move file if it exists at root and not yet in target ─────
migrate() {
  local src="$MEMORY_DIR/$1"
  local dst="$MEMORY_DIR/$2"
  if [ -f "$src" ] && [ ! -f "$dst" ]; then
    mv "$src" "$dst"
    echo "  ✓ $1 → $2"
  elif [ -f "$dst" ]; then
    # Already migrated; remove old copy if it still exists
    [ -f "$src" ] && rm -f "$src" && echo "  ✓ $1 (removed stale root copy)"
  fi
}

echo "Moving files..."

# Hot (always loaded)
migrate "user_profile.md" "hot/user_profile.md"
migrate "user_healing_frame.md" "hot/user_healing_frame.md"
migrate "interaction_principles.md" "hot/interaction_principles.md"

# Warm (on-demand)
migrate "health_fitness.md" "warm/health_fitness.md"
migrate "project_cultural_exploration.md" "warm/cultural_exploration.md"
migrate "investment_portfolio.md" "warm/investment_portfolio.md"
migrate "meeting_notes_wenyuan.md" "warm/meeting_notes_wenyuan.md"
migrate "project_industry_intel_liang_chenqi.md" "warm/industry_intel_liang_chenqi.md"

# Timeline
migrate "hourly_log.md" "timeline/hourly_log.md"
migrate "daily_log.md" "timeline/daily_log.md"
migrate "longterm_digest.md" "timeline/longterm_digest.md"
migrate "monthly_archive.md" "timeline/monthly_archive.md"
migrate "hourly_archive.md" "timeline/hourly_archive.md"
migrate "daily_archive.md" "timeline/daily_archive.md"
migrate "longterm_digest.bak.md" "timeline/longterm_digest.bak.md"
migrate "monthly_archive.bak.md" "timeline/monthly_archive.bak.md"

# System
migrate "todos.md" "system/todos.md"
migrate "open_threads.md" "system/open_threads.md"
migrate "pending_updates.md" "system/pending_updates.md"
migrate "checkin_log.jsonl" "system/checkin_log.jsonl"

# ── Merge feedback files into hot/feedback_rules.md ──────────────────
FEEDBACK_TARGET="$MEMORY_DIR/hot/feedback_rules.md"
if [ ! -f "$FEEDBACK_TARGET" ]; then
  echo ""
  echo "Merging feedback files into hot/feedback_rules.md..."

  cat > "$FEEDBACK_TARGET" <<'HEADER'
---
name: 行为准则合集
description: 所有交互反馈规则的合集
type: feedback
---

HEADER

  for fb in "$MEMORY_DIR"/feedback_*.md; do
    [ -f "$fb" ] || continue
    # Strip YAML frontmatter and append
    sed -n '/^---$/,/^---$/!p' "$fb" >> "$FEEDBACK_TARGET"
    echo "" >> "$FEEDBACK_TARGET"
    echo "  ✓ merged $(basename "$fb")"
  done

  # Remove old feedback files
  rm -f "$MEMORY_DIR"/feedback_*.md 2>/dev/null
  echo "  ✓ old feedback files removed"
else
  echo "  (hot/feedback_rules.md already exists, skipping merge)"
  # Still clean up old feedback files if they exist
  rm -f "$MEMORY_DIR"/feedback_*.md 2>/dev/null
fi

# ── Generate _index.md ───────────────────────────────────────────────
echo ""
echo "Generating _index.md..."

cat > "$MEMORY_DIR/_index.md" <<'EOF'
# Memory Index

> Claude: 需要 warm/ 下文件的详细内容时，用 Read 工具读取完整路径。
> 此文件由 memory-tidy 心跳任务自动更新。

## hot/ (始终加载)
EOF

for f in "$MEMORY_DIR"/hot/*.md; do
  [ -f "$f" ] || continue
  name=$(basename "$f")
  # Try to extract description from frontmatter
  desc=$(sed -n 's/^description: *//p' "$f" | head -1 | cut -c1-80)
  [ -z "$desc" ] && desc="(no description)"
  echo "- $name — $desc" >> "$MEMORY_DIR/_index.md"
done

cat >> "$MEMORY_DIR/_index.md" <<'EOF'

## warm/ (按需读取)
EOF

for f in "$MEMORY_DIR"/warm/*.md; do
  [ -f "$f" ] || continue
  name=$(basename "$f")
  desc=$(sed -n 's/^description: *//p' "$f" | head -1 | cut -c1-80)
  [ -z "$desc" ] && desc="(no description)"
  echo "- $name — $desc" >> "$MEMORY_DIR/_index.md"
done

cat >> "$MEMORY_DIR/_index.md" <<'EOF'

## system/ (运维)
EOF

for f in "$MEMORY_DIR"/system/*.md; do
  [ -f "$f" ] || continue
  name=$(basename "$f")
  echo "- $name" >> "$MEMORY_DIR/_index.md"
done

cat >> "$MEMORY_DIR/_index.md" <<'EOF'

## timeline/ (时间线)
- hourly_log.md — 今天的对话索引
- daily_log.md — 近几天的日级摘要
- longterm_digest.md — 周级摘要
- monthly_archive.md — 月级归档
EOF

echo "  ✓ _index.md generated"

# ── Clean up ─────────────────────────────────────────────────────────
# Remove .obsidian if present
rm -rf "$MEMORY_DIR/.obsidian" 2>/dev/null

echo ""
echo "Migration complete!"
echo ""
echo "Directory structure:"
echo "  $MEMORY_DIR/"
for d in hot warm timeline system; do
  count=$(ls "$MEMORY_DIR/$d/" 2>/dev/null | wc -l | tr -d ' ')
  echo "  ├── $d/  ($count files)"
done
echo "  └── _index.md"
