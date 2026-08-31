#!/usr/bin/env bash
# One local verification entry for humans and coding agents.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
JARVIS_DIR="$ROOT"
export JARVIS_DIR
# shellcheck source=runtime_env.sh
source "$ROOT/scripts/runtime_env.sh"
cd "$ROOT"

runtime=0
if [ "${1:-}" = "--runtime" ]; then
  runtime=1
  shift
fi
if [ "$runtime" -eq 1 ] && [ "$#" -gt 0 ]; then
  echo "[localtest] --runtime accepts no pytest arguments; run the ordinary full gate separately" >&2
  exit 2
fi

echo "[localtest] shell syntax"
bash -n bot.sh
while IFS= read -r script; do
  bash -n "$script"
done < <(find tasks scripts -type f -name '*.sh' -print)

if command -v shellcheck >/dev/null 2>&1; then
  # Same set and severity CI enforces — a narrower local check is how a red CI
  # gets quoted as a green local run.
  echo "[localtest] shellcheck (CI parity: bot.sh, restart.sh, tasks/*.sh, scripts/*.sh)"
  shellcheck -s bash -S error -e SC1090,SC1091 bot.sh
  shellcheck -s bash -S error -e SC1090,SC1091 restart.sh
  find tasks/ -name '*.sh' -not -path 'tasks/_quarantine/*' \
    -exec shellcheck -s bash -S error -e SC1090,SC1091 {} +
  find scripts/ -name '*.sh' -exec shellcheck -s bash -S error -e SC1090,SC1091 {} +
else
  echo "[localtest] shellcheck NOT INSTALLED — CI still runs it; install it" >&2
fi

if [ "$runtime" -eq 0 ]; then
  echo "[localtest] maintainability debt budget"
  python3 scripts/maintainability_budget.py
  if [ "$#" -gt 0 ]; then
    echo "[localtest] focused pytest (coverage budget belongs to the full gate)"
    python3 -m pytest "$@"
  else
    coverage_dir="$(mktemp -d "${TMPDIR:-/tmp}/jarvis-coverage.XXXXXX")"
    trap 'rm -rf "$coverage_dir"' EXIT
    export COVERAGE_FILE="$coverage_dir/jarvis-coverage"
    echo "[localtest] full pytest with branch coverage"
    python3 -m coverage run -m pytest tests/
    python3 -m coverage combine
    python3 -m coverage json -o "$coverage_dir/coverage.json"
    echo "[localtest] coverage budget"
    python3 scripts/coverage_budget.py "$coverage_dir/coverage.json"
  fi
else
  # A live heartbeat legitimately updates repository runtime state while the
  # strict pytest guard requires those same paths to stay byte-for-byte still.
  # The full suite belongs before deploy (and in protected CI); this mode is
  # deliberately the post-restart runtime gate only.
  echo "[localtest] pytest skipped in runtime mode (use the pre-deploy full gate)"
fi

if [ "$runtime" -eq 1 ]; then
  echo "[localtest] component health"
  python3 -m core.components
  echo "[localtest] deploy revision"
  python3 -m core.deploy verify
  echo "[localtest] runtime smoke"
  python3 -m core.deploy smoke
fi

echo "[localtest] passed"
