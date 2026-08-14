# Audited Product Closure

Status: active implementation plan
Owner: Pascal + Codex
Taskline: `9a8955bd-b88b-4dea-b5c5-c809318a549f`

## Problem

Jarvis has strong implementation and regression coverage, but several product
claims are not true in production.  The attention target is missed, provider
errors enter cross-session context, reduced-budget memory drops relevant facts,
legacy phone ingress remains callable after retirement, health panels confuse
process liveness with capability health, and L3 can close work on one clean
sample.

## Product Contract

1. Lark is the only mobile and decision surface. Jarvis has no Tailscale,
   pairing-code, device-token, Web Push, or public web-desk path. Historical
   rows remain readable only for retention and cleanup.
2. Provider failures are attempts, never assistant memories. Cross-session
   projection contains complete, redacted turns and never starts mid-record.
3. Private memory is selected for the current user turn before a reduced model
   budget is applied. Identity and safety remain unconditional; relevant warm
   and system sections outrank unrelated notes.
4. Ordinary proactive Lark cards have a product-wide daily interrupt budget of
   nine. Overflow remains in the Item ledger for the morning docket instead of
   becoming a delayed card backlog. Replies and genuine alerts remain exempt.
5. A health label names exactly what was checked. EigenFlux scheduled-task
   health cannot stand in for real-time stream health; runtime revision checks
   use content identity rather than touch-only mtimes.
6. A known-unhealthy provider rung is skipped during a bounded cooldown. A real
   request updates route health so the next request does not repeat the same
   doomed call.
7. L3 needs two independent post-release clean observations before verification.
   A recurrence after verification reopens the signal and can create follow-up
   work. Conversation complaints are classified by the complained-about
   capability, not by a generic negative adjective.

## Non-goals

- Reintroducing a phone web app, private-network ingress, or new public tunnel.
- Deleting historical database tables or records without a retention migration.
- Replacing lexical retrieval with an external vector database in this batch.
- Weakening alert delivery, external-action verification, or release gates.

## Acceptance

- Active code/config/install surfaces contain no Tailscale or device-pairing
  dependency; the stale production Matter is archived after release.
- Claude/API error text is absent from generated cross-session context and
  clipping preserves complete headings and turns.
- A focused prompt under the 40k backup budget includes the matching private
  memory section while unrelated large sections yield.
- The tenth ordinary proactive delivery in one local day is ledger-only and is
  not retried as an individual card.
- Components and the EigenFlux page expose real-time stream state separately;
  touch-only source changes do not fail deploy verification.
- An observed unhealthy backup is skipped on the next provider-gated turn.
- L3 remains shipped after one clean observation and verifies after the second.
- Focused tests, strict full suite, public-repo hygiene, import-graph gate,
  desktop/mobile browser smoke, backup verification, deploy verification, and
  post-release production queries all pass.
