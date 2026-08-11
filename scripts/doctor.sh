#!/usr/bin/env bash
# doctor.sh — one-command health/config check for Jarvis setup.
#
# Designed for AGENT-DRIVEN installs: every check prints PASS/WARN/FAIL with
# the exact fix command, so an AI assistant can loop run→fix→rerun until
# green. Exit code: 0 = no FAILs (WARNs allowed), 1 = at least one FAIL.
#
# Usage: ./scripts/doctor.sh

JARVIS_DIR="$(cd "$(dirname "$0")/.." && pwd)"
export JARVIS_DIR
# shellcheck source=runtime_env.sh
source "$JARVIS_DIR/scripts/runtime_env.sh"
cd "$JARVIS_DIR" || exit 1

PASS=0; WARN=0; FAIL=0

ok()   { printf '\033[0;32m  PASS\033[0m  %s\n' "$1"; PASS=$((PASS+1)); }
warn() { printf '\033[0;33m  WARN\033[0m  %s\n      fix: %s\n' "$1" "$2"; WARN=$((WARN+1)); }
bad()  { printf '\033[0;31m  FAIL\033[0m  %s\n      fix: %s\n' "$1" "$2"; FAIL=$((FAIL+1)); }
section() { printf '\n\033[1m%s\033[0m\n' "$1"; }

# ── 1. Core dependencies (required) ──────────────────────────────────
section "1/6 Core dependencies"

if command -v python3 >/dev/null 2>&1; then
  _pyv=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
  case "$_pyv" in
    3.1[0-9]|3.[2-9][0-9]) ok "python3 $_pyv" ;;
    *) bad "python3 $_pyv too old (need 3.10+)" "brew install python3 / apt install python3.11" ;;
  esac
  ok "Jarvis interpreter: $JARVIS_PYTHON"
else
  bad "python3 not found" "brew install python3 (macOS) / apt install python3 (Linux)"
fi

command -v jq >/dev/null 2>&1 \
  && ok "jq $(jq --version 2>/dev/null)" \
  || bad "jq not found" "brew install jq / apt install jq"

_missing_modules=$(python3 - <<'PYEOF' 2>/dev/null || true
import importlib

required = (
    "yaml", "nicegui", "aiohttp", "fastapi",
    "lark_oapi", "pytest",
)
missing = []
for module in required:
    try:
        importlib.import_module(module)
    except Exception:
        missing.append(module)
print(",".join(missing))
PYEOF
)
if [ -z "$_missing_modules" ]; then
  ok "all runtime/test Python modules import"
else
  bad "missing or broken Python modules: $_missing_modules" \
      "./scripts/python.sh -m pip install -r requirements-dev.txt"
fi
if python3 -m pip check >/dev/null 2>&1; then
  ok "Python dependency graph is consistent"
else
  bad "Python dependency conflicts detected" "./scripts/python.sh -m pip check"
fi

if command -v claude >/dev/null 2>&1; then
  ok "claude CLI $(claude --version 2>/dev/null | head -1)"
  # Auth check: a trivial prompt should succeed if logged in
  if printf 'Say OK' | claude -p --model haiku --no-session-persistence \
       --dangerously-skip-permissions >/dev/null 2>&1; then
    ok "claude CLI is authenticated (test prompt succeeded)"
  else
    bad "claude CLI not authenticated (test prompt failed)" \
        "run: claude   (then /login — needs a Claude subscription or API billing)"
  fi
else
  bad "claude CLI not found" "install Claude Code: https://claude.com/claude-code then run 'claude' and /login"
fi

command -v node >/dev/null 2>&1 \
  && ok "node $(node --version 2>/dev/null)" \
  || warn "node/npm not found — needed to install claude CLI and lark-cli" \
          "brew install node / apt install nodejs npm"

command -v gh >/dev/null 2>&1 \
  && ok "gh $(gh --version 2>/dev/null | head -1)" \
  || warn "GitHub CLI missing — foreground/local operation works, governed restart.sh does not" \
          "optional for production deploy: brew install gh && gh auth login"

if [ -f "$JARVIS_DIR/scripts/run_with_timeout.py" ]; then
  ok "portable timeout runner available"
else
  bad "portable timeout runner missing" \
      "restore scripts/run_with_timeout.py from the repository"
fi

