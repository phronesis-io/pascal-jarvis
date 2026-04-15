# Pascal Jarvis

Turn [Claude Code](https://claude.ai/claude-code) into a persistent personal AI agent with continuous heartbeat, self-evolving memory, and bidirectional IM integration.

## What is this?

Pascal Jarvis wraps Claude Code with three capabilities it doesn't have out of the box:

1. **Heartbeat Loop** — A background scheduler that runs tasks on intervals (feed triage, check-ins, memory consolidation). Tasks are defined in a single `HEARTBEAT.md` file and executed via pre/post shell scripts + a batched Claude call.

2. **Tiered Memory System** — Five-layer memory that compresses over time:
   - **Permanent** files (user profile, preferences, project context)
   - **Monthly** archive (compressed months)
   - **Weekly** digest (compressed weeks)
   - **Daily** summaries
   - **Hourly** log (today's fine-grained index)

   Memory is injected into every Claude call, giving it persistent context across sessions.

3. **IM Bridge** — Connects to Lark/Feishu (or runs headless) so you can chat with your agent from your phone. Messages are routed through Claude Code sessions with auto-rotation when context gets too large.

## Architecture

```
┌──────────────────────────────────────────────┐
│  bot.sh (entry point)                        │
│  ├── Lark event listener (foreground)        │
│  │   └── claude -p → reply to user           │
│  └── Heartbeat loop (background)             │
│      └── heartbeat.py                        │
│          ├── Parse HEARTBEAT.md              │
│          ├── Run pre-scripts (gather data)   │
│          ├── Batch Claude call               │
│          └── Run post-scripts (act on output)│
│                                              │
│  core/                                       │
│  ├── config.py    — YAML config loader       │
│  ├── heartbeat.py — task scheduler           │
│  ├── memory.py    — tiered memory loader     │
│  ├── session.py   — session rotation         │
│  └── search.py    — session history search   │
│                                              │
│  plugins/eigenflux/                          │
│  └── client.py    — EigenFlux API + local    │
│                     feed persistence         │
│                                              │
│  tasks/           — pre/post hook scripts    │
│  ├── eigenflux_*  — feed, messages, publish  │
│  ├── checkin_*    — free-time check-ins      │
│  └── memory_*     — hourly→daily→weekly→     │
│                     monthly consolidation    │
└──────────────────────────────────────────────┘
```

## Quick Start

### Prerequisites

- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) installed and authenticated
- Python 3.10+
- PyYAML (`pip install pyyaml`)
- (Optional) [lark-cli](https://github.com/nickerchen/lark-cli) for Lark/Feishu integration
- (Optional) `jq` for message parsing

### Setup

```bash
# Clone the repo
git clone https://github.com/phronesis-io/pascal-jarvis.git
cd pascal-jarvis

# Create your config
cp jarvis.example.yaml jarvis.yaml
# Edit jarvis.yaml — set your data_dir and (optionally) Lark credentials

# Set up initial memory
mkdir -p ~/.jarvis/memory
cp examples/memory/*.md ~/.jarvis/memory/

# (Optional) Set up EigenFlux
mkdir -p eigenflux
cp examples/eigenflux/user_settings.json eigenflux/
# Then authenticate: python3 -c "from plugins.eigenflux.client import EigenFluxClient; EigenFluxClient('eigenflux').login('you@example.com')"

# Make scripts executable
chmod +x bot.sh tasks/*.sh

# Run
./bot.sh
```

### Headless Mode (no Lark)

If you don't set `lark.user_id` in `jarvis.yaml`, the bot runs in heartbeat-only mode — it still does memory consolidation, EigenFlux feed triage, and everything else, but without IM.

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

## EigenFlux Plugin

[EigenFlux](https://eigenflux.ai) is a broadcast network where AI agents share and receive real-time signals. The plugin:

- **Pulls feed** on a 10-minute interval, triages items using your memory context
- **Persists all items locally** to `eigenflux/feed_store.jsonl` for history search
- **Auto-publishes** signals from your conversations (with your approval via prompt)
- **Manages your profile** — auto-updates based on memory changes

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

## License

MIT
