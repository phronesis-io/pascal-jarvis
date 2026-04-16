# Pascal Jarvis

Turn [Claude Code](https://claude.com/claude-code) into a persistent personal AI agent with continuous heartbeat, self-evolving memory, and bidirectional IM integration.

---

## 🚀 Fastest way to install (AI-guided setup)

If you're reading this via an AI assistant (Claude Code, Cursor, etc), paste this to it:

> Clone `https://github.com/phronesis-io/pascal-jarvis` into my working directory, cd into it, run `./setup.sh`, then walk me through the plugin setup and editing `jarvis.yaml`.

The `setup.sh` wizard is **non-interactive, idempotent, and safe to re-run**. It:

1. Checks for `python3` / `jq` / `pip3` (prints install commands if missing)
2. Installs `pyyaml` (with `--break-system-packages` fallback for modern macOS/Debian)
3. Makes shell scripts executable
4. Copies `jarvis.example.yaml → jarvis.yaml` if missing (never overwrites)
5. Seeds the memory directory with example templates
6. Runs the test suite as a sanity check
7. Prints clear "next steps" — including the plugin wizards below

### After `setup.sh`, the plugin wizards

Each plugin has its own interactive installer — **both are optional**, and headless mode (no plugins) works fine:

**Lark (Feishu) — chat with your bot from your phone**
```bash
npm install -g @larksuite/cli
npx skills add larksuite/cli -y -g
lark-cli config init --new          # creates Lark app (browser auth)
lark-cli auth login --recommend     # grants scopes (browser auth)
# then paste your open_id into jarvis.yaml
```
Full walkthrough: [plugins/lark/README.md](plugins/lark/README.md)

**EigenFlux — broadcast network for AI agents**
```bash
python3 plugins/eigenflux/setup.py  # ~2 min interactive wizard
```
Does login, OTP verification, profile setup, and flips `enabled: true` in `jarvis.yaml`. Full walkthrough: [plugins/eigenflux/README.md](plugins/eigenflux/README.md)

### Start it up

```bash
./bot.sh
```

- With `lark.user_id` set → Lark bot live
- Without → heartbeat-only mode (memory consolidation + EigenFlux still run)

---

## What is this?

Pascal Jarvis wraps Claude Code with four capabilities it doesn't have out of the box:

1. **Heartbeat Loop** — A background scheduler that runs tasks on intervals (feed triage, check-ins, memory consolidation). Tasks are defined in a single `HEARTBEAT.md` file and executed via pre/post shell scripts + a batched Claude call.

2. **Tiered Memory System** — Five-layer memory that compresses over time (permanent → monthly → weekly → daily → hourly). Memory is injected into every Claude call, giving it persistent context across sessions.

3. **Built-in Plugins** — Two first-class integrations that ship with the system:
   - **[Lark (Feishu)](plugins/lark/README.md)** — bidirectional IM bridge so you can chat with your agent from your phone.
   - **[EigenFlux](plugins/eigenflux/README.md)** — broadcast network that feeds the agent fresh signals from other agents and lets it publish back.

   Both plugins are optional — disable either by leaving its config section out of `jarvis.yaml`. See the [Plugins](#plugins) section below for usage.

4. **Admin Console** — Local web dashboard (`python3 admin.py`) for browsing memory, searching session history, and inspecting the Lark conversation rotation timeline.

## Architecture

```
┌──────────────────────────────────────────────────────────┐
│  bot.sh (entry point)                                    │
│  ├── Lark event listener (foreground)                    │
│  │   └── sources plugins/lark/client.sh → claude -p      │
│  ├── Heartbeat loop (background) → core/heartbeat.py     │
│  │   ├── Parse HEARTBEAT.md                              │
│  │   ├── Run pre-scripts (gather data)                   │
│  │   ├── Batch Claude call                               │
│  │   └── Run post-scripts (act on output)                │
│  └── Admin console (background, optional) → admin.py     │
│                                                          │
│  core/                         (system)                  │
│  ├── config.py      — jarvis.yaml loader                 │
│  ├── heartbeat.py   — task scheduler                     │
│  ├── memory.py      — tiered memory loader               │
│  ├── session.py     — session rotation + fcntl lock      │
│  ├── search.py      — session history parser             │
│  └── safety.py      — error-pattern filter               │
│                                                          │
│  plugins/                      (built-in)                │
│  ├── lark/                                               │
│  │   └── client.sh  — shell helpers sourced by bot.sh    │
│  └── eigenflux/                                          │
│      └── client.py  — HTTP client + local persistence    │
│                                                          │
│  tasks/                        (pre/post hooks)          │
│  ├── checkin_*      — hourly free-time check-ins         │
│  ├── memory_*       — hourly→daily→weekly→monthly        │
│  │                    consolidation pipeline             │
│  └── eigenflux_*    — feed, messages, publish, profile   │
└──────────────────────────────────────────────────────────┘
```

## Quick Start

### Prerequisites

1. **Claude Code CLI** — install and authenticate:
   ```bash
   # macOS / Linux
   npm install -g @anthropic-ai/claude-code
   claude   # follow the auth flow on first run
   ```

2. **Python 3.10+** with PyYAML:
   ```bash
   pip install pyyaml
   ```

3. **jq** (for Lark message parsing):
   ```bash
   # macOS
   brew install jq
   # Ubuntu/Debian
   sudo apt install jq
   ```

4. **(Optional) Plugins** — both are built-in but opt-in:
   - **Lark** — see [plugins/lark/README.md](plugins/lark/README.md) for app creation + `lark-cli` setup
   - **EigenFlux** — see [plugins/eigenflux/README.md](plugins/eigenflux/README.md) for account registration

   You can run fully headless (no plugins) — memory consolidation still works.

### Setup

```bash
# Clone the repo
git clone https://github.com/phronesis-io/pascal-jarvis.git
cd pascal-jarvis

# Create your config
cp jarvis.example.yaml jarvis.yaml
# Edit jarvis.yaml:
#   - Set data_dir (where sessions/memory are stored)
#   - Set work_dir (directory Claude can access — your project root)
#   - (Optional) Set lark.user_id to your Lark open_id

# Set up initial memory
mkdir -p ~/.jarvis/memory
cp examples/memory/*.md ~/.jarvis/memory/
# Edit the memory files to describe yourself

# (Optional) Set up built-in plugins — see their dedicated READMEs for full setup
#   Lark:      plugins/lark/README.md
#   EigenFlux: plugins/eigenflux/README.md

# Make scripts executable
chmod +x bot.sh tasks/*.sh

# Run
./bot.sh
```

### Headless Mode (no Lark)

If you don't set `lark.user_id` in `jarvis.yaml`, the bot runs in heartbeat-only mode — it still does memory consolidation, EigenFlux feed triage, and everything else, but without IM.

### Running as a background service (macOS)

Create `~/Library/LaunchAgents/com.jarvis.bot.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.jarvis.bot</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>/path/to/pascal-jarvis/bot.sh</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/tmp/jarvis-stdout.log</string>
  <key>StandardErrorPath</key><string>/tmp/jarvis-stderr.log</string>
</dict>
</plist>
```

```bash
launchctl load ~/Library/LaunchAgents/com.jarvis.bot.plist
```

This auto-starts on login and restarts on crash.

## Configuration

All config lives in `jarvis.yaml`. See `jarvis.example.yaml` for the full schema.

Key settings:
- `data_dir` — where sessions and memory are stored
- `lark.user_id` — your Lark/Feishu open_id (skip for headless)
- `claude.heartbeat_model` — model for background tasks (default: `sonnet`, cheaper)
- `heartbeat.check_interval` — how often to check for due tasks (default: 10s)

## Customizing Tasks

Edit `HEARTBEAT.md` to add/remove/modify tasks. Each task has:

```markdown
### task-name
- interval: 10m          # how often to run
- pre: tasks/pre.sh      # data gathering script (stdout → Claude input)
- post: tasks/post.py    # response handler (stdin ← Claude output)
- prompt: |              # what to ask Claude
    Your prompt here.
```

Pre-scripts that exit with empty stdout cause the task to be skipped (retried later).

## Plugins

Jarvis ships with **two built-in plugins** that are integrated at the system level. Each has a dedicated README with full setup, API, and troubleshooting.

### Lark (Feishu) — IM Bridge

📖 **Full docs: [plugins/lark/README.md](plugins/lark/README.md)**

Chat with your agent from Lark/Feishu on any device. The plugin:
- Subscribes to incoming messages (`im.message.receive_v1`)
- Maps each conversation (`conv_key`) to a stable Claude Code session
- Auto-rotates sessions when they cross `claude.max_session_size`
- Shows transient `Thinking...` indicators during Claude calls
- Recognizes shortcut commands (`loop` / `heartbeat` to force-trigger a heartbeat cycle)

**Enable** — add to `jarvis.yaml`:
```yaml
lark:
  user_id: "ou_your_open_id"
  app_id:  "cli_your_app_id"
```

**Run** — the bot picks it up automatically. Leave `lark:` out to run headless.

**Shell API** (sourced by `bot.sh` from `plugins/lark/client.sh`):
`lark_send` · `lark_reply` · `lark_reply_text` · `lark_delete_message` · `lark_subscribe_messages` · `lark_freebusy`

### EigenFlux — Agent Broadcast Network

📖 **Full docs: [plugins/eigenflux/README.md](plugins/eigenflux/README.md)**

[EigenFlux](https://eigenflux.ai) is a broadcast network where AI agents share and receive real-time signals. Four heartbeat tasks integrate it:

| Task | Interval | What it does |
|---|---|---|
| `eigenflux-feed-triage` | 10m | Pull feed, score items, push actionable ones to you |
| `eigenflux-messages`    | 10m | Fetch unread DMs, suggest responses |
| `eigenflux-publish`     | 1h  | Auto-broadcast useful signals from your conversations |
| `eigenflux-profile`     | 24h | Sync your EigenFlux bio with memory changes |

**Enable** — add to `jarvis.yaml`:
```yaml
plugins:
  eigenflux:
    enabled: true
    persist_feed: true
    feed_db: eigenflux/feed_store.jsonl
```

**Setup** — one-time login + email verification (see [the plugin README](plugins/eigenflux/README.md#quick-start)).

**Python API** (import from anywhere):
```python
from plugins.eigenflux.client import EigenFluxClient
c = EigenFluxClient("eigenflux")
c.pull_feed(); c.publish(...); c.get_me(); c.search_feed_history("...")
```

### Writing your own plugin

A plugin is just a directory under `plugins/` that provides one or both of:

1. **A client library** (Python for API-style plugins, shell for CLI-style plugins) — the shared code task scripts import.
2. **Heartbeat tasks** in `HEARTBEAT.md` + matching `tasks/<plugin>_*_pre.sh` / `_post.py` scripts.

Pre-scripts write to stdout (becomes Claude's input data); post-scripts read stdin (Claude's response) and can call the plugin's client library to act on it. If a post-script writes to stdout, that becomes the message sent to Lark. Follow the [EigenFlux plugin structure](plugins/eigenflux/) as a template.

## Memory System

Memory files live in `~/.jarvis/memory/` (or your configured `data_dir/memory/`).

### How it works

1. **Hourly**: Indexes the last hour's conversation into 1-3 lookup lines
2. **Daily**: Compresses hourly entries into 3-6 bullet points
3. **Weekly**: Merges daily entries into a 5-10 point digest
4. **Monthly**: Compresses weekly digest into a long-term archive
5. **Consolidation**: Nightly review that proposes updates to permanent memory files

Each layer archives before clearing, so nothing is ever lost.

### Adding permanent memory

Create a `.md` file in the memory directory with frontmatter:

```markdown
---
name: My Project
description: One-line description used for relevance matching
type: project
---

Your content here.
```

Types: `user`, `feedback`, `project`, `reference`

## Admin Console

A local web dashboard for browsing memories, searching session history, and viewing skills/settings.

```bash
python3 admin.py
# open http://localhost:3456
```

Configure host/port in `jarvis.yaml` under the `admin:` section. Config-driven: it reads `memory_dir` and derives the sessions path from `work_dir`, so it always matches your bot's view.

## Troubleshooting

**Bot stuck on "Thinking..." forever**
- Check `jarvis.log` for errors
- Verify `work_dir` in `jarvis.yaml` matches where your Claude Code sessions live (`~/.claude/projects/<hash>/`)
- Delete `active_sessions.json` to start fresh sessions in the correct project dir

**`[SDK Error] handle message failed` in logs**
- Benign — lark-cli receives event types (like `message_read_v1`) it doesn't have a handler for. The bot ignores these.

**Heartbeat not running tasks**
- Check `heartbeat_state.json` for last-run timestamps
- Delete it to force all tasks to run on next cycle
- Tasks also skip if their pre-script exits with empty output (see `tasks/*.sh`)

**Tests**
```bash
python3 -m pytest tests/
```

## License

MIT
