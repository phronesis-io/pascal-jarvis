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
12. After a tool-capable provider process starts, later error text cannot prove
    that no tool ran; automatic cross-route replay stops for reconciliation.

## Current Implementation

- `core.model_runtime`: policy execution, budget, replay, receipts, audit and
  read-only CLI.
- `core.aux_model`: Claude CLI and OpenAI Responses adapters now consume the
  shared runtime instead of maintaining their own provider loop.
- Migrated callers: compaction, EigenFlux analysis, and heartbeat idle-noise
  classification, each with a stable task attribution.
- SQLite migration v16: `model_runtime_calls` and `model_runtime_attempts`.
- `model-runtime` component: observation-only checks for stale calls, receipt
  mismatch, repeated recent failure, and recent ambiguous write/external
  effects. Authoritative reconciliation remains with the owning connector.

## Remaining Migration

- Main owner/group Lark conversation execution in `bot.sh`.
- Primary heartbeat task execution in `core.heartbeat`.
- Self-improve coding worker, after its acquire/run/release receipt is mapped
  without weakening its independent lifecycle boundary.
- Per-provider cost adapters where a provider returns authoritative usage.
- Production receipt observation and runtime failure-budget calibration.

These are named remaining callers, not hidden completion. Until they converge,
Phase 3 remains partial and the architecture gate cannot ban every legacy loop.

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
- migrating the two high-risk main loops in the same unreviewable rewrite.
