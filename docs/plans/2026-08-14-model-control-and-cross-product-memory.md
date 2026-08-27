# Model Control Plane and Cross-Product Memory

**Status:** model-control portion retained; cross-product-memory design
superseded on 2026-08-27 by
`docs/plans/2026-08-27-cross-product-memory-compiler.md`
**Owner outcome:** Jarvis can change model providers without changing its
product or execution semantics, and the main assistant can recover relevant
history from Pascal's interactive Claude Code and Codex conversations.

## Problem

Jarvis already has five model routes, but their configuration and ordering are
spread across `bot.sh`, heartbeat, auxiliary calls, health probes, and provider
adapters.  A route currently conflates three different facts:

1. who supplies the model and account,
2. which model is requested,
3. which harness executes the turn.

That makes a GPT model exposed through a Claude-compatible relay look like a
Claude provider, hides when two fallbacks share one upstream account, and makes
route behavior expensive to inspect.

The original cross-session diagnosis was: recent Claude Code and Codex
turns are projected correctly, but the 24-hour window is not a historical
memory.  Older decisions are reduced to one small rolling digest, so the main
assistant cannot retrieve a relevant old conversation on demand.

## Product Contract

### Model control plane

- `core.model_control` owns the sanitized route catalog and route policy.
- A route declares `upstream`, `model`, `adapter`, capabilities, trust scopes,
  enabled/configured state, and health state separately.
- Adapters (`claude_cli`, `codex_cli`, `openai_responses`) execute one bounded
  turn.  The control plane never starts a model process or treats prose as an
  execution receipt.
- Owner chat may use tool-capable Codex/OpenAI fallbacks.  Shared or untrusted
  input never receives the local Codex route and receives no model tools.
- `/model` reports the actual previous responder, current route order, route
  health, and whether the configured backups are upstream-independent.
- Configuration and diagnostics never serialize credentials.  Existing
  `claude`, `codex`, and `openai` configuration remains backward compatible.

### Historical cross-product memory contract

This section describes the first searchable-index implementation. It remains
valid for explicit raw-history audit, but it is no longer the default prompt or
durable-memory contract.

- Recent interactive Claude Code/Codex turns keep their direct, bounded prompt
  projection.
- A local, gitignored SQLite index stores only redacted visible turns and
  rebuild metadata.  Provider transcripts remain source of truth.
- Indexing is incremental and bounded per heartbeat cycle.  It converges from
  newest sessions into historical sessions without replaying unchanged files.
- Current owner text retrieves a small set of relevant historical turns.  A
  generic or empty query injects no historical archive.
- Managed Jarvis calls, canaries, subagents, tool payloads, provider errors,
  shared conversations, and credential-shaped text never enter the index.
- The index is never injected into groups or a named Matter.  Mutable claims
  still require authoritative verification.

## Acceptance

1. A MICU OpenAI-compatible GPT route is modeled as
   `adapter=openai_responses`, with its requested and observed model visible.
2. A GPT model exposed through a Claude-compatible endpoint can retain
   `adapter=claude_cli`; model family and harness are not conflated.
3. The catalog warns when Claude relay and GPT fallback share one upstream
   host, so nominal backup count is not mistaken for provider diversity.
4. Route plans differ deterministically for owner, group, heartbeat, trusted
   auxiliary, and untrusted auxiliary contexts.
5. Fresh unhealthy cooldowns remove a route from the executable plan without
   deleting its configuration.
6. Provider health and auxiliary routing consume the shared catalog rather
   than rebuilding route configuration independently.
7. Historical Claude Code and Codex fixtures can be indexed, queried by a
   later topic, deduplicated, updated after append, and deleted after source
   removal.
8. Secrets and provider-error responses are absent from persisted index text
   and rendered context.
9. Owner prompts receive relevant historical context; group and named-Matter
   prompts do not.
10. Focused tests, full local tests, protected CI, independent review, release
    gate, deploy verification, and live read-only provider/memory canaries pass.

## Non-Goals

- Treating model output as proof that a task, document write, or delivery
  completed.
- Sending every historical conversation to a model on every turn.
- Replacing Claude Code, Codex, provider-native sessions, Matter, or the
  curated tiered memory system.
- Claiming independent failover when routes use the same relay or billing
  account.
