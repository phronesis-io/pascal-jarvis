#!/bin/bash
# Stable Jarvis entry point for the external L2 task system.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export TASKLINE_PROJECT="${TASKLINE_PROJECT:-pascal-jarvis}"
cd "$ROOT"
exec taskline "$@"
