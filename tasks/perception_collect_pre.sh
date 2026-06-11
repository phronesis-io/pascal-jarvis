#!/usr/bin/env bash
# Pre-hook: run one perception collection pass (deterministic, no LLM).
# Output is a one-line summary; Claude only reacts to persistent errors.
JARVIS_DIR="${JARVIS_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
MEMORY_DIR="${MEMORY_DIR:-$JARVIS_DIR/memory}"

cd "$JARVIS_DIR" || exit 0
JARVIS_DIR="$JARVIS_DIR" MEMORY_DIR="$MEMORY_DIR" python3 -m core.perception 2>&1
