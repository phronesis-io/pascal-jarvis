#!/usr/bin/env bash
# Emit unseen owner-interactive Claude Code and Codex turns for the heartbeat
# digest. Parsing, redaction, managed-session filtering, and watermarks live in
# core.cross_session so the same contract also serves immediate prompt context.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"

exec python3 -m core.cross_session incremental
