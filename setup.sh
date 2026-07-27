#!/usr/bin/env bash
# Jarvis Setup Wizard — non-interactive, idempotent.
#
# Intended flow:
#   1. A user clones the repo and asks their Claude Code to set it up.
#   2. Claude runs this script, which:
#        - Checks prerequisites (python3, jq, Claude Code)
#        - Installs every Python runtime/test dependency. If the selected
#          interpreter is externally managed, creates ~/.jarvis/runtime-venv.
#        - Makes all shell scripts executable
#        - Creates jarvis.yaml from the example if missing
#        - Seeds memory/ with example templates
#        - Runs the test suite as a sanity check
#   3. Script prints clear "next steps" — what the user must fill in
#      manually (Lark credentials, EigenFlux email), and how to start the bot.
#
# This script NEVER writes secrets, NEVER pushes anything, NEVER edits
# jarvis.yaml after creating it. All config is the user's job.

set -euo pipefail

JARVIS_DIR="$(cd "$(dirname "$0")" && pwd -P)"
export JARVIS_DIR
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
need_cmd claude  "  npm i -g @anthropic-ai/claude-code"
need_optional lark-cli    "Lark plugin — install: npm i -g @larksuite/cli"
need_optional eigenflux   "EigenFlux plugin — install: curl -fsSL https://www.eigenflux.ai/install.sh | sh"
need_optional gh          "GitHub CLI — required only for governed production deploys"

if [ "$MISSING_REQUIRED" -ne 0 ]; then
  err ""
  err "Missing required tools. Install them and re-run this script."
  exit 1
fi

# Use the same interpreter policy as bot/restart/doctor/launchd.
# shellcheck source=scripts/runtime_env.sh
source "$JARVIS_DIR/scripts/runtime_env.sh"
if ! "$JARVIS_PYTHON" -c \
    'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'; then
  err "Jarvis requires Python 3.10+; selected: $JARVIS_PYTHON"
  exit 1
fi

# ── 2. Python deps ───────────────────────────────────────────────────
step "Installing Python dependencies"

PIP_LOG="${TMPDIR:-/tmp}/jarvis-setup-pip.$$"
trap 'rm -f "$PIP_LOG"' EXIT

install_requirements() {
  "$1" -m pip install -r requirements-dev.txt >"$PIP_LOG" 2>&1
}

if install_requirements "$JARVIS_PYTHON"; then
  ok "runtime and verification dependencies installed with $JARVIS_PYTHON"
elif [[ "$JARVIS_PYTHON" == "$JARVIS_VENV_DIR/"* ]]; then
  cat "$PIP_LOG" >&2
  err "dependency installation failed inside $JARVIS_VENV_DIR"
  exit 1
else
  warn "selected Python cannot install packages; creating $JARVIS_VENV_DIR"
  mkdir -p "$(dirname "$JARVIS_VENV_DIR")"
  "$JARVIS_PYTHON" -m venv "$JARVIS_VENV_DIR"
  JARVIS_PYTHON="$JARVIS_VENV_DIR/bin/python3"
  export JARVIS_PYTHON
  # Re-select so PATH and the canonical interpreter path agree.
  jarvis_select_python
  if ! "$JARVIS_PYTHON" -m pip install --upgrade pip >"$PIP_LOG" 2>&1; then
    cat "$PIP_LOG" >&2
    err "failed to initialize pip in $JARVIS_VENV_DIR"
    exit 1
  fi
  if ! install_requirements "$JARVIS_PYTHON"; then
    cat "$PIP_LOG" >&2
    err "dependency installation failed inside $JARVIS_VENV_DIR"
    exit 1
  fi
  ok "runtime and verification dependencies installed in $JARVIS_VENV_DIR"
fi

if "$JARVIS_PYTHON" - <<'PYEOF'
import importlib

required = (
    "yaml", "nicegui", "pywebpush", "qrcode", "aiohttp", "fastapi",
    "lark_oapi", "pytest",
)
missing = []
for module in required:
    try:
        importlib.import_module(module)
    except Exception as exc:
        missing.append(f"{module} ({type(exc).__name__})")
if missing:
    raise SystemExit("missing or broken Python modules: " + ", ".join(missing))
PYEOF
then
  ok "all required Python modules import successfully"
