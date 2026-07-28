#!/usr/bin/env bash
# Pre-hook: fetch FULL bodies for not-yet-triaged emails and print them as the
# DATA block for the mail-triage prompt. Pure read of the inbox buffer + network
# fetch; prints nothing when there's no new mail (keeps the cycle cheap).
JARVIS_DIR="${JARVIS_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
MEMORY_DIR="${MEMORY_DIR:-$JARVIS_DIR/memory}"

cd "$JARVIS_DIR" || exit 0
mail_data=$(JARVIS_DIR="$JARVIS_DIR" MEMORY_DIR="$MEMORY_DIR" \
  python3 tasks/mail_triage_lib.py 2>/dev/null)

# No new mail → print nothing, so the heartbeat skips the task entirely.
[ -n "$mail_data" ] || exit 0

printf '%s\n' "$mail_data"

# Reply-draft voice. Per-user config (jarvis.yaml mail.voice / memory
# warm/mail_voice.md), never a personality hardcoded in the repo — a fresh
# install must write in nobody's voice rather than in a stranger's.
voice=$(JARVIS_DIR="$JARVIS_DIR" MEMORY_DIR="$MEMORY_DIR" \
  python3 -c 'from core.mail_draft import voice_guidance; print(voice_guidance())' \
  2>/dev/null)
if [ -n "$voice" ]; then
  printf '\n=== VOICE (草稿语气，来自他自己的配置) ===\n%s\n' "$voice"
fi
