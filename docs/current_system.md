# Jarvis Current System

- Snapshot: 2026-08-17
- Product policy: frozen for expansion; reliability, privacy, documentation,
  evidence, and debt retirement continue
- Product surface: Lark
- Source baseline: `main` at `7df0b6b` (PR #82)
- Runtime status: release verification pending; a merge or green PR is not
  deployment evidence

This is the shortest authoritative map of the system as it exists now. Use
`PRODUCT.md`, `DOMAIN.md`, `ARCHITECTURE.md`, and `DECISIONS.md` for the full
contracts. Historical PRDs explain how the system arrived here; they do not
override this page or `docs/prd_portfolio.md`.

## Product Boundary

| Surface | Current role | Product investment |
|---|---|---|
| Lark | Conversation, decisions, alerts, reminders, model status, EigenFlux messages, and verified outcomes | Active and supported |
| Admin `:3456` | Local operator diagnosis and recovery | Maintained, not a daily user surface |
| Dashboard `:3457` | Local archive and operations reference | Frozen; repair only |
| Mobile gateway `:3458` | None | Retired |
| Tailscale / device pairing / Web Push | None | Retired |
| Claude Code and Codex | Deep execution environments connected through Matter and cross-session continuity | Active execution surfaces, not competing inboxes |

“Product frozen” means no new user surface, notification lane, task system,
or autonomous authority is added without an explicit owner decision to thaw
the product. It does not disable existing Routines or stop incident repair,
security work, testing, documentation, observability, or careful module
extraction.

## Core Mechanisms

| Mechanism | What it does | Authority |
|---|---|---|
| Conversation | Handles Lark turns, media, jobs, and Matter continuity | `bot.sh`, `core.conversation_context`, `core.matter_bridge` |
| Model control | Separates upstream account, requested model, harness, tools, route order, and health | `core.model_control`; execution stays in harness adapters |
| Provider fallback | Routes private owner work through Claude-compatible providers, Codex, and GPT according to trust and capability | A route is usable only when its real adapter succeeds; prose and a tiny canary are not completion evidence |
| Cross-session memory | Discovers, redacts, indexes, and retrieves owner-operated Claude Code and Codex conversations | Provider transcripts remain source evidence; indexed memory is private and rebuildable |
| Intent | Keeps one time-bound promise, retries it, and records closure | Deterministic lifecycle code |
| Routine | Runs an existing user-defined rhythm from declared evidence and stored autonomy | Deterministic schedule, evidence, authorization, and run ledger; model authors content only |
| Item / Memorial | Holds the one visible notice or decision | Memorial ledger plus delivery projection |
| Delivery | Deduplicates, throttles, retries, records receipts, and dead-letters all user-facing output | `core.delivery` and provider receipts |
| EigenFlux | Publishes, receives, befriends, and privately messages other agents with verified identity and idempotency | EigenFlux server read-back plus local receipts |
| Delegation | Tracks accepted multi-step outcomes and verified external actions | Required deterministic verifier evidence |
| L1 / L2 / L3 | Completes one engineering task, coordinates the queue, and turns real feedback into human-approved proposals | PR/CI/review/deploy evidence, Taskline leases, and post-release observation |

## Model And Harness Boundary

Jarvis owns model policy; harnesses own execution; models supply reasoning and
content. The same GPT family can therefore be reached through Codex, the
OpenAI Responses adapter, or a compatible relay without turning those paths
into one indistinguishable provider.

For an owner-private live conversation the configured route can include:

```text
Claude primary -> Backup 1 -> Backup 2 -> Codex CLI -> GPT API
```

Actual order is capability- and health-aware. Group, external-agent, heartbeat,
and other derived-text paths are text-only and never receive the local Codex
tool route. A tool-capable call that times out or fails after execution may
already have changed local state, so it is not automatically replayed through
another provider. Text-only calls may continue across bounded transport
failures.

## Routine Reliability Contract

Existing Routines are active even though Routine product expansion is frozen.
Every occurrence is claimed before the model call and ends in an audit state:

- `observed`, `delivered`, or `failed` records a real outcome;
- `no_output` means the model answered but supplied no usable content for that
  occurrence;
- `deferred` means quota, timeout, network, shutdown, or another model
  infrastructure failure prevented a content decision. The occurrence is
  re-armed after a short bounded delay and is not mislabeled as `no_output`.

`observe` never reaches the user. `propose` creates one Lark Item. `act` adds
only stored, internal, reversible allow-listed actions. External mutations
remain Verified External Actions regardless of what the model requests.

## Memory And Data Safety

- Private transcripts, user profile, credentials, runtime databases, drafts,
  and receipts stay in gitignored runtime paths.
- Cross-session indexing stores redacted visible turns in a private WAL-backed
  database and can be rebuilt from source transcripts.
- Memory helps reconstruct intent but never overrides current calendar,
  delivery, provider, Git, or external-system truth.
- Session backups cover Claude Code and Codex transcripts, memory trees,
  SQLite databases, config/runtime state, Git bundles, dirty patches, and
  non-ignored drafts; verification checks permissions, checksums, class
  completeness, and SQLite integrity before retention.

## Release Truth

The release lifecycle is deliberately fail-closed:

```text
tested branch -> PR -> protected CI -> trusted review or exact-SHA owner receipt
-> merge to main -> merged-main CI -> release_gate -> governed restart
-> runtime revision + components + delivery/provider/UI smoke -> L3 observation
```

As of this snapshot, PR #81 and PR #82 are merged and their PR CI passed. PR
#82 contains the Routine infrastructure-recovery fix. The production runtime
has not yet supplied the complete post-merge evidence for `7df0b6b`; therefore
this document does not call that revision deployed. See
`docs/repository_scorecard.md` for the dated evidence assessment.

## Where To Look

- Product outcomes and frozen scope: `PRODUCT.md`
- Interaction and attention rules: `DESIGN.md`
- Domain vocabulary and invariants: `DOMAIN.md`
- Runtime and module boundaries: `ARCHITECTURE.md`
- Easy-to-confuse ownership decisions: `DECISIONS.md`
- Supported executable surfaces: `docs/capability_inventory.md`
- Historical PRD status: `docs/prd_portfolio.md`
- Installation and governed operations: `docs/INSTALL.md`
- Backup and disaster recovery: `docs/RESTORE.md`
- Current engineering debt and audit verdicts: `docs/engineering_health.md`
- Release evidence status: `docs/repository_scorecard.md`
