#!/usr/bin/env bash
# Daily backup — sessions + MEMORY + DB + runtime state (REQ-41).
#
# v1 backed up only top-level *.jsonl transcripts of ONE project slug and
# silently failed for 27 days (TCC). The irreplaceable core of the system —
# the three memory directories, data/jarvis.db (intents + bookmarks),
# heartbeat state, jarvis.yaml — had ZERO backup coverage. This version backs
# all of it up, WAL-safe for SQLite, and stamps .last_backup_ok on success so
# self-diagnostic (components.yaml: session-backup, max_age_hours 48) pages
# when backups stop working instead of nobody noticing for a month.
#
# Run via launchd (com.jarvis.session-backup, 03:00 daily). launchd TCC rules
# apply: logs to /tmp, invoked through a Homebrew-python wrapper (bash alone
# has no Desktop permission) — see scripts/launchd/.

set -u

BACKUP_BASE="/Users/pascal/Desktop/jarvis/session_backups"
REPO_DIR="/Users/pascal/Desktop/jarvis/repos/pascal-jarvis"
TODAY=$(date '+%Y-%m-%d')
BACKUP_DIR="$BACKUP_BASE/$TODAY"
FAILED=0

mkdir -p "$BACKUP_DIR"

# ── 1. Session transcripts — BOTH project slugs ─────────────────────────
for slug in "-Users-pascal-Desktop-jarvis" "-Users-pascal-Desktop-jarvis-repos" \
            "-Users-pascal-Desktop-jarvis-repos-pascal-jarvis"; do
  src="/Users/pascal/.claude/projects/$slug"
  [ -d "$src" ] || continue
  dest="$BACKUP_DIR/sessions$slug"
  mkdir -p "$dest"
  rsync -a --include='*.jsonl' --exclude='*/' --exclude='*' "$src/" "$dest/" || FAILED=1
done

# ── 2. Memory directories — the most irreplaceable data in the system ───
for slug in "-Users-pascal-Desktop-jarvis" "-Users-pascal-Desktop-jarvis-repos" \
            "-Users-pascal-Desktop-jarvis-repos-pascal-jarvis"; do
  src="/Users/pascal/.claude/projects/$slug/memory"
  [ -d "$src" ] || continue
  dest="$BACKUP_DIR/memory$slug"
  mkdir -p "$dest"
  rsync -a "$src/" "$dest/" || FAILED=1
done

# ── 3. SQLite DB — WAL-safe via .backup (a raw file copy would lose every
#      transaction still in the -wal, currently larger than the db itself) ──
if [ -f "$REPO_DIR/data/jarvis.db" ]; then
  sqlite3 "$REPO_DIR/data/jarvis.db" ".backup '$BACKUP_DIR/jarvis.db'" || FAILED=1
fi

# ── 4. Runtime state + config ────────────────────────────────────────────
mkdir -p "$BACKUP_DIR/state"
for f in heartbeat_state.json active_sessions.json interval_overrides.json \
         engagement_log.jsonl sched_events.jsonl heartbeat_outbox.jsonl \
         memorials.jsonl memorial_queue.jsonl \
         calendar_event_mapping.json perception_state.json; do
  [ -f "$REPO_DIR/$f" ] && cp "$REPO_DIR/$f" "$BACKUP_DIR/state/" 2>/dev/null
done
if [ -f "$REPO_DIR/jarvis.yaml" ]; then
  cp "$REPO_DIR/jarvis.yaml" "$BACKUP_DIR/state/jarvis.yaml" && \
    chmod 600 "$BACKUP_DIR/state/jarvis.yaml"
fi

count=$(find "$BACKUP_DIR" -type f | wc -l | tr -d ' ')
echo "[backup] $TODAY: $count files → $BACKUP_DIR (failed=$FAILED)"

# ── 5. Success stamp — the monitoring hook (components.yaml session-backup)
if [ "$FAILED" -eq 0 ]; then
  date '+%Y-%m-%dT%H:%M:%S' > "$REPO_DIR/.last_backup_ok"
fi

# Cleanup old backups (keep 30 days)
find "$BACKUP_BASE" -maxdepth 1 -type d -mtime +30 -exec rm -rf {} \; 2>/dev/null

# Also maintain a "latest" symlink
ln -sfn "$BACKUP_DIR" "$BACKUP_BASE/latest"

# ── 6. Protect OLD session files from accidental deletion (read-only) ───
# Skip ALL sessions referenced in active_sessions.json (bot may resume them).
SESSION_DIR="/Users/pascal/.claude/projects/-Users-pascal-Desktop-jarvis"
TRACKER="$REPO_DIR/active_sessions.json"

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
find "$SESSION_DIR" -name "*.jsonl" -perm +200 -mtime +7 2>/dev/null | while read -r f; do
  base=$(basename "$f" .jsonl)
  skip=false
  for id in $active_ids; do
    [ "$base" = "$id" ] && skip=true && break
  done
  $skip || chmod 444 "$f"
done

protected=$(find "$SESSION_DIR" -name "*.jsonl" -perm 444 2>/dev/null | wc -l | tr -d ' ')
echo "[session-protect] $protected old session files set to read-only (active + recent 7d excluded)"

exit "$FAILED"
