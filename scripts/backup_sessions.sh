#!/usr/bin/env bash
# Daily backup of Claude session transcripts.
# Keeps all .jsonl session files safe from Claude Code's auto-cleanup.
# Run via launchd or cron daily.

SESSION_DIR="/Users/pascal/.claude/projects/-Users-pascal-Desktop-jarvis"
BACKUP_BASE="/Users/pascal/Desktop/jarvis/session_backups"
TODAY=$(date '+%Y-%m-%d')
BACKUP_DIR="$BACKUP_BASE/$TODAY"

mkdir -p "$BACKUP_DIR"

# Copy only new/changed files (rsync for efficiency)
rsync -a --include='*.jsonl' --exclude='*' "$SESSION_DIR/" "$BACKUP_DIR/"

# Count
count=$(ls "$BACKUP_DIR"/*.jsonl 2>/dev/null | wc -l | tr -d ' ')
echo "[session-backup] $TODAY: $count files backed up to $BACKUP_DIR"

# Cleanup old backups (keep 30 days)
find "$BACKUP_BASE" -maxdepth 1 -type d -mtime +30 -exec rm -rf {} \; 2>/dev/null

# Also maintain a "latest" symlink
ln -sfn "$BACKUP_DIR" "$BACKUP_BASE/latest"

# Protect OLD session files from accidental deletion by setting read-only.
# Skip ALL sessions referenced in active_sessions.json (bot may resume them anytime).
REPO_DIR="/Users/pascal/Desktop/jarvis/repos/pascal-jarvis"
TRACKER="$REPO_DIR/active_sessions.json"

# Get active session IDs from tracker (these must remain writable)
active_ids=""
if [ -f "$TRACKER" ]; then
  active_ids=$(python3 -c "
import json
try:
    tracker = json.load(open('$TRACKER'))
    for entry in tracker.values():
        sid = entry.get('session_id', '')
        if sid:
            print(sid)
except: pass
" 2>/dev/null)
fi

# Also skip any session modified in the last 7 days (might be resumed)
find "$SESSION_DIR" -name "*.jsonl" -perm +200 -mtime +7 | while read -r f; do
  base=$(basename "$f" .jsonl)
  skip=false
  for id in $active_ids; do
    [ "$base" = "$id" ] && skip=true && break
  done
  $skip || chmod 444 "$f"
done

protected=$(find "$SESSION_DIR" -name "*.jsonl" -perm 444 | wc -l | tr -d ' ')
echo "[session-protect] $protected old session files set to read-only (active + recent 7d excluded)"
