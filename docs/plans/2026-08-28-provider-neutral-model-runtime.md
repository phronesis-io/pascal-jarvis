# Provider-Neutral Model Runtime

**Status:** foundation implemented in repository; review and release pending  
**Parent:** `2026-08-27-codex-frontstage-jarvis-backstage.md` Phase 3  
**Date:** 2026-08-28

## Problem

Jarvis already had a canonical route catalog, but execution still belonged to
several unrelated loops. Each loop made its own timeout, fallback, tool, health,
and receipt decisions. A tiny canary could look green while a production call
timed out, and a failed tool-capable call could not be safely replayed without
knowing whether an effect had started.

The Model Runtime is a narrow execution boundary. It does not become a new
product brain, task system, permission source, or completion authority.

## Contract

One `RuntimeRequest` declares:

- a required task ID and optional Matter ID;
- trust context and route preference;
- requested model tier, if any;
- an optional ordered route subset when a caller has a narrower contract;
- effect authority: none, read-only, workspace write, or external;
- whether tools are available;
- one total wall-clock budget.

`core.model_control` chooses eligible routes. An adapter runs one bounded model
attempt. `core.model_runtime` decides whether another model or route is safe,
then persists one call receipt and one row per attempt.

## Safety Invariants

1. Group and untrusted contexts cannot turn tools on.
2. Write/external calls require a tool-capable request.
3. Pre-execution failures may move routes because no effect began.
4. Transport or ambiguous failures may replay only for effect-free/read-only
   work, or when the adapter proves `effects_started=false`.
5. A `next_model` hint never bypasses the same replay gate.
6. Cross-family fallback uses the destination route's model; a Claude tier is
   never sent as an OpenAI model name.
7. Prompts, credentials, and raw provider errors are not persisted.
8. Health observation failure cannot alter the model result or leave its call
   receipt running.
9. A stale `running` receipt is recovered only when its recorded executor PID
   is confirmed absent; recovery never guesses that a live call has ended, and
   interrupted write/external work becomes `ambiguous` for reconciliation.
10. Contradictory pre-execution evidence fails closed, and an effectful
    cancellation with unknown effect state also requires reconciliation.
11. The request digest covers system and user prompts while persisting neither;
    model identifiers are bounded and reject control characters.
12. After a tool-capable provider process starts, later execution/transport
    error text cannot prove that no tool ran; automatic cross-route replay
    stops for reconciliation. Explicit account/model/auth/rate/overload
    admission rejection remains pre-execution and may fail over.
13. Replay safety and provider health are separate judgments: an ambiguous
    tool-capable network failure cannot be replayed, but still cools the broken
    route for later calls.
14. Generic `opus` means the configured high-quality tier. A relay receives
    its own configured Opus alias; explicit `sonnet` and `haiku` tiers remain
    portable across Claude-compatible routes.

## Current Implementation

- `core.model_runtime`: policy execution, budget, replay, receipts, audit and
  read-only CLI.
- `core.aux_model`: Claude CLI and OpenAI Responses adapters now consume the
  shared runtime instead of maintaining their own provider loop.
- `core.heartbeat_model`: route-specific prompt composition, isolated
  credentials, Claude CLI/OpenAI adapters, bounded relay timeouts, usage
  observations, and transient redacted diagnostics.
- `core.owner_chat_model` and `core.owner_chat_adapters`: one owner-private
  Lark turn boundary for Claude, Codex, and OpenAI. The resident shell passes
  one gate/preference fact, supervises one killable wrapper, and receives a
  bounded result envelope; route selection and replay do not happen again in
  shell.
- Migrated callers: compaction, EigenFlux analysis, heartbeat idle-noise
  classification, all primary heartbeat task execution, and owner-private Lark
  conversations. Solo and batch heartbeat calls and owner turns carry stable
  task IDs and durable per-attempt receipts.
- The previous heartbeat provider loop and its duplicate timeout/fallback
  helpers have been deleted. The owner shell's second Codex provider loop has
  also been deleted; both callers now have one provider execution path.
- SQLite migration v16: `model_runtime_calls` and `model_runtime_attempts`.
- `model-runtime` component: observation-only checks for stale calls, receipt
  mismatch, repeated recent failure, and recent ambiguous write/external
  effects. Authoritative reconciliation remains with the owning connector.

## Remaining Migration And Evidence

- Shared/group and non-owner Lark traffic intentionally remains on the
  restricted text/no-private-tools path. Moving that boundary is separate work
  and must preserve its narrower trust contract; it is not implied by the
  owner migration.
- Per-provider cost adapters where a provider returns authoritative usage.
- Independent review, protected CI, production receipt observation, and runtime
  failure-budget calibration for the owner-chat candidate.

The unattended self-improve coding worker is not a remaining caller. It was
retired on 2026-08-29 after a real containment probe proved that a workspace-
write coding process could submit a second launchd job outside the controller
coalition. L3 remains observation and proposal generation; mutation starts only
inside an owner-started Codex or Claude Code task.

The bullets above are named remaining gaps, not hidden completion. Until they
converge, Phase 3 remains partial for the retained runtime callers.

## Acceptance For This Foundation

- effect-replay, trust, model-family, privacy, attribution, cancellation,
  session-registration and observer-failure tests pass;
- existing auxiliary call behavior stays compatible;
- component health is read-only and does not create a restart or alert loop;
- full local suite, maintainability budget, capability inventory, CI and review
  pass before release;
- production deploy creates the schema, runs a bounded read-only call, and
  leaves a terminal receipt at the same revision.

## Non-Goals

- inferring product completion from model success;
- persisting prompts for convenience;
- replaying an uncertain side effect to improve availability;
- pretending provider allowance or cost is known when no authoritative API
  exposes it;
- migrating shared/untrusted chat without its own focused review and runtime
  evidence. Unattended code mutation was retired separately on 2026-08-29.
