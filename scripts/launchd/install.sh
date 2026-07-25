#!/usr/bin/env bash
# Install/verify the launchd supervision config (REQ-40).
#
# The plist files in this directory are TEMPLATES with __JARVIS_DIR__,
# __WORK_DIR__, and __HOME__ placeholders. This script substitutes them
# with real paths at install time — no hardcoded user paths in tracked code.
#
# TCC hard constraints on this Mac (memory: jarvis-launchd-tcc-gotchas):
#  1. StandardOut/ErrorPath must NOT point under ~/Desktop (launchd has no
#     Desktop TCC permission → exit 78, zero logs). Use /tmp/jarvis-*.log.
#  2. Interpreter must be /opt/homebrew/bin/python3 (system python lacks
#     deps AND Homebrew python carries the Desktop TCC grant).
#  3. bash scripts cannot be ProgramArguments directly (no Desktop TCC) —
#     wrap with python3 -c 'subprocess.call(["/bin/bash", script])'.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
DEST="$HOME/Library/LaunchAgents"
UID_N=$(id -u)

JARVIS_DIR="$(cd "$HERE/../.." && pwd)"
WORK_DIR="${WORK_DIR:-$(cd "$JARVIS_DIR/../.." 2>/dev/null && pwd || echo "$JARVIS_DIR")}"

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
  target="gui/$UID_N/$label"
  if [[ "$label" == "com.pascal.jarvis.taskline" \
        && ! -x "$WORK_DIR/repos/taskline/dist/taskline-server" ]]; then
    launchctl bootout "$target" 2>/dev/null || true
    rm -f "$DEST/$name" "$DEST/$name.tmp"
    echo "skipped $name (optional Taskline binary not installed)"
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
  # Template substitution → installed copy
  sed \
    -e "s|__JARVIS_DIR__|$JARVIS_DIR|g" \
    -e "s|__WORK_DIR__|$WORK_DIR|g" \
    -e "s|__HOME__|$HOME|g" \
    "$plist" > "$DEST/$name.tmp"

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
  launchctl print "$target" >/dev/null 2>&1 \
    && echo "  ✓ $label loaded" \
    || echo "  ⚠️ $label NOT loaded"
done
fi

trap - ERR INT TERM HUP
if ! discard_rollback_copies; then
  echo "launchd definitions were updated, but rollback-copy cleanup failed" >&2
  exit 1
fi
