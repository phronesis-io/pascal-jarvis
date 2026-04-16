#!/usr/bin/env bash
# Lark (Feishu) plugin client — shell helpers around `lark-cli`.
#
# Sourced by bot.sh. All functions:
#   - Expect $USER_ID and $LOG_FILE to be set by the caller
#   - Send stdout to /dev/null (to keep the bot's stdout pipeline clean)
#   - Send stderr to $LOG_FILE (so errors are recoverable without polluting
#     user-facing Lark replies)
#   - Never crash the caller: failed lark-cli invocations log a warning and
#     return non-zero without aborting
#
# Design note: Lark is one of the two built-in plugins (the other is
# plugins/eigenflux). The bot's event loop lives in bot.sh and delegates
# every lark-cli call to a function in this file, so that swapping Lark for
# another IM requires only providing a drop-in shell module.

# ── Prerequisites check ─────────────────────────────────────────────
lark_require() {
  command -v lark-cli >/dev/null 2>&1 || {
    echo "[lark] lark-cli not installed — install with: npm i -g @larksuite/cli" >&2
    return 1
  }
}

# ── Outbound: send / reply / delete ──────────────────────────────────

# lark_send <markdown>
# Send a markdown message to the configured user ($USER_ID).
lark_send() {
  local content="$1"
  [ -z "$content" ] && return 0
  [ -z "${USER_ID:-}" ] && return 0
  if ! lark-cli im +messages-send \
      --user-id "$USER_ID" \
      --markdown "$content" \
      --as bot 2>>"${LOG_FILE:-/dev/null}" >/dev/null; then
    echo "[lark] send failed" >> "${LOG_FILE:-/dev/null}" 2>/dev/null
    return 1
  fi
}

# lark_reply <message_id> <markdown>
# Reply (markdown) to a specific incoming message.
lark_reply() {
  local message_id="$1" content="$2"
  [ -z "$message_id" ] || [ -z "$content" ] && return 0
  if ! lark-cli im +messages-reply \
      --message-id "$message_id" \
      --markdown "$content" \
      --as bot 2>>"${LOG_FILE:-/dev/null}" >/dev/null; then
    echo "[lark] reply failed (mid=$message_id)" >> "${LOG_FILE:-/dev/null}" 2>/dev/null
    return 1
  fi
}

# lark_reply_text <message_id> <plain_text>
# Like lark_reply but plain text (used for Thinking... indicators and
# short acknowledgements). Echoes the lark-cli JSON response on stdout
# so callers can extract the returned message_id (for later deletion).
lark_reply_text() {
  local message_id="$1" text="$2"
  [ -z "$message_id" ] || [ -z "$text" ] && return 0
  lark-cli im +messages-reply \
    --message-id "$message_id" \
    --text "$text" \
    --as bot 2>>"${LOG_FILE:-/dev/null}" || true
}

# lark_delete_message <message_id>
# Delete a message the bot previously sent (used to clear "Thinking...").
lark_delete_message() {
  local mid="$1"
  [ -z "$mid" ] && return 0
  lark-cli im messages delete \
    --params "{\"message_id\":\"$mid\"}" \
    --as bot 2>>"${LOG_FILE:-/dev/null}" >/dev/null || true
}

# ── Inbound: event subscription ──────────────────────────────────────

# lark_subscribe_messages
# Emits NDJSON lines to stdout, one per incoming im.message.receive_v1 event.
# The caller pipes this into a `while read` loop and parses each line with jq.
# Stderr (SDK errors) is redirected to the log file.
lark_subscribe_messages() {
  lark-cli event +subscribe \
    --event-types im.message.receive_v1 \
    --compact --quiet --as bot 2>>"${LOG_FILE:-/dev/null}"
}

# ── Calendar (used by tasks/checkin_pre.sh for free/busy detection) ──

# lark_freebusy <iso_start> <iso_end>
# Returns the raw JSON response of the freebusy API, or empty on failure.
lark_freebusy() {
  local start="$1" end="$2"
  lark-cli calendar +freebusy --start "$start" --end "$end" \
    2>>"${LOG_FILE:-/dev/null}" || true
}
