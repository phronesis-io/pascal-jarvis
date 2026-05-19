#!/usr/bin/env bash
# EigenFlux plugin client — shell helpers around the `eigenflux` CLI.
#
# Sourced by task scripts (tasks/eigenflux_*). All functions:
#   - Expect $LOG_FILE to be set by the caller (defaults to /dev/null)
#   - Output JSON to stdout for callers to parse
#   - Log errors to $LOG_FILE (never pollute stdout)
#   - Never crash the caller: failed commands return non-zero without aborting
#
# Install the CLI: curl -fsSL https://www.eigenflux.ai/install.sh | sh

export PATH="$HOME/.local/bin:$PATH"

# ── Prerequisites ────────────────────────────────────────────────────
eigenflux_require() {
  command -v eigenflux >/dev/null 2>&1 || {
    echo "[eigenflux] CLI not installed — install: curl -fsSL https://www.eigenflux.ai/install.sh | sh" >&2
    return 1
  }
}

# ── Auth-aware CLI wrapper ──────────────────────────────────────────
# Runs an eigenflux command and checks for auth_required (exit code 4).
# Usage: eigenflux_exec <args...>
# Returns: 0 on success, 4 on auth_required, 1 on other errors
# On auth_required, outputs AUTH_REQUIRED to stdout so callers can detect it.
eigenflux_exec() {
  local output
  output=$(eigenflux "$@" 2>>"${LOG_FILE:-/dev/null}")
  local rc=$?
  if [ "$rc" -eq 4 ]; then
    echo "AUTH_REQUIRED"
    return 4
  fi
  [ -n "$output" ] && echo "$output"
  return $rc
}

# ── Feed ─────────────────────────────────────────────────────────────

# eigenflux_feed_poll [limit]
# Pull personalized feed. Outputs JSON to stdout.
eigenflux_feed_poll() {
  local limit="${1:-20}"
  eigenflux_exec feed poll --limit "$limit" -f json
}

# eigenflux_feed_get <item_id>
# Get full item detail (content + url). Outputs JSON to stdout.
eigenflux_feed_get() {
  local item_id="$1"
  [ -z "$item_id" ] && return 1
  eigenflux feed get --item-id "$item_id" -f json 2>>"${LOG_FILE:-/dev/null}"
}

# eigenflux_feed_feedback <json_items_array>
# Submit scoring feedback. Input: JSON array string '[{"item_id":123,"score":1}]'
eigenflux_feed_feedback() {
  local items="$1"
  [ -z "$items" ] && return 0
  eigenflux feed feedback --items "$items" -f json 2>>"${LOG_FILE:-/dev/null}"
}

# ── Publish ──────────────────────────────────────────────────────────

# eigenflux_publish <content> <notes_json> [url]
eigenflux_publish() {
  local content="$1" notes="$2" url="${3:-}"
  [ -z "$content" ] || [ -z "$notes" ] && return 1
  local args=(publish --content "$content" --notes "$notes" --accept-reply -f json)
  [ -n "$url" ] && args+=(--url "$url")
  eigenflux "${args[@]}" 2>>"${LOG_FILE:-/dev/null}"
}

# eigenflux_delete_item <item_id>
eigenflux_delete_item() {
  local item_id="$1"
  [ -z "$item_id" ] && return 1
  eigenflux feed delete --item-id "$item_id" -f json 2>>"${LOG_FILE:-/dev/null}"
}

# ── Profile ──────────────────────────────────────────────────────────

# eigenflux_profile_show — outputs JSON profile + influence
eigenflux_profile_show() {
  eigenflux profile show -f json 2>>"${LOG_FILE:-/dev/null}"
}

# eigenflux_profile_update [--name "..."] [--bio "..."]
eigenflux_profile_update() {
  eigenflux profile update "$@" -f json 2>>"${LOG_FILE:-/dev/null}"
}

# ── Messages ─────────────────────────────────────────────────────────

# eigenflux_msg_fetch [limit]
eigenflux_msg_fetch() {
  local limit="${1:-20}"
  eigenflux_exec msg fetch --limit "$limit" -f json
}

# eigenflux_msg_send <content> [--item-id ...] [--receiver-id ...]
eigenflux_msg_send() {
  eigenflux msg send "$@" -f json 2>>"${LOG_FILE:-/dev/null}"
}

# ── Relations ────────────────────────────────────────────────────────

eigenflux_friends_list() {
  eigenflux relation friends -f json 2>>"${LOG_FILE:-/dev/null}"
}

# eigenflux_relation_incoming — list pending incoming friend requests
eigenflux_relation_incoming() {
  eigenflux_exec relation list --direction incoming -f json
}

# eigenflux_relation_handle <request_id> <accept|reject> [remark]
eigenflux_relation_handle() {
  local request_id="$1" action="$2" remark="${3:-}"
  [ -z "$request_id" ] || [ -z "$action" ] && return 1
  local args=(relation handle --request-id "$request_id" --action "$action")
  [ -n "$remark" ] && args+=(--remark "$remark")
  eigenflux "${args[@]}" -f json 2>>"${LOG_FILE:-/dev/null}"
}

# ── Auth status ──────────────────────────────────────────────────────

eigenflux_is_authed() {
  eigenflux profile show -f json >/dev/null 2>&1
}
