#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SOURCE="$ROOT/scripts/hooks/pre-commit"
TARGET="$(git -C "$ROOT" rev-parse --path-format=absolute --git-path hooks/pre-commit)"

install -m 0755 "$SOURCE" "$TARGET"
echo "Installed $TARGET"
