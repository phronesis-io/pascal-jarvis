# Jarvis Documentation Map

The repository contains current contracts, operational runbooks, generated
evidence, and historical PRDs. They do not have equal authority.

## Read First

1. [`current_system.md`](current_system.md) - dated map of product surfaces,
   mechanisms, frozen scope, and release truth.
2. [`../PRODUCT.md`](../PRODUCT.md) - human outcomes, product surface, and
   non-goals.
3. [`../DOMAIN.md`](../DOMAIN.md) - vocabulary and invariants.
4. [`../ARCHITECTURE.md`](../ARCHITECTURE.md) - runtime, flows, modules, state,
   and authority.
5. [`../DECISIONS.md`](../DECISIONS.md) - easy-to-confuse ownership and change
   routing decisions.
6. [`../DESIGN.md`](../DESIGN.md) - attention, Lark, card, and interaction rules.

Product expansion is frozen as of 2026-08-17. Reliability, privacy, tests,
documentation, evidence, and behavior-preserving debt retirement continue. A
historical PRD cannot implicitly add a surface, notification lane, workflow, or
authority.

## Operations

- [`INSTALL.md`](INSTALL.md) - first installation, identity setup, model
  fallbacks, launchd, and governed release.
- [`RESTORE.md`](RESTORE.md) - verified backup and disaster recovery.
- [`heartbeat_tasks.md`](heartbeat_tasks.md) - current scheduler pre/model/post
  contract and failure semantics.
- [`concurrency_and_bg_jobs.md`](concurrency_and_bg_jobs.md) - foreground,
  background, and session ownership.
- [`delivery_retry_and_caps.md`](delivery_retry_and_caps.md) - frozen Delivery
  retry/cap state machine.

## Evidence And Health

- [`capability_inventory.md`](capability_inventory.md) - generated list of
  active executable surfaces and test references.
- [`engineering_health.md`](engineering_health.md) - reproducible debt and
  audit verdicts.
- [`repository_scorecard.md`](repository_scorecard.md) - dated source-quality
  and release-evidence assessment.
- [`release_acceptance_2026-07-24.md`](release_acceptance_2026-07-24.md) -
  requirement, remediation, and release-evidence ledger.
- [`prd_portfolio.md`](prd_portfolio.md) - authority for whether every PRD is
  current, historical, superseded, rejected, or production-gated.

## Current Product Contracts

These PRDs still define a current mechanism, sometimes with explicitly
superseded clauses:

- [`prd_unified_delivery_items.md`](prd_unified_delivery_items.md) - one Item
  and Delivery authority; all phone/web routing clauses are retired.
- [`prd_verified_delegation.md`](prd_verified_delegation.md) - deterministic
  external-action evidence; automatic shadow promotion remains gated.
- [`prd_cross_session_context.md`](prd_cross_session_context.md) - private
  Claude Code/Codex continuity and historical index.
- [`prd_codex_runtime_switch.md`](prd_codex_runtime_switch.md) - owner-private
  Claude/Codex route choice and safe replay boundary.
- [`prd_perception_ingestion.md`](prd_perception_ingestion.md) - shipped
  perception core; new source expansion is frozen and demand-gated.
- [`prd_card_delivery_closure.md`](prd_card_delivery_closure.md) - honest taps
  and durable approval; historical web/phone routing is superseded.
- [`prd_companion_checkin.md`](prd_companion_checkin.md) - bounded check-in
  behavior.
- [`prd_2026_08_11_signal_over_noise.md`](prd_2026_08_11_signal_over_noise.md) -
  Lark-only attention and retired mobile gateway.

## Historical Material

All dated files under [`plans/`](plans/) are implementation records. The other
`prd_*.md`, `design_task_system.md`, and research documents may describe
earlier topology, line numbers, measurements, or target-state code that has
since shipped, changed, or been rejected.

Before acting on any historical statement:

1. check [`prd_portfolio.md`](prd_portfolio.md);
2. compare it with the current Product/Domain/Architecture/Decisions contract;
3. reproduce the behavior in code, tests, and production evidence;
4. require an explicit owner thaw if it expands the frozen product.

Do not rewrite historical observations to look current. Add a supersession
banner or update the portfolio, while preserving the original evidence that
explains the decision.
