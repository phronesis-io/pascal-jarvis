# Lark (Feishu) Plugin

Bidirectional IM bridge — lets Jarvis receive messages and reply on Lark/Feishu from any device.

This is one of the two **built-in plugins** (the other is [EigenFlux](../eigenflux/README.md)). When `lark.user_id` is configured in `jarvis.yaml`, the plugin:

- Subscribes to `im.message.receive_v1` events (foreground in `bot.sh`)
- Sends replies back as the bot identity
- Shows a transient `Thinking...` indicator while Claude is working
- Exposes calendar free/busy checks (used by the `checkin` task)

---

## 🚀 Quick Start

Built on the official [larksuite/cli](https://github.com/larksuite/cli) — **3 commands from zero to live bot**.

### If your assistant (Claude Code etc) is driving

Paste this to your assistant:

> Set up Lark for my Jarvis bot. Run the `lark-cli config init --new` and `lark-cli auth login --recommend` commands in the background, send me the authorization URLs they print, wait for me to approve in the browser, then verify with `lark-cli auth status`. Finally grab my `open_id` and put it in `jarvis.yaml`.

It'll handle the rest — the CLI hands out a URL, you click approve, it configures everything including app creation, scope selection, and credential storage.

### Manual (if you prefer)

```bash
# 1. Install CLI + the Jarvis-relevant skill bundle
npm install -g @larksuite/cli
npx skills add larksuite/cli -y -g

# 2. Create a Lark app (opens browser; ~30 seconds)
lark-cli config init --new

# 3. Log in with the recommended scope set
lark-cli auth login --recommend

# 4. Verify — should show your user_name + list of granted scopes
lark-cli auth status
```

### Get your `open_id` for jarvis.yaml

Your `open_id` (starts with `ou_`) is what the bot keys conversations by. After step 4:

```bash
lark-cli contact +users-get --as user --user-id me --format json | python3 -c "
import json, sys
print('open_id:', json.load(sys.stdin)['data']['user']['open_id'])
"
```

Paste it into `jarvis.yaml`:

```yaml
lark:
  user_id: "ou_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
  app_id: ""  # optional — lark-cli remembers it internally, but setting it here is fine
```

Now `./bot.sh` will start the Lark listener. Send your bot a message on Lark — it replies.

---

## How it wires into the system

- **Event subscription**: `bot.sh` pipes `lark_subscribe_messages` into a `while read` loop → parses each NDJSON event with `jq` → routes to `claude -p`.
- **Session mapping**: Each Lark `conv_key` (sender's `open_id` for p2p, `chat_id` for groups) maps to a stable UUID5 session in `active_sessions.json`, auto-rotating when the session file crosses `claude.max_session_size`.
- **Replies**: `lark_reply` for the main markdown answer; `lark_reply_text` for short acks; `lark_delete_message` clears the `Thinking...` placeholder.
- **Calendar**: `tasks/checkin_pre.sh` sources this plugin and calls `lark_freebusy` to skip check-ins when you're busy.

---

## Shell API

The plugin is a **shell module** sourced by `bot.sh`. See [`client.sh`](client.sh) for the full surface:

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

---

## Special commands

The bot recognizes these as case-insensitive shortcuts you can send from Lark:

| Send | Effect |
|---|---|
| `loop` or `heartbeat` | Force-trigger the next heartbeat cycle |

---

## Identity switching — `--as user` vs `--as bot`

lark-cli supports two identities after login. Jarvis always uses the **bot** identity for replies (so the conversation UI shows the bot avatar), but you as a human can also issue commands as yourself (`--as user`):

```bash
lark-cli calendar +agenda --as user              # your agenda
lark-cli im +messages-send --as bot --user-id ou_xxx --text "hi"  # bot replies
```

The plugin's `client.sh` hard-codes `--as bot` for outbound messages.

---

## Scopes used

Running `lark-cli auth login --recommend` auto-grants everything we need. If you want to narrow, the minimum for Jarvis is:

- `im:message` — send/read messages
- `im:message:send_as_bot` — reply as bot
- `im:message.p2p_msg:readonly` — receive DMs
- `im:message.group_at_msg:readonly` — receive @ mentions in groups (optional)
- `calendar:calendar.event:readonly` — freebusy for check-in task (optional)
- `contact:user.base:readonly` — resolve your own open_id once during setup

```bash
# Narrow login (scope list can also be a comma-separated string)
lark-cli auth login --scope "im:message,im:message:send_as_bot,calendar:calendar.event:readonly"
```

---

## Troubleshooting

**`[SDK Error] ... not found handler` in `jarvis.log`**
Benign. `lark-cli` receives event types we don't subscribe to (like `message_read_v1`); the bot ignores them.

**Bot stuck on "Thinking..." forever**
Check `work_dir` in `jarvis.yaml` matches the Claude project dir (`~/.claude/projects/<slug>/`). See top-level [Troubleshooting](../../README.md#troubleshooting).

**`lark-cli auth status` says "not authenticated" after login**
You likely missed `npx skills add larksuite/cli -y -g` (the Skill bundle is required for the CLI to know how to format commands). Re-run it and try again.

**Token expired (401 errors after weeks/months)**
```bash
lark-cli auth login --recommend   # re-auth in place
```

**Want to switch to a different Lark app**
```bash
lark-cli auth logout
lark-cli config init --new
lark-cli auth login --recommend
# then update jarvis.yaml with new open_id if it changed
```

---

## References

- Official CLI docs + source: <https://github.com/larksuite/cli>
- Lark Open Platform: <https://open.feishu.cn/app> (app management console)
- Full scope catalog: `lark-cli auth scopes` (CLI) or the Open Platform docs
