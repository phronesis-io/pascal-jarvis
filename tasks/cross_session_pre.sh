#!/usr/bin/env bash
# Index owner-interactive Claude Code/Codex sessions, then emit one durable
# Memory Compiler batch spanning those sessions and eligible owner Lark turns.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"

# Build the private historical index in small batches. An index failure is
# logged; already indexed sources and eligible Lark turns can still compile.
"${JARVIS_PYTHON:-python3}" -m core.cross_session index \
  --batch-size "${CROSS_SESSION_INDEX_BATCH_SIZE:-16}" >/dev/null || \
  printf '%s\n' '[cross-session] historical index batch failed' >&2

exec "${JARVIS_PYTHON:-python3}" -m core.memory_compiler prepare \
  --batch-size "${MEMORY_COMPILER_BATCH_SIZE:-16}"
