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

SCRIPT_DIR=$(cd "$(dirname "$0")/.." && pwd -P)
REPO_DIR="${JARVIS_DIR:-$SCRIPT_DIR}"
WORK_DIR="${WORK_DIR:-$(cd "$REPO_DIR/../.." 2>/dev/null && pwd)}"
BACKUP_BASE="${WORK_DIR}/session_backups"
TODAY=$(date '+%Y-%m-%d')
FINAL_DIR="$BACKUP_BASE/$TODAY"
BACKUP_DIR="$BACKUP_BASE/.${TODAY}.staging.$$"
FAILED=0
OLD_DIR=""
LATEST_TMP="$BACKUP_BASE/.latest.$$"

cleanup_staging() {
  if [ -n "$OLD_DIR" ] && [ -d "$OLD_DIR" ] && [ ! -d "$FINAL_DIR" ]; then
    mv "$OLD_DIR" "$FINAL_DIR" 2>/dev/null || true
  fi
  [ ! -d "$BACKUP_DIR" ] || rm -rf "$BACKUP_DIR"
  [ ! -L "$LATEST_TMP" ] || rm -f "$LATEST_TMP"
}
trap cleanup_staging EXIT
trap 'cleanup_staging; exit 130' INT TERM

mkdir -p "$BACKUP_BASE"
chmod 700 "$BACKUP_BASE"
PREVIOUS=""
if [ -L "$BACKUP_BASE/latest" ]; then
  PREVIOUS=$(cd "$BACKUP_BASE" && cd "$(readlink latest)" 2>/dev/null && pwd -P) || PREVIOUS=""
fi

mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"

# ── 1. Session transcripts — Claude Code AND Codex, every project ───────
# Hard-link unchanged files from the previous verified snapshot. This keeps
# all local history recoverable without multiplying ~2.5GB by 30 days.
if [ -d "$HOME/.claude/projects" ]; then
  mkdir -p "$BACKUP_DIR/claude_sessions"
  if [ -n "$PREVIOUS" ] && [ -d "$PREVIOUS/claude_sessions" ]; then
    rsync -a --link-dest="$PREVIOUS/claude_sessions" \
      --include='*/' --include='*.jsonl' --exclude='*' \
      "$HOME/.claude/projects/" "$BACKUP_DIR/claude_sessions/" || FAILED=1
  else
    rsync -a --include='*/' --include='*.jsonl' --exclude='*' \
      "$HOME/.claude/projects/" "$BACKUP_DIR/claude_sessions/" || FAILED=1
  fi
fi
if [ -d "$HOME/.codex/sessions" ]; then
  mkdir -p "$BACKUP_DIR/codex_sessions"
  if [ -n "$PREVIOUS" ] && [ -d "$PREVIOUS/codex_sessions" ]; then
    rsync -a --link-dest="$PREVIOUS/codex_sessions" \
      --include='*/' --include='*.jsonl' --exclude='*' \
      "$HOME/.codex/sessions/" "$BACKUP_DIR/codex_sessions/" || FAILED=1
  else
    rsync -a --include='*/' --include='*.jsonl' --exclude='*' \
      "$HOME/.codex/sessions/" "$BACKUP_DIR/codex_sessions/" || FAILED=1
  fi
fi

