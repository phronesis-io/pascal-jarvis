#!/usr/bin/env bash
# Pre-hook: git pull all repos under ~/Desktop/jarvis/repos/
REPOS_DIR="/Users/pascal/Desktop/jarvis/repos"

echo "Repos sync:"
for repo in "$REPOS_DIR"/*/; do
  [ -d "$repo/.git" ] || continue
  name=$(basename "$repo")
  branch=$(git -C "$repo" branch --show-current 2>/dev/null || echo "?")
  result=$(git -C "$repo" pull --ff-only 2>&1 || echo "PULL_FAILED")
  if echo "$result" | grep -q "Already up to date"; then
    echo "  $name ($branch): up to date"
  elif echo "$result" | grep -q "PULL_FAILED"; then
    echo "  $name ($branch): ⚠️ pull failed — $result"
  else
    echo "  $name ($branch): updated"
    echo "$result" | head -5
  fi
done
