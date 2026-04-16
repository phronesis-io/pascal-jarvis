# Lark (Feishu) Plugin

Bidirectional IM bridge — lets Jarvis receive messages and reply on Lark/Feishu.

This is one of the two **built-in plugins** (the other is [EigenFlux](../eigenflux/README.md)). When `lark.user_id` is configured, the plugin:

- Subscribes to `im.message.receive_v1` events (foreground in `bot.sh`)
- Sends replies back as the bot identity
- Shows a transient `Thinking...` indicator while Claude is working
- Exposes calendar free/busy checks (used by the `checkin` task)

## Quick Start

### 1. Install the CLI

```bash
npm install -g @larksuite/cli
```

### 2. Create a Lark app (one-time)

Go to <https://open.feishu.cn/app> → **Create Custom App**. Enable these scopes:

| Scope | Why |
|---|---|
| `im:message` | send/read messages |
| `im:message:send_as_bot` | reply as bot |
| `im:message.group_at_msg` | receive group @-mentions (optional) |
| `im:message.p2p_msg` | receive direct messages |
| `calendar:calendar.event:readonly` | freebusy for checkin task (optional) |
| `contact:user.base:readonly` | resolve open_id (optional) |

Under **Events & Callbacks → Subscribe Events**, add `im.message.receive_v1`.

Under **Version Management**, publish a version and wait for tenant admin approval. Once live, Lark will issue you an `app_id` + `app_secret`.

### 3. Authenticate lark-cli

```bash
lark-cli config init        # enter app_id + app_secret
lark-cli auth login --as bot
```

### 4. Find your open_id

Send any message to the bot from your own Lark account. The event listener will log your `sender_id` (starts with `ou_`). Put that in `jarvis.yaml`:

```yaml
lark:
  user_id: "ou_6cdf67159f83ad5fafacd5ec6d8901b6"
  app_id:  "cli_a95e1abba7319cb5"
```

### 5. Run the bot

```bash
./bot.sh
```

Now messaging the bot on Lark kicks off a Claude Code session (scoped to your conv_key) and replies in-thread.

## How it wires into the system

- **Event subscription**: `bot.sh` pipes `lark_subscribe_messages` into a `while read` loop → parses each NDJSON event with `jq` → routes to `claude -p`.
- **Session mapping**: Each Lark `conv_key` (sender's `open_id` for p2p, `chat_id` for groups) maps to a stable UUID5 session in `active_sessions.json`, auto-rotating when the session file crosses `claude.max_session_size`.
- **Replies**: `lark_reply` for the main markdown answer; `lark_reply_text` for short acks; `lark_delete_message` clears the `Thinking...` placeholder.
- **Calendar**: `tasks/checkin_pre.sh` sources this plugin and calls `lark_freebusy` to skip check-ins when you're busy.

## Shell API

The plugin is a **shell module** sourced by `bot.sh`. See `client.sh` for the full surface:

| Function | Purpose |
|---|---|
| `lark_require` | Abort with a readable error if `lark-cli` is missing |
| `lark_send <md>` | Push a message to `$USER_ID` |
| `lark_reply <mid> <md>` | Reply markdown to an incoming message |
| `lark_reply_text <mid> <text>` | Reply plain text, echoes lark-cli JSON on stdout |
| `lark_delete_message <mid>` | Delete a message the bot previously sent |
| `lark_subscribe_messages` | Stream `im.message.receive_v1` NDJSON to stdout |
| `lark_freebusy <start> <end>` | Call the freebusy API (ISO8601 UTC) |

All functions:
- Log stderr to `$LOG_FILE` (never pollute the reply pipe)
- Return non-zero on failure without crashing the caller
- Expect `$USER_ID` and `$LOG_FILE` already set (bot.sh populates these)

## Special commands

The bot recognizes these as first-character-case-insensitive shortcuts:

| User sends | Effect |
|---|---|
| `loop` or `heartbeat` | Force-trigger the next heartbeat cycle |

## Troubleshooting

**`[SDK Error] ... not found handler` in `jarvis.log`** — Benign. `lark-cli` receives event types we don't subscribe to (like `message_read_v1`); the bot ignores them.

**Bot stuck on "Thinking..." forever** — Check `work_dir` in `jarvis.yaml` matches the Claude project dir. See top-level [Troubleshooting](../../README.md#troubleshooting).

**Messages delivered out of order / duplicated** — Known: `lark-cli` can replay events briefly after reconnect. We dedupe on `message_id` implicitly (same `message_id` → same session → idempotent reply), but rapid duplicates could still produce two `Thinking...` messages.
