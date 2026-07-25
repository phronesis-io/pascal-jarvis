#!/usr/bin/env bash
# Daily conversation audit runner (REQ-82).
#
# The audit engine (core/conversation_audit.py) is a pure-regex pass over
# jarvis.log + session transcripts — zero LLM cost — but it had NO scheduler
# mount anywhere: the last run was a manual CLI call on 6/18, a 13-day
# blind spot in the very tool that watches the heartbeat. It runs here as an
# independent launchd cron (com.jarvis.conversation-audit, 04:20 daily),
# NOT as a heartbeat task: Tier0's run_script 60s hard timeout is borderline
# for a full transcript sweep, and the audit must not share fate with the
# heartbeat it is auditing. Freshness is watched via the latest successful
# audit_runs.completed_at value in components.yaml (48h).
#
# default_paths() resolves everything from Path.cwd()
# (core/conversation_audit.py) — the cd below is load-bearing.
#
# --hours 25: one hour of overlap so a slightly-late launchd fire never
# leaves an unaudited gap between consecutive daily runs.

set -u

REPO_DIR="${JARVIS_DIR:-/Users/pascal/Desktop/jarvis/repos/pascal-jarvis}"
cd "$REPO_DIR" || { echo "[conversation-audit] cannot cd $REPO_DIR" >&2; exit 1; }

exec /opt/homebrew/bin/python3 -m core.conversation_audit \
  --hours 25 \
  --report data/conversation_audit_daily.md
