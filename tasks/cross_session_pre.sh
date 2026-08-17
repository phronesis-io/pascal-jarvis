#!/usr/bin/env bash
# Emit unseen owner-interactive Claude Code and Codex turns for the heartbeat
# digest. Parsing, redaction, managed-session filtering, and watermarks live in
# core.cross_session so the same contract also serves immediate prompt context.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"

# Build the private historical index in small batches. Index failures must not
# block the recent-turn digest; both paths are rebuildable projections over the
# provider transcripts.
python3 -m core.cross_session index \
  --batch-size "${CROSS_SESSION_INDEX_BATCH_SIZE:-16}" >/dev/null || \
  printf '%s\n' '[cross-session] historical index batch failed' >&2

exec python3 -m core.cross_session incremental