else
  err "Python dependency verification failed"
  exit 1
fi
"$JARVIS_PYTHON" -m pip check
ok "Python dependency graph is consistent"

# ── 3. Executable bits ───────────────────────────────────────────────
step "Making shell scripts executable"
chmod +x bot.sh setup.sh restart.sh scripts/*.sh tasks/*.sh plugins/lark/client.sh
chmod -x scripts/config_env.sh scripts/runtime_env.sh
ok "bot.sh, setup/restart, scripts/*.sh, tasks/*.sh, plugin clients"

# ── 4. jarvis.yaml ───────────────────────────────────────────────────
step "Checking configuration"

if [ -f jarvis.yaml ]; then
  ok "jarvis.yaml already exists — not overwriting"
else
  cp jarvis.example.yaml jarvis.yaml
  ok "jarvis.yaml created from jarvis.example.yaml"
  warn "You MUST edit jarvis.yaml before running the bot. See next steps below."
fi

if [ -f sources.yaml ]; then
  ok "sources.yaml already exists — not overwriting"
else
  cp sources.example.yaml sources.yaml
  ok "sources.yaml created from sources.example.yaml (perception layer — optional, edit to enable sources)"
fi

# Install stamp: health checks (core/watermarks.py) use its mtime to give
# never-run tasks a fresh-install grace period instead of alarming on day one.
mkdir -p data
if [ ! -f data/.install_stamp ]; then
  touch data/.install_stamp
  ok "install stamp created (health checks now know this is a fresh install)"
fi

# ── 5. Seed memory directory ─────────────────────────────────────────
step "Seeding memory directory"

# Figure out data_dir from the user's config
DATA_DIR=$("$JARVIS_PYTHON" -c "
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
  # Copy tiered directory structure (hot/, warm/, system/) and root files
  cp -R examples/memory/* "$DATA_DIR/" 2>/dev/null || true
  ok "seeded with tiered memory structure (hot/ warm/ system/)"
  warn "Edit these files to describe yourself — they shape how the bot treats you"
else
  ok "memory dir already populated — not overwriting"
fi

# ── 6. Test suite (sanity check) ─────────────────────────────────────
step "Running test suite (sanity check)"

if [ "${JARVIS_SETUP_SKIP_TESTS:-0}" = "1" ]; then
  warn "tests skipped because JARVIS_SETUP_SKIP_TESTS=1"
else
  "$JARVIS_PYTHON" -m pytest tests/ -q
  ok "tests passed"
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

  3. (Optional) Set up the two built-in plugins — each has its own guided flow:

     Lark (chat with your bot on Feishu):
         npm install -g @larksuite/cli
         npx skills add larksuite/cli -y -g
         lark-cli config init --new          # creates a Lark app (browser)
         lark-cli auth login --recommend     # grants common scopes (browser)
         lark-cli auth status                # verify
         # then paste your open_id into jarvis.yaml → lark.user_id
         # full walkthrough: plugins/lark/README.md

     EigenFlux (broadcast network for AI agents):
         curl -fsSL https://www.eigenflux.ai/install.sh | sh
         eigenflux auth login --email you@example.com
         # verify OTP from email, then:
         # eigenflux auth verify --challenge-id <id> --code <code>
         # full walkthrough: plugins/eigenflux/README.md

  4. Verify the install (each FAIL prints its own fix command):
     ./scripts/doctor.sh

  5. Start the bot:
     ./bot.sh

     If no Lark is configured, it runs in heartbeat-only mode (still does
     memory consolidation, etc) — you'll see "heartbeat-only mode" in logs.

  6. (Optional, macOS) Install supervision so everything survives reboots
     and crashes — guardian daemon, dashboard, backups:
     ./scripts/launchd/install.sh
     # The plists are templates; the script fills in YOUR paths. Without
     # this step the bot only runs while your terminal session lives.

  7. Admin dashboard (optional, enable admin.enabled: true in jarvis.yaml):
     ./scripts/python.sh admin.py
     # open http://localhost:3456

  Health-check behavior on a fresh install: optional features you have not
  configured (EigenFlux, Lark sidecar, admin, launchd services) are reported
  as "○ skipped", never alarmed. Tasks that simply haven't had their first
  run yet get a grace period before any "NEVER run" warning.

EOF

ok "Happy hacking!"
