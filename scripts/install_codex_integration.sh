#!/usr/bin/env bash
# Register the repo-owned Jarvis plugin and local stdio MCP server with Codex.
set -euo pipefail

JARVIS_DIR="$(cd "$(dirname "$0")/.." && pwd -P)"
export JARVIS_DIR

command -v codex >/dev/null 2>&1 || {
  echo "codex CLI is required" >&2
  exit 2
}
[[ -f "$JARVIS_DIR/.agents/plugins/marketplace.json" ]] || {
  echo "Jarvis plugin marketplace is missing" >&2
  exit 2
}

mkdir -p "$HOME/.jarvis"
path_file="$HOME/.jarvis/repo-path"
temporary="${path_file}.$$"
printf '%s\n' "$JARVIS_DIR" > "$temporary"
chmod 600 "$temporary"
mv "$temporary" "$path_file"

if ! codex plugin marketplace list --json | python3 -c '
import json, os, sys
target = os.path.realpath(sys.argv[1])
data = json.load(sys.stdin)
raise SystemExit(0 if any(
    item.get("root")
    and os.path.realpath(str(item["root"])) == target
    for item in data.get("marketplaces", [])
) else 1)
' "$JARVIS_DIR"; then
  codex plugin marketplace add "$JARVIS_DIR" >/dev/null
fi

codex plugin add jarvis-matters@pascal-jarvis >/dev/null
echo "Jarvis Matters installed. Start a new Codex task to load the plugin."
