#!/usr/bin/env bash
# Pre-hook: fetch FULL bodies for not-yet-triaged emails and print them as the
# DATA block for the mail-triage prompt. Pure read of the inbox buffer + network
# fetch; prints nothing when there's no new mail (keeps the cycle cheap).
JARVIS_DIR="${JARVIS_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
MEMORY_DIR="${MEMORY_DIR:-$JARVIS_DIR/memory}"

cd "$JARVIS_DIR" || exit 0
JARVIS_DIR="$JARVIS_DIR" MEMORY_DIR="$MEMORY_DIR" python3 tasks/mail_triage_lib.py 2>/dev/null
