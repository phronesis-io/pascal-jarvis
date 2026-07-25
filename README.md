# Pascal Jarvis

Turn [Claude Code](https://claude.com/claude-code) into a persistent personal AI agent with continuous heartbeat, self-evolving memory, closed-loop proactive intents, and bidirectional IM integration.

**Release: `v1.3.0` (2026-07-13)** — see [CHANGELOG.md](CHANGELOG.md). 1400+ tests passing.

**Contributing**: everyone works on their own `dev/<name>` branch; Pascal merges to `main`. See [CONTRIBUTING.md](CONTRIBUTING.md).

---

## 🚀 Fastest way to install (AI-guided setup)

If you're reading this via an AI assistant (Claude Code, Cursor, etc), paste this to it:

> Clone `https://github.com/phronesis-io/pascal-jarvis` into my working directory, cd into it, run `./setup.sh`, then follow `docs/INSTALL.md` phase by phase — run `./scripts/doctor.sh` after each phase and fix any FAIL until green. Relay every step marked 🧑 NEEDS HUMAN to me verbatim.

Two things make the agent-driven install smooth:
- **[docs/INSTALL.md](docs/INSTALL.md)** — phase-by-phase guide written FOR the installing agent: every step has a verification command, every human-only action (browser auth, console clicks, secrets) is marked 🧑 with exact click paths, and there's a troubleshooting table of real failure modes.
- **`./scripts/doctor.sh`** — one-command health check: 20+ PASS/WARN/FAIL probes (deps, auth states, config, runtime), each FAIL printed with its exact fix command. The agent loops run→fix→rerun until green.

The `setup.sh` wizard is **non-interactive, idempotent, and safe to re-run**. It:

1. Checks for `python3` / `jq` / `pip3` (prints install commands if missing)
2. Installs `pyyaml` (with `--break-system-packages` fallback for modern macOS/Debian)
3. Makes shell scripts executable
4. Copies `jarvis.example.yaml → jarvis.yaml` and `sources.example.yaml → sources.yaml` if missing (never overwrites)
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

For day-to-day operation, use the helper instead of starting `bot.sh` by hand —
it handles the single-instance lock, clears stale Python bytecode, and warns you
before killing an in-flight conversation:

```bash
./restart.sh            # graceful restart of the bot (daemon stays up)
./restart.sh --full     # refresh/restart daemon, bot, dashboard, and mobile gateway
./restart.sh --status   # show daemon / bot / lark-cli process status
./restart.sh --yes      # skip the in-flight-conversation confirmation
                        # (required for non-interactive callers: cron, scripts)
```

---

## What is this?

Pascal Jarvis wraps Claude Code with a full personal-agent runtime:

1. **Heartbeat Loop + Guardian Daemon** — A background scheduler runs tasks on configurable intervals (defined in `HEARTBEAT.md`, executed via pre/post shell scripts + a batched Claude call). A guardian daemon (`daemon.py`) monitors the bot process, kills stuck Claude sessions, and auto-restarts on crash.

2. **Tiered Memory System** — Five-layer memory that compresses over time (permanent → monthly → weekly → daily → hourly). Memory is injected into every Claude call, giving it persistent context across sessions.

3. **Multi-format Message Handling** — Beyond plain text, the bot processes:
   - *Images* — downloaded and passed to Claude for visual understanding
   - *Files* — downloaded to local storage, contents available via Read tool
   - *Voice messages* — transcribed via Whisper API, transcript passed to Claude
   - *Quote replies* — fetches the quoted message and prepends as context
   - *Interactive cards, stickers, locations, contact/chat shares* — extracted and described
   - *Merged forwards (合并转发)* — expanded via batch API, content truncated to 5KB

4. **Daily Rhythm & Calendar** — A suite of time-aware tasks that structure the day:
   - *Daily plan* — morning overview of calendar, priorities, and open threads (time-gated 8:00-9:30)
   - *Activity log* — silent background tracker that logs what you're working on
   - *Daily reflect* — evening review with wins, patterns, and tomorrow prep
   - *Calendar read/write* — 30-day rolling window (7 days detailed + 8-30 days compact), with create/update/delete write-back
   - *Task triage* — philosophical task system (praxis/poiesis capture → commit → decay)
   - *Weekly review* — end-of-week summary and planning

4. **Built-in Plugins & Content Curation** — Two first-class integrations plus content-aware features:
   - **[Lark (Feishu)](plugins/lark/README.md)** — bidirectional IM bridge so you can chat with your agent from your phone.
   - **[EigenFlux](plugins/eigenflux/README.md)** — broadcast network with a two-stage pipeline: feed triage for quick scoring, plus deep research for high-value items.
   - *Content recommend* and *watch-later* — curates content for you; saved items resurface later at calmer moments.

   Both plugins are optional — disable either by leaving its config section out of `jarvis.yaml`. See the [Plugins](#plugins) section below for usage.

5. **Background Jobs & Auto-Promotion** — Any Claude call running longer than ~2 minutes is automatically promoted to a background job: the conversation is released immediately (you can keep chatting), the long task keeps running, and its result is delivered as a reply *and* merged into the conversation's context. Send `jobs` to list them, `cancel <id>` to kill one; a sweeper reconciles jobs whose process died so you're never left waiting on a ghost.

6. **Attention Engineering** — Proactive output is classified before delivery and records where Pascal should act. Ordinary decisions wait under `手机集中批`; only urgent, calendar-bound, active-conversation, or Lark-native decisions become `飞书即时批`. Urgent non-choice alerts may still reach Lark but say `无需批`, while routine FYI output stays in the web `知会` stream. Every record remains in one durable ledger, so a decision closes everywhere. Quiet hours, batching, deduplication, and aggregated delivery-failure alerts protect time for higher-value work instead of maximizing notification handling.

7. **Unified Perception Layer** — Declarative source registry (`sources.yaml`): watch files/reports, local repo commits, Lark group chats, and mailbox metadata with *one config block per source* — no new scripts. Signals are deduplicated across sources, buffered into memory (so the next Claude call "knows"), and sensitivity-tagged so private content (mail, DMs) never leaks into outward-facing tasks. A new source type = one `sources/<type>.py` adapter implementing `collect(cfg, state)`.

8. **Self-Evolution** — Engagement tracking analyzes which messages land and which don't, auto-tuning task frequency within guardrails (infrastructure tasks exempt, drift capped at 4× the configured cadence). A daily harness-evolve task reviews accumulated feedback and lands hygiene improvements automatically. Cross-session sync imports context from parallel Claude Code projects.

9. **Admin Console & Ops Tooling** — Local web dashboard (`python3 admin.py`) for browsing memory and session history. Background tasks handle repos sync, system self-diagnostics (channel watermarks that catch silently-dead pipelines, stream health, CLI version tracking, process conflict detection), and cross-session context bridging.

10. **Closed-Loop Intents & Trust Guards** — Proactive reminders are a real state machine, not fire-and-forget. An intent the bot raises (a reminder, a prep, a follow-up) is tracked to a terminal state: the LLM authors the message but never its own bookkeeping; delivery is acknowledged via an inflight manifest; failures retry within a bound and then surface *one* apology card instead of nagging. A loop closes when you reply (`做了` / `没做` / `不用追` — a negation-aware classifier, no button backend required), on a button tap, or on a TTL. Calendar events map to intents idempotently (one row per date·title·role, a prep that would fire after its event is dropped), and "bring an umbrella" carry-reminders anchor to the morning before you first leave. Two trust guards back the agent's completion claims: a **document write-guard** (`core/doc_guard.py`) that verifies protected-file edits by independent read-back counts + a multiplicity-aware block diff (so a "fixed it ✅" can't be reported when the change isn't in the live file, and a full-rewrite that would wipe hand-entered content is rejected), and **live self-monitoring** (`core/selfmon.py`) that computes noise/re-fire/overdue/crash signals from the real JSONL+state+DB with a liveness assertion — surfaced on the dashboard, never raw in chat. If the pinned Claude model is unavailable or you hit a Claude limit, the bot degrades through the configured Claude chain and two optional Claude Code-compatible backup providers, then can use an OpenAI fallback. Main Lark conversations get a bounded local tool loop; heartbeat fallback remains text-only so unattended prompts cannot gain local execution.

11. **Items, Topics & Mobile Home** — `/items` is the canonical visual inbox: a Memorial is the only user-facing Item, Matter is an optional topic filter and handoff context, and Intent appears only as a timed-reminder attribute. Ordinary decisions wait for phone/web batch review while Lark stays a sparse conversation and immediate-decision channel. A durable Matter still connects Lark, Claude/Codex sessions, jobs, and artifacts under the surface. Exact Item and Matter routes support durable `电脑继续` / `发到手机` handoffs: the next interaction moves devices, the underlying object never forks, and completion on any surface clears every stale continuation. All Memorial, heartbeat, bot-reply, and Guardian output crosses one SQLite-backed delivery state machine with global sanitization, 6-hour deduplication, throttling, quiet hours, retry, dead-letter, and delivered/read/acted confirmation. An authenticated TLS gateway on `:3458` gives paired phones a revocable, audited PWA; pairing automatically creates a test notice. Tailscale Funnel exposes only that gateway, never `:3456` or `:3457`. See [the cross-device continuity PRD](docs/prd_cross_device_continuity.md), [the unified delivery PRD](docs/prd_unified_delivery_items.md), and [the Matter/mobile PRD](docs/prd_matter_workspace_mobile.md).

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  daemon.py (guardian — monitors + restarts bot.sh)           │
│                                                             │
│  bot.sh (entry point)                                       │
│  ├── Startup: process conflict detection, message replay    │
│  ├── Lark event listener (foreground)                       │
│  │   ├── Text/rich text → claude -p                         │
│  │   ├── Images → download + visual Claude                  │
│  │   ├── Audio → download + Whisper transcribe              │
│  │   ├── Files/video/sticker/card/location → parse + desc   │
│  │   ├── Quote replies → fetch parent + prepend context     │
│  │   └── Auto-retry on empty response (4 attempts, silent) │
│  ├── Heartbeat loop (background) → core/heartbeat_loop.py   │
│  │   ├── Parse HEARTBEAT.md                                 │
│  │   ├── Run pre-scripts (gather data)                      │
│  │   ├── Priority tasks bypass batch cap                    │
│  │   ├── Batch Claude call                                  │
│  │   └── Run post-scripts (act on output)                   │
│  ├── EigenFlux stream (background) → core/ef_stream_loop.py │
│  │   └── WebSocket → real-time PM delivery + Claude analysis│
│  └── Admin console (background, optional) → admin.py        │
│                                                             │
│  core/                            (system)                  │
│  ├── config.py           — jarvis.yaml loader               │
│  ├── heartbeat.py        — task scheduler + priority tasks  │
│  ├── heartbeat_loop.py   — Python heartbeat runner          │
│  ├── ef_stream_loop.py   — EigenFlux WebSocket manager      │
│  ├── memory.py           — tiered memory loader             │
│  ├── session.py          — session rotation + fcntl lock    │
│  ├── search.py           — session history parser           │
│  ├── safety.py           — error-pattern filter             │
│  ├── card.py             — Lark card message builder        │
│  ├── timeutil.py         — timezone / time-range helpers    │
│  └── jobs.py             — background-job registry + runner │
│  (+ actions, tasks, engagement, intentions, prompt, … )     │
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
│  │   daily_plan, activity_log, daily_reflect                │
│  ├── Calendar & Tasks:                                      │
│  │   calendar_sync, calendar_write, task_triage,            │
│  │   weekly_review                                          │
│  ├── Memory pipeline:                                       │
│  │   memory_hourly → daily → weekly → monthly,              │
│  │   memory_consolidate, memory_tidy                        │
│  ├── EigenFlux:                                             │
│  │   feed, messages, publish, profile, research             │
│  ├── Content:                                               │
│  │   content_recommend (reaction = one-tap watch-later)    │
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

# Set up initial memory (tiered structure)
mkdir -p ~/.jarvis/memory
cp -R examples/memory/* ~/.jarvis/memory/
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

Create `~/Library/LaunchAgents/com.jarvis.daemon.plist` — point at `daemon.py` (not `bot.sh` directly), so the guardian can monitor and auto-restart the bot:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.jarvis.daemon</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>/path/to/pascal-jarvis/daemon.py</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/tmp/jarvis-daemon-stdout.log</string>
  <key>StandardErrorPath</key><string>/tmp/jarvis-daemon-stderr.log</string>
</dict>
</plist>
```

```bash
launchctl load ~/Library/LaunchAgents/com.jarvis.daemon.plist
```

This auto-starts on login. The daemon manages `bot.sh` lifecycle — you can also run `./bot.sh` directly for development.

## Configuration

All config lives in `jarvis.yaml`. See `jarvis.example.yaml` for the full schema.

Key settings:
- `data_dir` — where sessions and memory are stored
- `lark.user_id` — your Lark/Feishu open_id (skip for headless)
- `claude.heartbeat_model` — model for background tasks. The example config ships `sonnet` (cheaper — recommended while trying things out); switch to `opus` for the highest-quality proactive work. Note the heartbeat calls Claude continuously, so this is your main cost lever.
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

## Background Jobs

Long-running requests (deep research, multi-step builds) don't have to block the
conversation. When a task would take a while, the bot can spin it off into an
**independent background job** — a separate Claude session that runs in its own
process and notifies you with a result card when it finishes. Your foreground
chat stays responsive the whole time.

From Lark you control jobs with three commands:

| Command | What it does |
|---|---|
| `jobs` | List running/recent jobs with their status |
| `job output <id>` | Show the full result of a job |
| `cancel <id>` | Cancel a running job |

When a job starts you get a "🚀 后台任务已启动" card with its `<id>`; when it
finishes (or is killed by the watchdog) you get a completion card. Job state
lives under `jobs/` (gitignored runtime data) and is managed by `core/jobs.py`.

The key invariant — **one conversation = one session file = at most one Claude
process at a time** — and how apparent parallelism is achieved across three
separate execution lanes is documented in
[docs/concurrency_and_bg_jobs.md](docs/concurrency_and_bg_jobs.md).

## Plugins

Jarvis ships with **two built-in plugins** that are integrated at the system level. Each has a dedicated README with full setup, API, and troubleshooting.

### Lark (Feishu) — IM Bridge

📖 **Full docs: [plugins/lark/README.md](plugins/lark/README.md)**

Chat with your agent from Lark/Feishu on any device. The plugin:
- Subscribes to incoming messages (`im.message.receive_v1`)
- Handles all message types: text, rich text, images, files, audio (with Whisper transcription), video, stickers, interactive cards, locations, contact/chat shares, merged forwards
- Supports quote replies — fetches the quoted message and passes it as context
- Maps each conversation (`conv_key`) to a stable Claude Code session
- Auto-rotates sessions when they cross `claude.max_session_size`
- Auto-retries on empty Claude responses (4 attempts, escalating backoff)
- Shows transient `Thinking...` indicators during Claude calls
- Saves in-flight messages on shutdown/restart, notifies user to resend on startup
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

[EigenFlux](https://eigenflux.ai) is a broadcast network where AI agents share and receive real-time signals. Six heartbeat tasks plus a real-time stream integrate it:

| Task | Interval | What it does |
|---|---|---|
| `eigenflux-feed-triage` | 10m | Pull feed, score items, push actionable ones to you |
| `eigenflux-research`    | 30m | Deep analysis of items flagged as "needs research" |
| `eigenflux-messages`    | 10m | Fetch unread DMs, suggest responses (**priority**) |
| `eigenflux-friends`     | 10m | Detect incoming friend requests (**priority**) |
| `eigenflux-publish`     | 1h  | Auto-broadcast useful signals from your conversations |
| `eigenflux-profile`     | 24h | Sync your EigenFlux bio with memory changes |

`eigenflux-messages` and `eigenflux-friends` are **priority tasks** — they bypass the batch cap (max 4 regular tasks per cycle) so social signals are never delayed.

Additionally, `bot.sh` runs a continuous EigenFlux stream (WebSocket) that delivers messages in real-time with background Claude analysis. The stream is managed exclusively by `ef_stream_loop.py` — the `openclaw-eigenflux` gateway plugin must be disabled to avoid "Connection replaced" conflicts.

**Enable** — add to `jarvis.yaml`:
```yaml
plugins:
  eigenflux:
    enabled: true
    persist_feed: true
    feed_db: eigenflux/feed_store.jsonl
```

**Setup** — install the official `eigenflux` CLI and authenticate
(see [the plugin README](plugins/eigenflux/README.md#first-time-setup)).

**Programmatic access** — call the CLI directly via `plugins/eigenflux/client.sh`
(bash) or `python3 -m plugins.eigenflux.feed_search` (Python). The plugin
intentionally has no standalone Python SDK — the CLI is the only API surface.

### Writing your own plugin

A plugin is just a directory under `plugins/` that provides one or both of:

1. **A client wrapper** (shell helpers around a CLI, or Python helpers) — the shared code task scripts import.
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

- Monitors `bot.sh` health every 30 seconds — checks PID, heartbeat freshness, and session locks (suspended during `restart.sh` deploy windows via the `.deploying` flag)
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

## Admin Console & Dashboard

Two local web UIs ship with Jarvis, on adjacent ports:

### Admin console — `admin.py` (port 3456)

A lightweight console for browsing memories, searching session history, and
viewing skills/settings.

```bash
python3 admin.py
# open http://localhost:3456
```

Configure host/port in `jarvis.yaml` under the `admin:` section. Config-driven: it reads `memory_dir` and derives the sessions path from `work_dir`, so it always matches your bot's view.

### Dashboard — `dashboard/` (port 3457)

A richer [NiceGUI](https://nicegui.io) app with live pages for home, tasks,
Items, topic drill-down, bookmarks, "thinking" stream, agent calendar, and settings. It keeps
its own SQLite store (`data/jarvis.db`) for bookmarks and cached views.

```bash
./dashboard/start.sh             # foreground
./dashboard/start.sh --bg        # background (daemonized)
./dashboard/start.sh --status    # check if running → http://127.0.0.1:3457
./dashboard/start.sh --stop      # stop the background process
./dashboard/start.sh --install-launchd   # macOS auto-start on login
```

## Troubleshooting

**Bot stuck on "Thinking..." forever**
- Check `jarvis.log` for errors
- Verify `work_dir` in `jarvis.yaml` matches where your Claude Code sessions live (`~/.claude/projects/<hash>/`)
- Delete `active_sessions.json` to start fresh sessions in the correct project dir

**`[SDK Error] handle message failed` in logs**
- Benign — lark-cli receives event types (like `message_read_v1`, `reaction.created_v1`) it doesn't have a handler for. The bot ignores these.

**EigenFlux stream "Connection replaced" loop**
- Only one WebSocket connection per agent is allowed. If `openclaw-eigenflux` plugin is loaded, it competes with `ef_stream_loop.py`. Fix: `openclaw plugins disable openclaw-eigenflux && openclaw gateway restart`.
- The self-diagnostic task checks for competing stream processes on each run.

**Voice messages not transcribed**
- Requires `OPENAI_API_KEY` environment variable set for Whisper API access. Without it, audio is downloaded but the user is asked to type instead.

**Claude Code is rate-limited**
- Configure `claude.backup_auth_token` + `claude.backup_base_url` for the first
  Claude Code-compatible relay. An optional `claude.backup2_*` block provides
  another independent relay before GPT.
- Configure `openai.api_key` or `OPENAI_API_KEY` for the final GPT fallback.
  Main-chat fallback can use the bounded `bash`, `file_read`, and `file_write`
  loop in `core.openai_fallback`; pass `--no-tools` for text-only operation.
  Owner background jobs retain the tool loop. Group conversations, heartbeat,
  EigenFlux analysis, progress narration, and session compaction use text-only
  paths by design. These auxiliary paths share the same four-stage provider
  order through `core.aux_model`.

**Heartbeat not running tasks**
- Check `heartbeat_state.json` for last-run timestamps
- Delete it to force all tasks to run on next cycle
- Tasks also skip if their pre-script exits with empty output (see `tasks/*.sh`)

**Tests**
```bash
python3 -m pytest tests/
```

## Developer Documentation

Deeper design notes live in [`docs/`](docs/):

- [docs/design_task_system.md](docs/design_task_system.md) — the philosophical task
  system (praxis/poiesis capture → commit → decay): data model, lifecycle, and rationale.
- [docs/concurrency_and_bg_jobs.md](docs/concurrency_and_bg_jobs.md) — how the bot
  stays responsive while running long tasks: the three execution lanes and the
  one-conversation-one-session-file rule.

Operational reference for the heartbeat tasks themselves lives in `HEARTBEAT.md`;
the roadmap and explicitly-out-of-scope ideas are in `TODO.md`.

## License

MIT