# Gate for config checks below: without python3+PyYAML every probe in
# sections 2-3 would mislead (empty command substitutions read as PASS,
# import errors read as 'broken YAML').
PYCHECK_OK=1
if ! command -v python3 >/dev/null 2>&1 || ! python3 -c "import yaml" 2>/dev/null; then
  PYCHECK_OK=0
fi

# ── 2. Config files ──────────────────────────────────────────────────
section "2/6 Config files"

if [ "$PYCHECK_OK" -ne 1 ]; then
  warn "skipping config-file parsing checks" "fix the Python dependency FAILs in section 1 first, then re-run"
elif [ -f jarvis.yaml ]; then
  ok "jarvis.yaml exists"
  _missing=$(python3 - <<'PYEOF'
import sys
sys.path.insert(0, ".")
try:
    from core.config import Config
    c = Config("jarvis.yaml")
    problems = []
    if not c.data_dir:
        problems.append("data_dir")
    if not c.work_dir:
        problems.append("work_dir")
    print(",".join(problems))
except Exception as e:
    print(f"PARSE_ERROR:{e}")
PYEOF
)
  case "$_missing" in
    "") ok "jarvis.yaml parses; data_dir/work_dir set" ;;
    PARSE_ERROR*) bad "jarvis.yaml does not parse: ${_missing#PARSE_ERROR:}" "fix the YAML syntax; compare with jarvis.example.yaml" ;;
    *) bad "jarvis.yaml missing required fields: $_missing" "edit jarvis.yaml; see jarvis.example.yaml for the reference values" ;;
  esac
else
  bad "jarvis.yaml missing" "cp jarvis.example.yaml jarvis.yaml && edit it (or run ./setup.sh)"
fi

if [ "$PYCHECK_OK" -ne 1 ]; then
    : # sources.yaml also needs PyYAML — covered by the skip note above
elif [ -f sources.yaml ]; then
  python3 -c "import yaml; yaml.safe_load(open('sources.yaml'))" 2>/dev/null \
    && ok "sources.yaml exists and parses (perception layer)" \
    || bad "sources.yaml does not parse" "fix YAML syntax; compare with sources.example.yaml"
else
  warn "sources.yaml missing — perception layer disabled" \
       "cp sources.example.yaml sources.yaml (optional; bot works without it)"
fi

# ── 3. Lark plugin (optional — phone IM bridge) ─────────────────────
section "3/6 Lark/Feishu plugin (optional)"

