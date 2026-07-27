#!/usr/bin/env bash
# Install/verify the launchd supervision config (REQ-40).
#
# The plist files in this directory are templates. This script renders the
# selected Python, repo, work, Taskline, and home paths at install time.
#
# TCC hard constraints on this Mac (memory: jarvis-launchd-tcc-gotchas):
#  1. StandardOut/ErrorPath must NOT point under ~/Desktop (launchd has no
#     Desktop TCC permission → exit 78, zero logs). Use /tmp/jarvis-*.log.
#  2. Interpreter must be the exact Python validated by setup/doctor. On
#     Pascal's Mac that is Homebrew Python because it carries the Desktop TCC
#     grant; portable installs may use another absolute path or the managed
#     ~/.jarvis/runtime-venv.
#  3. bash scripts cannot be ProgramArguments directly (no Desktop TCC) —
#     wrap with python3 -c 'subprocess.call(["/bin/bash", script])'.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
DEST="$HOME/Library/LaunchAgents"
UID_N=$(id -u)

JARVIS_DIR="$(cd "$HERE/../.." && pwd)"
export JARVIS_DIR
# shellcheck source=../runtime_env.sh
source "$JARVIS_DIR/scripts/runtime_env.sh"
PYTHON_BIN="$JARVIS_PYTHON"
PYTHON_DIR="$JARVIS_PYTHON_DIR"
CONFIG_FILE="${JARVIS_CONFIG_FILE:-$JARVIS_DIR/jarvis.yaml}"

mkdir -p "$DEST"

if [ -z "${WORK_DIR:-}" ]; then
  if [ -f "$CONFIG_FILE" ]; then
    WORK_DIR=$(
      "$PYTHON_BIN" - "$JARVIS_DIR" "$CONFIG_FILE" <<'PYEOF'
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from core.config import Config

print(Path(Config(sys.argv[2]).work_dir).expanduser().resolve())
PYEOF
    )
  else
    WORK_DIR="$JARVIS_DIR"
  fi
fi
if [ ! -d "$WORK_DIR" ]; then
  echo "configured work_dir does not exist: $WORK_DIR" >&2
  exit 2
fi
WORK_DIR="$(cd "$WORK_DIR" && pwd -P)"

TASKLINE_DIR="${TASKLINE_DIR:-$JARVIS_DIR/../taskline}"
if [ -d "$TASKLINE_DIR" ]; then
  TASKLINE_DIR="$(cd "$TASKLINE_DIR" && pwd -P)"
fi

LAUNCHD_PROBE_DETAIL=""
launchd_job_state() {
  local target="$1"
  local detail
  if detail=$(launchctl print "$target" 2>&1); then
    LAUNCHD_PROBE_DETAIL=""
    return 0
  fi
  case "$detail" in
    *"Could not find service"*|*"could not find service"*|\
    *"Service cannot be found"*|*"service cannot be found"*)
      LAUNCHD_PROBE_DETAIL="$detail"
      return 1
      ;;
    *)
      LAUNCHD_PROBE_DETAIL="${detail:-launchctl print failed without details}"
      return 2
      ;;
  esac
}

plist_requires_resident_process() {
  "$PYTHON_BIN" - "$1" <<'PYEOF'
import plistlib
import sys

with open(sys.argv[1], "rb") as handle:
    definition = plistlib.load(handle)
raise SystemExit(0 if bool(definition.get("KeepAlive")) else 1)
PYEOF
}

