#!/usr/bin/env bash
# repos_sync_worker.sh — the SLOW half of repos-sync (REQ-52).
#
# Pulls every repo under ~/Desktop/jarvis/repos/ and writes a digest to
# $JARVIS_DIR/.repos_sync_product.txt (atomic). Network fetches across 12
# repos routinely exceed the heartbeat's 60s pre-script cap — the old
# monolithic pre-script timed out on 19/19 runs and the channel was dead for
# days while watermarks said everything was fine. The pre-script now just
# reads the freshest product and spawns this worker detached.
#
# Single-flight: a lock directory prevents overlapping workers.

JARVIS_DIR="${JARVIS_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
# WORK_DIR (set by heartbeat) is ~/Desktop/jarvis; standalone fallback must be
# JARVIS_DIR/../.. (the old `JARVIS_DIR/..` fallback produced repos/repos —
# an empty glob that silently reported nothing).
REPOS_DIR="${WORK_DIR:-$(cd "$JARVIS_DIR/../.." 2>/dev/null && pwd || echo "$JARVIS_DIR")}/repos"
PRODUCT="$JARVIS_DIR/.repos_sync_product.txt"
LOCK_DIR="$JARVIS_DIR/.repos_sync_worker.lock"

# Single-flight guard (mkdir is atomic). A lock older than 30min is stale
# (worker crashed) — reclaim it.
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  if [ -n "$(find "$LOCK_DIR" -maxdepth 0 -mmin +30 2>/dev/null)" ]; then
    rmdir "$LOCK_DIR" 2>/dev/null || true
    mkdir "$LOCK_DIR" 2>/dev/null || exit 0
  else
    exit 0  # another worker is running
  fi
fi
trap 'rmdir "$LOCK_DIR" 2>/dev/null' EXIT

TMP_OUT=$(mktemp)
{
echo "Repos sync (worker run $(date '+%Y-%m-%d %H:%M')):"
for repo in "$REPOS_DIR"/*/; do
  [ -d "$repo/.git" ] || continue
  name=$(basename "$repo")
  branch=$(git -C "$repo" branch --show-current 2>/dev/null || echo "?")

  # Snapshot HEAD before pull so we can show the actual incoming range.
  pre_head=$(git -C "$repo" rev-parse HEAD 2>/dev/null || echo "")
  # Snapshot remote branches before fetch to detect newly-pushed branches.
  pre_branches=$(git -C "$repo" for-each-ref --format='%(refname:short)' refs/remotes/origin 2>/dev/null | sort)

  # Fetch all refs (so we see new branches, not just current branch fast-forward).
  git -C "$repo" fetch --all --prune --quiet 2>/dev/null
  result=$(git -C "$repo" pull --ff-only 2>&1 || echo "PULL_FAILED")
  post_head=$(git -C "$repo" rev-parse HEAD 2>/dev/null || echo "")
  post_branches=$(git -C "$repo" for-each-ref --format='%(refname:short)' refs/remotes/origin 2>/dev/null | sort)

  # Detect new branches (post − pre).
  new_branches=$(comm -13 <(echo "$pre_branches") <(echo "$post_branches") | grep -v '^origin/HEAD$' || true)

  if echo "$result" | grep -q "PULL_FAILED"; then
    echo "  $name ($branch): PULL FAILED — $result"
    continue
  fi

  main_updated=false
  [ -n "$pre_head" ] && [ -n "$post_head" ] && [ "$pre_head" != "$post_head" ] && main_updated=true

  if [ "$main_updated" = false ] && [ -z "$new_branches" ]; then
    echo "  $name ($branch): up to date"
    continue
  fi

  echo ""
  echo "  === $name ($branch) ==="

  if [ "$main_updated" = true ]; then
    # Range short hash + commit count + diff stat summary
    range_count=$(git -C "$repo" rev-list --count "$pre_head..$post_head" 2>/dev/null || echo "?")
    short_pre=$(git -C "$repo" rev-parse --short "$pre_head" 2>/dev/null)
    short_post=$(git -C "$repo" rev-parse --short "$post_head" 2>/dev/null)
    echo "  $branch: $short_pre → $short_post ($range_count commits)"
    echo "  commits (newest first):"
    git -C "$repo" log "$pre_head..$post_head" \
      --pretty=format:'    %h %ad %an: %s' --date=format:'%m-%d %H:%M' 2>/dev/null
    echo ""
    # Diff stat summary — files changed, +/- lines.
    diffstat=$(git -C "$repo" diff --shortstat "$pre_head..$post_head" 2>/dev/null)
    echo "  diff: $diffstat"
    # Top 5 most-changed files for context.
    echo "  hot files:"
    git -C "$repo" diff --stat "$pre_head..$post_head" 2>/dev/null \
      | head -n -1 | sort -t'|' -k2 -nr | head -5 \
      | sed 's/^/    /'
  fi

  if [ -n "$new_branches" ]; then
    echo "  new branches (since last sync):"
    while IFS= read -r b; do
      [ -z "$b" ] && continue
      tip=$(git -C "$repo" log "$b" --pretty=format:'%h %ad %an: %s' --date=format:'%m-%d %H:%M' -1 2>/dev/null)
      # Commits not yet in main, capped at 5
      ahead=$(git -C "$repo" log "origin/main..$b" --pretty=format:'      %h %an: %s' 2>/dev/null | head -5)
      echo "    $b  ←  $tip"
      [ -n "$ahead" ] && echo "$ahead"
    done <<< "$new_branches"
  fi
done
} > "$TMP_OUT" 2>&1

mv -f "$TMP_OUT" "$PRODUCT"
