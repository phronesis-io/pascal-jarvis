#!/usr/bin/env bash
# Jarvis Setup Wizard — non-interactive, idempotent.
#
# Intended flow:
#   1. A user clones the repo and asks their Claude Code to set it up.
#   2. Claude runs this script, which:
#        - Checks prerequisites (python3, jq, pip)
#        - Installs Python deps (pyyaml) — with a friendly fallback if the
#          system python is externally-managed (macOS/Debian modern behavior)
#        - Makes all shell scripts executable
#        - Creates jarvis.yaml from the example if missing
#        - Seeds memory/ with example templates
#        - Runs the test suite as a sanity check
#   3. Script prints clear "next steps" — what the user must fill in
#      manually (Lark credentials, EigenFlux email), and how to start the bot.
#
# This script NEVER writes secrets, NEVER pushes anything, NEVER edits
# jarvis.yaml after creating it. All config is the user's job.

set -uo pipefail

JARVIS_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$JARVIS_DIR"

# ── Pretty printing ──────────────────────────────────────────────────
_blue()  { printf "\033[1;34m%s\033[0m\n" "$*"; }
_green() { printf "\033[1;32m%s\033[0m\n" "$*"; }
_yellow() { printf "\033[1;33m%s\033[0m\n" "$*"; }
_red()   { printf "\033[1;31m%s\033[0m\n" "$*"; }

step() { echo; _blue "▸ $*"; }
ok()   { _green "  ✓ $*"; }
warn() { _yellow "  ⚠ $*"; }
err()  { _red "  ✗ $*"; }

# ── 1. Prerequisites ────────────────────────────────────────────────
step "Checking prerequisites"

need_cmd() {
  if command -v "$1" >/dev/null 2>&1; then
    ok "$1 found: $(command -v "$1")"
  else
    err "$1 NOT found"
    [ -n "${2:-}" ] && echo "     install: $2"
    MISSING_REQUIRED=1
  fi
}

need_optional() {
  if command -v "$1" >/dev/null 2>&1; then
    ok "$1 found: $(command -v "$1")"
  else
    warn "$1 not found (optional — $2)"
  fi
}

MISSING_REQUIRED=0

need_cmd python3 "  brew install python3   # macOS"
need_cmd jq      "  brew install jq        # macOS (apt install jq on Linux)"
need_cmd pip3    "  usually bundled with python3"
need_optional claude    "Claude Code CLI — install: npm i -g @anthropic-ai/claude-code"
need_optional lark-cli  "Lark plugin — install: npm i -g @larksuite/cli"

if [ "$MISSING_REQUIRED" -ne 0 ]; then
  err ""
  err "Missing required tools. Install them and re-run this script."
  exit 1
fi

# ── 2. Python deps ───────────────────────────────────────────────────
step "Installing Python dependencies"

if python3 -c "import yaml" 2>/dev/null; then
  ok "pyyaml already installed"
else
  if pip3 install -r requirements.txt >/dev/null 2>&1; then
    ok "pyyaml installed via pip"
  elif pip3 install --break-system-packages -r requirements.txt >/dev/null 2>&1; then
    ok "pyyaml installed via pip --break-system-packages (modern macOS/Debian)"
  else
    err "pip install failed. Try a virtualenv instead:"
    err "    python3 -m venv .venv"
    err "    .venv/bin/pip install -r requirements.txt"
    err "    then run this script from within the venv"
    exit 1
  fi
fi

# ── 3. Executable bits ───────────────────────────────────────────────
step "Making shell scripts executable"
chmod +x bot.sh setup.sh tasks/*.sh plugins/lark/client.sh 2>/dev/null || true
ok "bot.sh, tasks/*.sh, plugins/lark/client.sh"

# ── 4. jarvis.yaml ───────────────────────────────────────────────────
step "Checking configuration"

if [ -f jarvis.yaml ]; then
  ok "jarvis.yaml already exists — not overwriting"
else
  cp jarvis.example.yaml jarvis.yaml
  ok "jarvis.yaml created from jarvis.example.yaml"
  warn "You MUST edit jarvis.yaml before running the bot. See next steps below."
fi

# ── 5. Seed memory directory ─────────────────────────────────────────
step "Seeding memory directory"

# Figure out data_dir from the user's config
DATA_DIR=$(python3 -c "
import sys, os
sys.path.insert(0, '.')
try:
    from core.config import Config
    c = Config('jarvis.yaml')
    print(c.memory_dir)
except Exception as e:
    print('~/.jarvis/memory')
" 2>/dev/null)
DATA_DIR="${DATA_DIR/#\~/$HOME}"

mkdir -p "$DATA_DIR"
ok "memory dir: $DATA_DIR"

if [ -d examples/memory ] && [ -z "$(ls -A "$DATA_DIR" 2>/dev/null)" ]; then
  cp examples/memory/*.md "$DATA_DIR/" 2>/dev/null || true
  ok "seeded with $(ls "$DATA_DIR" 2>/dev/null | wc -l | tr -d ' ') example memory files"
  warn "Edit these files to describe yourself — they shape how the bot treats you"
else
  ok "memory dir already populated — not overwriting"
fi

# ── 6. Test suite (sanity check) ─────────────────────────────────────
step "Running test suite (sanity check)"

if command -v pytest >/dev/null 2>&1 || python3 -c "import pytest" 2>/dev/null; then
  if python3 -m pytest tests/ -q 2>&1 | tail -3; then
    ok "tests passed"
  else
    warn "some tests failed — see above"
  fi
else
  warn "pytest not installed — skipping (install with: pip install pytest)"
fi

# ── 7. Next steps ────────────────────────────────────────────────────
step "Setup complete — next steps"

cat <<'EOF'

  1. Edit jarvis.yaml
     - Set work_dir to the directory you want the bot to operate in
     - (Optional) Configure lark.user_id + lark.app_id for IM integration
       See plugins/lark/README.md for Lark app setup walkthrough
     - (Optional) Enable EigenFlux plugin — see plugins/eigenflux/README.md

  2. Customize memory files in your memory dir (listed above)
     - user_profile.md    — who you are, your role, preferences
     - interaction_principles.md — how you want the bot to talk to you
     - Add more *.md files with frontmatter (type: user|feedback|project|reference)

  3. (Optional) Authenticate external plugins:
     - Lark:      lark-cli config init && lark-cli auth login --as bot
     - EigenFlux: python3 -c "from plugins.eigenflux.client import EigenFluxClient; \
                              EigenFluxClient('eigenflux').login('you@example.com')"

  4. Start the bot:
     ./bot.sh

     If no Lark is configured, it runs in heartbeat-only mode (still does
     memory consolidation, etc) — you'll see "heartbeat-only mode" in logs.

  5. Admin dashboard (optional, enable admin.enabled: true in jarvis.yaml):
     python3 admin.py
     # open http://localhost:3456

EOF

ok "Happy hacking!"