LAUNCHD_RUNTIME_DETAIL=""
verify_launchd_runtime() {
  local definition="$1"
  local target="$2"
  local attempts="${JARVIS_LAUNCHD_SETTLE_ATTEMPTS:-12}"
  local interval="${JARVIS_LAUNCHD_SETTLE_INTERVAL:-0.25}"
  local required=4
  local consecutive=0
  local seen_running=0
  local unstable=0
  local detail=""
  local failure_detail=""
  local i

  if ! plist_requires_resident_process "$definition"; then
    LAUNCHD_RUNTIME_DETAIL=""
    return 0
  fi
  case "$attempts" in
    ''|*[!0-9]*) attempts=12 ;;
  esac
  if [ "$attempts" -lt 8 ]; then
    attempts=8
  fi

  for ((i=0; i < attempts; i++)); do
    if detail=$(launchctl print "$target" 2>&1) \
        && printf '%s\n' "$detail" \
          | grep -Eq 'state[[:space:]]*=[[:space:]]*running'; then
      seen_running=1
      consecutive=$((consecutive + 1))
    else
      if [ "$seen_running" -eq 1 ]; then
        unstable=1
      fi
      consecutive=0
      failure_detail="$detail"
    fi
    if [ "$i" -lt $((attempts - 1)) ]; then
      sleep "$interval"
    fi
  done

  if [ "$unstable" -eq 0 ] && [ "$consecutive" -ge "$required" ]; then
    LAUNCHD_RUNTIME_DETAIL=""
    return 0
  fi
  if [ -n "$failure_detail" ]; then
    detail="$failure_detail"
  fi
  case "$detail" in
    *"last exit code = 78"*|*"last exit status = 78"*)
      LAUNCHD_RUNTIME_DETAIL="resident process exited with status 78; macOS TCC denied runtime access. Grant the selected Python access to the repository or set JARVIS_PYTHON to an approved interpreter, then rerun setup"
      ;;
    *)
      LAUNCHD_RUNTIME_DETAIL="resident process did not remain in launchd state=running after ${attempts} probes: ${detail:-no launchctl detail}"
      ;;
  esac
  return 1
}

PLISTS=()
if [ "$#" -gt 0 ]; then
  for requested in "$@"; do
    name="${requested##*/}"
    case "$name" in
      *.plist) ;;
      *) name="$name.plist" ;;
    esac
    plist="$HERE/$name"
    if [ ! -f "$plist" ]; then
      echo "unknown launchd service: $requested" >&2
      exit 2
    fi
    PLISTS+=("$plist")
  done
else
  for plist in "$HERE"/com.*.plist; do
    PLISTS+=("$plist")
  done
fi

FILTERED_PLISTS=()
for plist in "${PLISTS[@]}"; do
  name=$(basename "$plist")
  label="${name%.plist}"
  if [[ "$label" == "com.pascal.jarvis.taskline" \
        && ! -x "$TASKLINE_DIR/dist/taskline-server" ]]; then
    echo "skipped $name (optional Taskline binary not installed; existing definition preserved)"
    continue
  fi
  FILTERED_PLISTS+=("$plist")
done
if [ "${#FILTERED_PLISTS[@]}" -gt 0 ]; then
  PLISTS=("${FILTERED_PLISTS[@]}")
else
  PLISTS=()
fi

UPDATED_NAMES=()
UPDATED_LABELS=()
UPDATED_TARGETS=()
UPDATED_WAS_LOADED=()
UPDATED_HAD_PREVIOUS=()
UPDATED_ROLLBACKS=()

