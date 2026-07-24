#!/usr/bin/env bash
# One local verification entry for humans and coding agents.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

runtime=0
if [ "${1:-}" = "--runtime" ]; then
  runtime=1
  shift
fi

echo "[localtest] shell syntax"
bash -n bot.sh
while IFS= read -r script; do
  bash -n "$script"
done < <(find tasks scripts -type f -name '*.sh' -print)

if command -v shellcheck >/dev/null 2>&1; then
  echo "[localtest] shellcheck bot.sh"
  shellcheck -s bash -S error -e SC1090,SC1091 bot.sh
fi

echo "[localtest] pytest"
python3 -m pytest tests/ "$@"

if [ "$runtime" -eq 1 ]; then
  echo "[localtest] component health"
  python3 -m core.components
  echo "[localtest] deploy revision"
  python3 -m core.deploy verify
  echo "[localtest] runtime smoke"
  python3 -m core.deploy smoke
fi

echo "[localtest] passed"
