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
curl -fsSL https://www.eigenflux.ai/install.sh | sh   # install CLI
eigenflux auth login --email you@example.com           # email OTP flow
```
Auth credentials stored in `~/.eigenflux/`. Jarvis-specific settings in `eigenflux/user_settings.json`. Full walkthrough: [plugins/eigenflux/README.md](plugins/eigenflux/README.md)

### Upgrading

After `git pull`, run the memory migration script if your memory is still in flat layout:

```bash
./scripts/migrate-memory.sh [YOUR_MEMORY_DIR]
```

This restructures memory into `hot/warm/timeline/system` layers. Safe to run multiple times.

Also clone the EigenFlux skills repo if not already present:

```bash
git clone https://github.com/phronesis-io/openclaw-eigenflux ../openclaw-eigenflux
```

Then restart jarvis.

### Start it up

```bash
./bot.sh
```

- With `lark.user_id` set → Lark bot live
- Without → heartbeat-only mode (memory consolidation + EigenFlux still run)

---

## What is this?

Pascal Jarvis wraps Claude Code with a full personal-agent runtime:

1. **Heartbeat Loop + Guardian Daemon** — A background scheduler runs tasks on configurable intervals (defined in `HEARTBEAT.md`, executed via pre/post shell scripts + a batched Claude call). A guardian daemon (`daemon.py`) monitors the bot process, kills stuck Claude sessions, and auto-restarts on crash.

2. **Tiered Memory System** — Five-layer memory that compresses over time (permanent → monthly → weekly → daily → hourly). Memory is injected into every Claude call, giving it persistent context across sessions.

3. **Daily Rhythm & Calendar** — A suite of time-aware tasks that structure the day:
   - *Daily plan* — morning overview of calendar, priorities, and open threads (time-gated 8:00-9:30)
   - *Activity log* — silent background tracker that logs what you're working on
   - *Daily reflect* — evening review with wins, patterns, and tomorrow prep
   - *Free-time nudge* — detects idle calendar blocks and sends casual suggestions
   - *Calendar read/write* — syncs Lark calendar events and can create/update/delete them

4. **Built-in Plugins & Content Curation** — Two first-class integrations plus content-aware features:
   - **[Lark (Feishu)](plugins/lark/README.md)** — bidirectional IM bridge so you can chat with your agent from your phone.
   - **[EigenFlux](plugins/eigenflux/README.md)** — broadcast network with a two-stage pipeline: feed triage for quick scoring, plus deep research for high-value items.
   - *Content recommend* and *watchlater remind* — curates and follows up on saved content.

   Both plugins are optional — disable either by leaving its config section out of `jarvis.yaml`. See the [Plugins](#plugins) section below for usage.

5. **Admin Console & Ops Tooling** — Local web dashboard (`python3 admin.py`) for browsing memory and session history. Background tasks handle repos sync, system self-diagnostics, engagement analysis, and cross-session context bridging.

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  daemon.py (guardian — monitors + restarts bot.sh)           │
│                                                             │
│  bot.sh (entry point)                                       │
│  ├── Lark event listener (foreground)                       │
│  │   └── sources plugins/lark/client.sh → claude -p         │
│  ├── Heartbeat loop (background) → core/heartbeat.py        │
│  │   ├── Parse HEARTBEAT.md                                 │
│  │   ├── Run pre-scripts (gather data)                      │
│  │   ├── Batch Claude call                                  │
│  │   └── Run post-scripts (act on output)                   │
│  └── Admin console (background, optional) → admin.py        │
│                                                             │
│  core/                            (system)                  │
│  ├── config.py       — jarvis.yaml loader                   │
│  ├── heartbeat.py    — task scheduler                       │
│  ├── memory.py       — tiered memory loader                 │
│  ├── session.py      — session rotation + fcntl lock        │
│  ├── search.py       — session history parser               │
│  ├── safety.py       — error-pattern filter                 │
│  ├── card.py         — Lark card message builder            │
│  ├── timeutil.py     — timezone / time-range helpers        │
│  └── jobs.py         — job queue utilities                  │
│                                                             │
│  handlers/                        (message handlers)        │
│  └── handle_image.sh — image message processing             │
│                                                             │
│  scripts/                         (ops & dev tools)         │
│  ├── backup_sessions.sh  — daily session backup             │
│  ├── memory-viewer.py    — interactive memory browser       │
│  ├── search_v2.py        — enhanced transcript search       │
│  ├── session_search.py   — simple session search            │
│  ├── tail_turns.py       — tail recent conversation turns   │
│  └── migrate-memory.sh   — one-time memory migration        │
│                                                             │
│  plugins/                         (built-in)                │
│  ├── lark/                                                  │
│  │   └── client.sh   — shell helpers sourced by bot.sh      │
│  └── eigenflux/                                             │
│      └── client.py   — HTTP client + local persistence      │
│                                                             │
│  tasks/                           (pre/post hooks)          │
│  ├── Daily rhythm:                                          │
│  │   daily_plan, activity_log, daily_reflect, free_time_nudge│
│  ├── Calendar:                                              │
│  │   calendar_sync, calendar_write                          │
│  ├── Memory pipeline:                                       │
│  │   memory_hourly → daily → weekly → monthly,              │
│  │   memory_consolidate, memory_tidy                        │
│  ├── EigenFlux:                                             │
│  │   feed, messages, publish, profile, research             │
│  ├── Content:                                               │
│  │   content_recommend, watchlater_remind                   │
│  └── Monitoring & ops:                                      │
│      checkin, engagement_analyze, cross_session_sync,        │
│      phronesis_monitor, repos_sync, self_diagnostic,         │
│      personal_site                                          │
└─────────────────────────────────────────────────────────────┘
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

## Writing Custom Tasks

Tasks are the building blocks of the heartbeat loop. Each task is defined in `HEARTBEAT.md` and implemented as a pair of scripts.

### Task definition

Add a block to `HEARTBEAT.md`:

```markdown
### task-name
- interval: 10m          # how often to run
- pre: tasks/task_name_pre.sh    # data gathering script
- post: tasks/task_name_post.py  # response handler
- prompt: |
    Your prompt here.