rollback_updated_services() {
  local recovery_failed=0
  local recovery_detail
  local restored_file
  local probe_rc
  local i
  local name
  local label
  local target
  local was_loaded
  local had_previous
  local rollback

  if [ "${#UPDATED_NAMES[@]}" -eq 0 ]; then
    return 0
  fi

  for ((i=${#UPDATED_NAMES[@]} - 1; i >= 0; i--)); do
    name="${UPDATED_NAMES[$i]}"
    label="${UPDATED_LABELS[$i]}"
    target="${UPDATED_TARGETS[$i]}"
    was_loaded="${UPDATED_WAS_LOADED[$i]}"
    had_previous="${UPDATED_HAD_PREVIOUS[$i]}"
    rollback="${UPDATED_ROLLBACKS[$i]}"

    launchctl bootout "$target" 2>/dev/null || true
    restored_file=1
    if [ "$had_previous" -eq 1 ]; then
      if [ ! -f "$rollback" ]; then
        echo "recovery failed for $label: rollback copy is missing" >&2
        recovery_failed=1
        restored_file=0
      elif ! mv "$rollback" "$DEST/$name"; then
        echo "recovery failed for $label: cannot restore rollback copy" >&2
        recovery_failed=1
        restored_file=0
      fi
    elif ! rm -f "$DEST/$name" "$rollback"; then
      echo "recovery failed for $label: cannot restore absent definition" >&2
      recovery_failed=1
      restored_file=0
    fi

    if [ "$restored_file" -eq 0 ]; then
      # The old file state could not be reconstructed. Keep a previously
      # loaded service available on the remaining definition, but never call
      # this a successful rollback.
      if [ "$was_loaded" -eq 1 ] && [ -f "$DEST/$name" ]; then
        launchctl bootstrap "gui/$UID_N" "$DEST/$name" >/dev/null 2>&1 || true
      fi
      continue
    fi

    if [ "$was_loaded" -eq 1 ]; then
      if ! recovery_detail=$(launchctl bootstrap \
          "gui/$UID_N" "$DEST/$name" 2>&1); then
        echo "recovery failed for $label: ${recovery_detail:-bootstrap failed}" >&2
        recovery_failed=1
      elif ! launchctl print "$target" >/dev/null 2>&1; then
        echo "recovery failed for $label: previous definition is not loaded" >&2
        recovery_failed=1
      fi
    elif launchd_job_state "$target"; then
      echo "recovery failed for $label: service should be unloaded" >&2
      recovery_failed=1
    else
      probe_rc=$?
      if [ "$probe_rc" -eq 2 ]; then
        echo "recovery failed for $label: $LAUNCHD_PROBE_DETAIL" >&2
        recovery_failed=1
      fi
    fi
  done

  return "$recovery_failed"
}

discard_rollback_copies() {
  local cleanup_failed=0
  local rollback
  if [ "${#UPDATED_ROLLBACKS[@]}" -eq 0 ]; then
    return 0
  fi
  for rollback in "${UPDATED_ROLLBACKS[@]}"; do
    rm -f "$rollback" || cleanup_failed=1
  done
  return "$cleanup_failed"
}

cleanup_selected_temps() {
  local selected
  if [ "${#PLISTS[@]}" -eq 0 ]; then
    return 0
  fi
  for selected in "${PLISTS[@]}"; do
    rm -f "$DEST/$(basename "$selected").tmp"
  done
}

rollback_after_unexpected_failure() {
  local rc="$1"
  trap - ERR INT TERM HUP
  set +e
  cleanup_selected_temps
  if rollback_updated_services; then
    echo "launchd batch failed unexpectedly; previous state restored" >&2
  else
    echo "launchd batch failed unexpectedly; recovery was incomplete" >&2
  fi
  exit "$rc"
}

trap 'rollback_after_unexpected_failure $?' ERR
trap 'rollback_after_unexpected_failure 130' INT
trap 'rollback_after_unexpected_failure 143' TERM
trap 'rollback_after_unexpected_failure 129' HUP

if [ "${#PLISTS[@]}" -gt 0 ]; then
for plist in "${PLISTS[@]}"; do
  name=$(basename "$plist")
  label="${name%.plist}"
  target="gui/$UID_N/$label"
  # Exact token replacement avoids sed metacharacter/path escaping bugs.
  "$PYTHON_BIN" - "$plist" "$DEST/$name.tmp" \
    "$JARVIS_DIR" "$WORK_DIR" "$HOME" "$PYTHON_BIN" "$PYTHON_DIR" \
    "$TASKLINE_DIR" <<'PYEOF'
import re
import sys
from pathlib import Path
from xml.sax.saxutils import escape

source, destination, jarvis, work, home, python, python_dir, taskline = sys.argv[1:]
text = Path(source).read_text(encoding="utf-8")
for token, value in {
    "__JARVIS_DIR__": jarvis,
    "__WORK_DIR__": work,
    "__HOME__": home,
    "__PYTHON_BIN__": python,
    "__PYTHON_DIR__": python_dir,
    "__TASKLINE_DIR__": taskline,
}.items():
    text = text.replace(token, escape(value))
if re.search(r"__[A-Z0-9_]+__", text):
    raise SystemExit(f"unrendered required placeholder in {source}")
Path(destination).write_text(text, encoding="utf-8")
PYEOF
  if command -v plutil >/dev/null 2>&1 \
      && ! plutil -lint "$DEST/$name.tmp" >/dev/null; then
    rm -f "$DEST/$name.tmp"
    if rollback_updated_services; then
      echo "rendered plist is invalid: $name; previous state restored" >&2
    else
      echo "rendered plist is invalid: $name; batch recovery was incomplete" >&2
    fi
    exit 1
  fi

  was_loaded=0
  if launchd_job_state "$target"; then
    was_loaded=1
  else
    probe_rc=$?
    if [ "$probe_rc" -eq 2 ]; then
      rm -f "$DEST/$name.tmp"
      echo "cannot inspect $label before update: $LAUNCHD_PROBE_DETAIL" >&2
      if ! rollback_updated_services; then
        echo "batch recovery was incomplete" >&2
      fi
      exit 1
    fi
  fi
  if [ "$was_loaded" -eq 1 ] && [ ! -f "$DEST/$name" ]; then
    rm -f "$DEST/$name.tmp"
    echo "$label is loaded without $DEST/$name; cannot guarantee rollback" >&2
    if ! rollback_updated_services; then
      echo "batch recovery was incomplete" >&2
    fi
    exit 1
  fi

  definition_changed=0
  if ! cmp -s "$DEST/$name.tmp" "$DEST/$name" 2>/dev/null; then
    definition_changed=1
  fi

  if [ "$definition_changed" -eq 1 ] || [ "$was_loaded" -eq 0 ]; then
    rollback="$DEST/$name.rollback.$$"
    had_previous=0
    if [ -f "$DEST/$name" ]; then
      cp "$DEST/$name" "$rollback"
      had_previous=1
    fi
    UPDATED_NAMES+=("$name")
    UPDATED_LABELS+=("$label")
    UPDATED_TARGETS+=("$target")
    UPDATED_WAS_LOADED+=("$was_loaded")
    UPDATED_HAD_PREVIOUS+=("$had_previous")
    UPDATED_ROLLBACKS+=("$rollback")

    if [ "$definition_changed" -eq 1 ]; then
      mv "$DEST/$name.tmp" "$DEST/$name"
      echo "installed $name"
    else
      rm -f "$DEST/$name.tmp"
      echo "up-to-date $name (was unloaded)"
    fi

    deploy_error=""
    if [ "$was_loaded" -eq 1 ] \
        && ! launchctl bootout "$target" 2>/dev/null; then
      deploy_error="bootout failed"
    elif ! deploy_detail=$(launchctl bootstrap "gui/$UID_N" "$DEST/$name" 2>&1); then
      deploy_error="${deploy_detail:-bootstrap failed}"
    elif ! launchctl print "$target" >/dev/null 2>&1; then
      deploy_error="service is not loaded after bootstrap"
    fi

    if [ -n "$deploy_error" ]; then
      if rollback_updated_services; then
        echo "failed to install $label: $deploy_error; previous state restored" >&2
      else
        echo "failed to install $label: $deploy_error; batch recovery was incomplete" >&2
      fi
      exit 1
    fi

    echo "  (re)bootstrapped $label"
  else
    rm -f "$DEST/$name.tmp"
    echo "up-to-date $name"
  fi
  if ! verify_launchd_runtime "$DEST/$name" "$target"; then
    if rollback_updated_services; then
      echo "failed to verify $label: $LAUNCHD_RUNTIME_DETAIL; previous state restored" >&2
    else
      echo "failed to verify $label: $LAUNCHD_RUNTIME_DETAIL; batch recovery was incomplete" >&2
    fi
    exit 1
  fi
  echo "  ✓ $label loaded and stable"
done
fi

trap - ERR INT TERM HUP
if ! discard_rollback_copies; then
  echo "launchd definitions were updated, but rollback-copy cleanup failed" >&2
  exit 1
fi
