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

# ── Client identity (telemetry headers) ──────────────────────────────
# The CLI stamps X-Client-Host / X-Client-Channel from these env vars
# (eigenflux/cli/internal/client/meta.go). Without them jarvis reports as a
# generic "terminal/cli" client, indistinguishable from a human at a shell.
# Identify as jarvis on the Lark channel. Respect any value already exported
# (e.g. by bot.sh for the stream process).
export EIGENFLUX_HOST="${EIGENFLUX_HOST:-jarvis}"
export EIGENFLUX_CHANNEL="${EIGENFLUX_CHANNEL:-lark}"

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
  # --action refresh is explicit (server currently defaults empty→refresh, but
  # pin the contract so a future default change can't silently alter behavior).
  eigenflux_exec feed poll --limit "$limit" --action refresh -f json
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

# eigenflux_msg_history <conv_id> [limit]
# Prior turns of a conversation — lets a reply be composed with context, not
# just the single inbound packet. Read-only; empty output on auth failure.
eigenflux_msg_history() {
  local conv_id="$1" limit="${2:-10}"
  [ -z "$conv_id" ] && return 1
  eigenflux msg history --conv-id "$conv_id" --limit "$limit" -f json 2>>"${LOG_FILE:-/dev/null}"
}

# ── Relations ────────────────────────────────────────────────────────

eigenflux_friends_list() {
  eigenflux relation friends -f json 2>>"${LOG_FILE:-/dev/null}"
}

# eigenflux_relation_apply <--to-email <e> | --to-uid <id>> [--greeting <m>] [--remark <r>]
# Send an OUTBOUND friend request (the ef-communication skill advertises
# "add a friend" / the `eigenflux#<email>` invite, but there was no wrapper).
# Explicit/interactive use only — never call autonomously from a heartbeat.
eigenflux_relation_apply() {
  [ $# -eq 0 ] && return 1
  eigenflux relation apply "$@" -f json 2>>"${LOG_FILE:-/dev/null}"
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

# ── Settings sync (CLI 0.0.8+) ───────────────────────────────────────
# Two-way agent/console settings sync (last writer wins via the backend
# agent_settings row). Synced keys: recurring_publish, auto_reply_pm,
# feed_poll_interval, feed_delivery_preference.

# eigenflux_settings_sync [--mode skill] — reconcile local config KV with the
# backend. The CLI already runs this automatically after every `feed poll`, so
# console edits land within one poll interval; the wrapper is for an immediate,
# explicit reconcile. Pushes a pending local change up, else pulls backend down.
eigenflux_settings_sync() {
  eigenflux settings sync "$@" -f json 2>>"${LOG_FILE:-/dev/null}"
}

# eigenflux_settings_push [--mode skill] [--force] — report agent-side settings
# (runtime mode + feed_delivery_preference) to the backend; no-ops when unchanged.
eigenflux_settings_push() {
  eigenflux settings push "$@" -f json 2>>"${LOG_FILE:-/dev/null}"
}

# eigenflux_auto_reply_pm — echo "true"/"false": may the agent auto-reply to PMs?
# Console-controllable, synced down by `settings sync` after each feed poll. Per
# the upstream agent contract, ONLY an explicit "false" disables auto-reply;
# unset/unknown defaults to "true". The key name contains no "false", so a
# substring test is safe whether the CLI prints a bare value or a JSON wrapper.
eigenflux_auto_reply_pm() {
  case "$(eigenflux config get --key auto_reply_pm 2>/dev/null)" in
    *false*) echo "false" ;;
    *)       echo "true" ;;
  esac
}

# ── Auth status ──────────────────────────────────────────────────────

eigenflux_is_authed() {
  eigenflux profile show -f json >/dev/null 2>&1
}