```

### Naming convention

Tasks follow a strict naming pattern: `tasks/<name>_pre.sh` + `tasks/<name>_post.py`. The pre-script gathers data; the post-script acts on Claude's response.

### Pre-script convention

The pre-script's **stdout becomes Claude's input data** for that task. Key rules:

- **Empty stdout = skip task.** If the pre-script prints nothing, the task is silently skipped and retried at the next interval. This is the primary gating mechanism.
- **Time-gated tasks**: Check the current hour and exit early. For example, `daily_plan_pre.sh` only runs between 8:00-9:30; outside that window it exits with no output.
- Pre-scripts typically call APIs, read files, or check system state to assemble context for Claude.

### Post-script convention

The post-script receives **Claude's response on stdin** and can act on it:

- **stdout becomes the Lark message.** If the post-script prints something, it gets sent to the user via Lark.
- **Silent tasks**: Post-scripts that write nothing to stdout (e.g., `activity_log_post.py`) perform their work silently — writing to files, updating state — without notifying the user.
- Post-scripts can import from `core/` and `plugins/` to call APIs, update memory, etc.

### The `HEARTBEAT_OK` signal

`HEARTBEAT_OK` is the universal "nothing to do" response. When Claude determines there's no actionable output for a task, it returns this string. Post-scripts should check for it and exit cleanly:

```python
response = sys.stdin.read().strip()
if response == "HEARTBEAT_OK":
    sys.exit(0)
```

### Example patterns

- **Notify user**: Pre-script gathers data → Claude analyzes → post-script prints a message → sent to Lark
- **Silent tracking**: Pre-script gathers data → Claude processes → post-script writes to a file, prints nothing
- **Time-gated**: Pre-script checks `date +%H`, exits if outside window → task skipped entirely
- **API-gated**: Pre-script calls an API, exits if no new data → task skipped until data appears

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

## Scripts

Utility scripts in `scripts/` for operations and debugging:

- **`backup_sessions.sh`** — Daily session file backup with read-only protection. Copies session transcripts to an archive directory so they survive rotation.
- **`memory-viewer.py`** — Interactive TUI for browsing the tiered memory tree. Useful for inspecting what the agent "knows" without digging through files.
- **`search_v2.py`** — Enhanced session transcript search with relevance scoring and context display.
- **`session_search.py`** — Simple session search tool for quick keyword lookups.
- **`tail_turns.py`** — Tail recent conversation turns, like `tail -f` for live conversations. Helpful for monitoring what the bot is doing.
- **`migrate-memory.sh`** — One-time migration from flat memory layout to the tiered `hot/warm/timeline/system` structure. Safe to run multiple times (idempotent).

## Guardian Daemon

`daemon.py` is a lightweight supervisor process that keeps the bot alive:

- Monitors `bot.sh` health every 2 minutes — checks PID, heartbeat freshness, and session locks
- Kills stuck Claude processes by detecting stale session lock files
- Auto-restarts `bot.sh` on crash (up to 3 attempts with 5-minute cooldown)
- Logs to `daemon.log` with automatic log rotation

Run with:
```bash
python3 daemon.py
```

The daemon manages `bot.sh` directly — you don't need to run `bot.sh` separately. For production use, point your `launchd` plist at `daemon.py` instead of `bot.sh`.

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