# ── 2. Memory directories — the most irreplaceable data in the system ───
for provider in claude Codex; do
  if [ "$provider" = "claude" ]; then
    memory_root="$HOME/.claude/projects"
    backup_provider="claude"
  else
    memory_root="$HOME/.Codex/projects"
    backup_provider="codex"
  fi
  for src in "$memory_root"/*/memory; do
    [ -d "$src" ] || continue
    slug=$(basename "$(dirname "$src")")
    dest="$BACKUP_DIR/memory/$backup_provider/$slug"
    mkdir -p "$dest"
    previous_memory="$PREVIOUS/memory/$backup_provider/$slug"
    if [ -n "$PREVIOUS" ] && [ -d "$previous_memory" ]; then
      rsync -a --link-dest="$previous_memory" "$src/" "$dest/" || FAILED=1
    else
      rsync -a "$src/" "$dest/" || FAILED=1
    fi
  done
done

# ── 3. Every SQLite DB — WAL-safe via sqlite backup API ──────────────────
mkdir -p "$BACKUP_DIR/databases"
python3 - "$REPO_DIR" "$BACKUP_DIR" <<'PYEOF' || FAILED=1
from pathlib import Path
import sqlite3
import sys

root = Path(sys.argv[1])
dest = Path(sys.argv[2])
databases = sorted(list(root.glob("*.db")) + list((root / "data").glob("*.db")))
for source_path in databases:
    rel = source_path.relative_to(root)
    target = dest / "databases" / ("__".join(rel.parts))
    with sqlite3.connect(source_path) as source:
        with sqlite3.connect(target) as destination:
            source.backup(destination)
    if rel == Path("data/jarvis.db"):
        with sqlite3.connect(source_path) as source:
            with sqlite3.connect(dest / "jarvis.db") as destination:
                source.backup(destination)
PYEOF

# ── 4. Runtime state + config ────────────────────────────────────────────
mkdir -p "$BACKUP_DIR/state"
for f in heartbeat_state.json active_sessions.json interval_overrides.json \
         interval_overrides_meta.json \
         engagement_log.jsonl sched_events.jsonl heartbeat_outbox.jsonl \
         memorials.jsonl memorial_queue.jsonl \
         calendar_event_mapping.json perception_state.json; do
  [ -f "$REPO_DIR/$f" ] && cp "$REPO_DIR/$f" "$BACKUP_DIR/state/" 2>/dev/null
done
# Preserve all private runtime data (keys, local inputs, queues, reports),
# excluding SQLite files already copied transactionally and ephemeral locks.
if [ -d "$REPO_DIR/data" ]; then
  mkdir -p "$BACKUP_DIR/state/data"
  rsync -a --exclude='*.db' --exclude='*.db-wal' --exclude='*.db-shm' \
    --exclude='*.lock' "$REPO_DIR/data/" "$BACKUP_DIR/state/data/" || FAILED=1
fi
# Nested state the flat list above can't reach (7/22 restore drill gaps):
# eigenflux cooldown/dedup state, intent-card ledger, provider gate, and
# metrics probe histories. Subdirs mirror the repo layout so a restore is
# a straight copy-back.
mkdir -p "$BACKUP_DIR/state/eigenflux" "$BACKUP_DIR/state/data/metrics"
for f in "$REPO_DIR"/eigenflux/*.json; do
  [ -f "$f" ] && cp "$f" "$BACKUP_DIR/state/eigenflux/" 2>/dev/null
done
for f in "$REPO_DIR"/data/.intent_card_ledger.jsonl \
         "$REPO_DIR"/data/provider_state.json \
         "$REPO_DIR"/data/metrics/*.jsonl; do
  case "$f" in
    */metrics/*) [ -f "$f" ] && cp "$f" "$BACKUP_DIR/state/data/metrics/" 2>/dev/null ;;
    *)           [ -f "$f" ] && cp "$f" "$BACKUP_DIR/state/data/" 2>/dev/null ;;
  esac
done
# Monthly ledger archives (memorials.YYYY-MM.jsonl, produced by
# core.memorial.rotate_ledger) — without this, rotated 批红 history exists
# only as a single working copy and ages out of the 30-day backup window.
for f in "$REPO_DIR"/memorials.[0-9][0-9][0-9][0-9]-[0-9][0-9].jsonl; do
  [ -f "$f" ] && cp "$f" "$BACKUP_DIR/state/" 2>/dev/null
done
if [ -f "$REPO_DIR/jarvis.yaml" ]; then
  cp "$REPO_DIR/jarvis.yaml" "$BACKUP_DIR/state/jarvis.yaml" && \
    chmod 600 "$BACKUP_DIR/state/jarvis.yaml"
fi

# ── 5. Code assets — branches, commits, dirty diffs, untracked drafts ────
python3 "$SCRIPT_DIR/scripts/snapshot_code_assets.py" \
  --root "$WORK_DIR" --destination "$BACKUP_DIR/code" || FAILED=1

count=$(find "$BACKUP_DIR" -type f | wc -l | tr -d ' ')
echo "[backup] $TODAY: $count files → $BACKUP_DIR (failed=$FAILED)"

# ── 6. Verify before promotion, stamp, latest switch, or pruning ─────────
if [ "$FAILED" -eq 0 ]; then
  python3 "$SCRIPT_DIR/scripts/verify_backup.py" "$BACKUP_DIR" --write \
    --source "$REPO_DIR" --home "$HOME" || FAILED=1
fi
if [ "$FAILED" -eq 0 ]; then
  OLD_DIR="$BACKUP_BASE/.${TODAY}.previous.$$"
  [ ! -d "$FINAL_DIR" ] || mv "$FINAL_DIR" "$OLD_DIR" || FAILED=1
  if [ "$FAILED" -eq 0 ]; then
    mv "$BACKUP_DIR" "$FINAL_DIR" || FAILED=1
    BACKUP_DIR="$BACKUP_BASE/.promoted-no-staging"
  fi
  if [ "$FAILED" -eq 0 ]; then
    # Switch ``latest`` atomically, then stamp monitoring. Neither failure is
    # allowed to claim success or authorize retention cleanup.
    ln -s "$FINAL_DIR" "$LATEST_TMP" && \
      python3 - "$LATEST_TMP" "$BACKUP_BASE/latest" <<'PYEOF' || FAILED=1
import os
import sys

# Unlike BSD ``mv``, os.replace does not follow a destination symlink that
# points at a directory. The switch is atomic and never writes inside the
# snapshot it is trying to select.
os.replace(sys.argv[1], sys.argv[2])
PYEOF
  fi
  if [ "$FAILED" -eq 0 ]; then
    stamp_tmp="$REPO_DIR/.last_backup_ok.tmp.$$"
    date '+%Y-%m-%dT%H:%M:%S' > "$stamp_tmp" && \
      mv -f "$stamp_tmp" "$REPO_DIR/.last_backup_ok" || FAILED=1
  fi
  if [ "$FAILED" -eq 0 ]; then
    [ ! -d "$OLD_DIR" ] || rm -rf "$OLD_DIR"
    # Retention is destructive, so it runs only after a complete verified
    # replacement snapshot is promoted.
    find "$BACKUP_BASE" -maxdepth 1 -type d -name '20??-??-??' \
      -mtime +30 -exec rm -rf {} \; 2>/dev/null
  elif [ -d "$OLD_DIR" ] && [ ! -d "$FINAL_DIR" ]; then
    mv "$OLD_DIR" "$FINAL_DIR"
  fi
fi

exit "$FAILED"
