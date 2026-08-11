# Codex and Claude Runtime Switching

- Date: 2026-08-11
- Status: Implemented product contract
- Surface: owner-private Lark conversation

## Problem

Jarvis could prepare manual Matter handoffs for Claude Code or Codex, but its
live Lark runtime could not actually move to Codex. A Claude account limit
therefore skipped directly to an API fallback, and saying "switch to Codex"
did not change the executor behind the conversation.

## Outcome

The user can keep one Lark conversation alive across Claude and Codex without
managing terminal sessions, API keys, or copied context. The provider/model
shown by `/model` is the route that actually answered, not the configured wish.

## Contract

1. Automatic order is Claude primary and relays, local Codex CLI, then GPT API.
2. `切到 Codex` makes Codex the first route for that private conversation.
3. `切回 Claude`, `/model claude`, and `/model auto` restore Claude-first order.
4. A definitely unavailable preferred route continues to the other executor.
   A missing or expired local Codex login is checked before the model turn and
   counts as definitely unavailable.
   An interrupted tool-capable turn with uncertain side effects stops for
   verification instead of being replayed on another model.
5. Each Lark conversation owns one durable Codex thread. A definitely missing
   thread may be recreated once; an uncertain failed turn is never replayed.
   A bounded provider-neutral turn projection carries delivered context back
   across Claude/Codex switches without replacing either native transcript.
6. Codex uses the existing ChatGPT login and a workspace-write review sandbox.
   The resident bot never uses the dangerous sandbox/approval bypass.
7. Group and non-owner conversations cannot select or fall through to the
   local Codex tool route.
8. The Codex health check is exact-marker, read-only, tool-free, ephemeral,
   bounded, and records no credentials or transcript.

## Acceptance Evidence

- command and persistence unit tests;
- stale-versus-uncertain replay tests;
- group trust-boundary regression;
- shell syntax and full repository suite;
- real Codex canary using the configured model;
- post-restart Lark command and provider/model runtime smoke.

## Non-Goals

- Heartbeat and derived/external text do not gain a local Codex tool route.
- Matter handoff remains a separate deep-execution workflow.
- Switching provider does not change confirmation, delegation, delivery, or
  external-action authority.
