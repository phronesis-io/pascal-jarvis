#!/usr/bin/env bash
# Pre-hook for repos-sync — the FAST half (REQ-52).
#
# The old monolithic version pulled 12 repos with network fetches inline and
# exceeded the heartbeat's 60s pre-script cap on 19/19 observed runs — the
# channel was effectively dead for days. Now: the slow pulls live in
# tasks/repos_sync_worker.sh (detached, single-flight); this pre just
# (a) spawns the worker when the product is stale, and (b) emits the product
# once when it has fresh, unconsumed content. Sub-second, never times out.

JARVIS_DIR="${JARVIS_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
PRODUCT="$JARVIS_DIR/.repos_sync_product.txt"
CONSUMED="$JARVIS_DIR/.repos_sync_consumed"
WORKER="$JARVIS_DIR/tasks/repos_sync_worker.sh"
MAX_PRODUCT_AGE_MIN=300  # respawn worker when product older than ~5h

# Spawn the worker (detached) when the product is missing or stale. The
# worker's own lock makes double-spawns harmless.
if [ ! -f "$PRODUCT" ] || [ -n "$(find "$PRODUCT" -mmin +$MAX_PRODUCT_AGE_MIN 2>/dev/null)" ]; then
  nohup bash "$WORKER" >/dev/null 2>&1 &
fi

# Emit the product once per refresh: only when it is newer than the last
# consumption stamp. Otherwise empty output → heartbeat skips the task.
[ -f "$PRODUCT" ] || exit 0
if [ -f "$CONSUMED" ] && [ ! "$PRODUCT" -nt "$CONSUMED" ]; then
  exit 0
fi
cat "$PRODUCT"
touch -r "$PRODUCT" "$CONSUMED"