_user_id=""
if [ "$PYCHECK_OK" -eq 1 ]; then
  _user_id=$(python3 -c "
import sys; sys.path.insert(0, '.')
from core.config import Config
print(Config('jarvis.yaml').lark.get('user_id', ''))" 2>/dev/null)
fi

if [ -z "$_user_id" ]; then
  warn "lark.user_id not set — bot runs HEADLESS (heartbeat-only, no IM)" \
       "optional: see plugins/lark/README.md, then put your open_id in jarvis.yaml"
else
  ok "lark.user_id configured"
  if command -v lark-cli >/dev/null 2>&1; then
    ok "lark-cli $(lark-cli --version 2>/dev/null | head -1)"
    if lark-cli config show >/dev/null 2>&1; then
      ok "lark-cli has an app profile"
    else
      bad "lark-cli not configured" "lark-cli config init --new   (browser flow, creates the Lark app)"
    fi
    # Auth probe (read-only, verified against a live install): +chat-list
    # exercises the bot token against the real API
    if lark-cli im +chat-list --page-size 1 --as bot >/dev/null 2>&1; then
      ok "lark-cli bot auth works (API probe succeeded)"
    else
      bad "lark-cli bot API probe failed (auth/scopes)" \
          "lark-cli auth login --recommend   (browser flow, grants scopes)"
    fi
  else
    bad "lark-cli not found but lark.user_id is set" \
        "npm install -g @larksuite/cli && npx skills add larksuite/cli -y -g"
  fi

  # Card-callback sidecar (optional sub-feature)
  _secret=$(python3 -c "
import sys; sys.path.insert(0, '.')
from core.config import Config
print('yes' if Config('jarvis.yaml').lark.get('app_secret') else '')" 2>/dev/null)
  _backend=$(python3 -c "
import sys; sys.path.insert(0, '.')
from core.config import Config
print(Config('jarvis.yaml').lark.get('event_backend', ''))" 2>/dev/null)
  if [ "$_backend" = "sidecar" ]; then
    if [ -n "$_secret" ] && python3 -c "import lark_oapi" 2>/dev/null; then
      ok "card-callback sidecar configured (event_backend=sidecar, secret set, lark_oapi installed)"
    else
      [ -z "$_secret" ] && bad "event_backend=sidecar but lark.app_secret missing" \
        "dev console (open.feishu.cn) → 凭证与基础信息 → copy App Secret into jarvis.yaml lark.app_secret"
      python3 -c "import lark_oapi" 2>/dev/null || bad "lark_oapi not installed" "./scripts/python.sh -m pip install lark-oapi"
    fi
  else
    warn "card buttons disabled (event_backend not 'sidecar') — text/emoji fallbacks active" \
         "optional: docs/INSTALL.md §4 explains enabling card buttons (needs App Secret + console callback config)"
  fi
fi

# ── 4. EigenFlux plugin (optional — agent broadcast network) ────────
section "4/6 EigenFlux plugin (optional)"

if command -v eigenflux >/dev/null 2>&1; then
  ok "eigenflux CLI installed"
  # Retry once: a transient network blip must not read as "not authed"
  if eigenflux profile show >/dev/null 2>&1 || { sleep 2; eigenflux profile show >/dev/null 2>&1; }; then
    ok "eigenflux authenticated"
  else
    warn "eigenflux CLI present but not authenticated (or eigenflux.ai unreachable)" \
         "eigenflux auth login --email you@example.com   (email OTP)"
  fi
else
  warn "eigenflux CLI not installed — EigenFlux tasks will be skipped" \
       "optional: curl -fsSL https://www.eigenflux.ai/install.sh | sh"
fi

# ── 5. Repo sanity ───────────────────────────────────────────────────
section "5/6 Repo sanity"

bash -n bot.sh 2>/dev/null && ok "bot.sh syntax OK" || bad "bot.sh syntax error" "git status / git checkout bot.sh — local edits broke it"
[ -x setup.sh ] && ok "setup.sh executable" || warn "setup.sh not executable" "chmod +x setup.sh *.sh tasks/*.sh"
if python3 -m pytest tests/ -q -x --co >/dev/null 2>&1; then
  ok "test suite collects (run: python3 -m pytest tests/ -q)"
else
  warn "pytest missing or tests fail to collect" "./scripts/python.sh -m pip install -r requirements-dev.txt && ./scripts/python.sh -m pytest tests/ -q"
fi

# ── 6. Runtime (only meaningful after first start) ───────────────────
section "6/6 Runtime (skip on first install)"

if pgrep -f "bash.*$JARVIS_DIR/bot\.sh" >/dev/null 2>&1; then
  ok "bot.sh running"
  pgrep -f "lark-cli event|lark_event_sidecar" >/dev/null 2>&1 \
    && ok "event listener running" \
    || warn "bot up but no event listener (headless mode, or starting up)" "./restart.sh --status for details"
  if [ -f jarvis.log ] && grep -q "Beat sent" jarvis.log 2>/dev/null; then
    ok "heartbeat has beaten"
  else
    warn "no heartbeat beat found yet" "wait ~30s after start, then: grep 'Beat sent' jarvis.log | tail -1"
  fi
  _component_report=$(python3 -m core.components 2>&1) || _component_rc=$?
  if [ "${_component_rc:-0}" -eq 0 ]; then
    ok "all configured components healthy"
  else
    printf '%s\n' "$_component_report"
    bad "one or more configured components are unhealthy" \
        "./scripts/python.sh -m core.components"
  fi
else
  warn "bot not running (fine if you haven't started it)" \
       "./bot.sh  — supervised macOS installs can use scripts/launchd/install.sh"
fi

# ── Summary ──────────────────────────────────────────────────────────
printf '\n\033[1m=== doctor summary: %d PASS / %d WARN / %d FAIL ===\033[0m\n' "$PASS" "$WARN" "$FAIL"
if [ "$FAIL" -gt 0 ]; then
  echo "Fix the FAIL items above (each has a fix command), then re-run: ./scripts/doctor.sh"
  exit 1
fi
[ "$WARN" -gt 0 ] && echo "WARNs are optional features — the bot runs without them."
exit 0
