# Jarvis Heartbeat Tasks And The Pre/Post Contract

- Reviewed: 2026-08-17
- Runtime authority: `HEARTBEAT.md` plus `core.heartbeat`
- Product output: Lark or ledger-only; never the retired web/mobile gateway

The heartbeat runs Jarvis's scheduled background work: memory upkeep, Intent
closure, Routines, calendar sync, EigenFlux, delivery recovery, health, backup,
and L3 observation. The generated component check currently recognizes 39 task
definitions. This document explains the shared execution contract; the task
file remains the authority for names, intervals, priorities, and hooks.

## Execution Shape

Most tasks have a gather/apply pair:

```text
pre hook -> typed DATA -> model route -> bounded output -> post hook
         -> deterministic state/Item -> core.delivery -> Lark receipt
```

- A pre-hook normally gathers fresh evidence and prints it to stdout. Empty
  stdout means “not due/no evidence” and skips the model call.
- A pre-hook may reserve or claim state only when its domain contract requires
  crash recovery, as Routines do. The receipt for that reservation must be
  durable and the post-hook must terminalize or defer it.
- The model writes analysis or content. It never owns schedule watermarks,
  authorization, side effects, delivery truth, or completion.
- A post-hook parses the bounded contract, applies deterministic policy, writes
  private state, and emits either an Item/reply or nothing.
- Tier-0 tasks have no model call: deterministic pre-hook output passes directly
  to deterministic apply/delivery code.

## Scheduler Cycle

`core.heartbeat_loop` invokes `HeartbeatRunner` on a short loop. A cycle:

1. Parses and caches `HEARTBEAT.md` by mtime.
2. Selects due tasks and runs their pre-hooks.
3. Applies priority, batch, isolation, and retry rules only to tasks with data.
4. Runs trusted `no-tools: true` tasks alone so their sandbox cannot alter an
   unrelated task's tool policy.
5. Calls the bounded heartbeat model route: Claude primary, configured Claude-
   compatible backups, then the text-only GPT API path. Heartbeat never uses
   the local Codex tool route.
6. Splits a usable envelope by task and invokes each post-hook.
7. Routes user-facing output through the unified Delivery ledger and records
   scheduler events and watermarks.

Provider selection comes from `core.model_control`; harness code executes the
call. A tiny canary informs health but does not override a real production-call
failure.

## Failure Contracts

The runner distinguishes three outcomes that must not be collapsed:

| Input to post-hook | Meaning | Scheduler/domain behavior |
|---|---|---|
| usable task envelope | Model made a content decision | Apply deterministic policy and close normally |
| `__NO_ENVELOPE__` | Model answered without a usable task slice | Domain records an honest no-content/parse outcome |
| `__CALL_FAILED__` | Quota, timeout, network, shutdown, or model infrastructure prevented a content decision | Domain defers/retries without pretending the model chose silence |

For Routines, `__CALL_FAILED__` closes each claimed run as `deferred`, re-arms
the definition after a short bounded delay, and clears the inflight receipt
only after the database commit. A usable call that omits Routine content is
`no_output`.

Text-only/no-tools calls may continue through bounded network, timeout, server,
quota, authentication, and model-availability failures. A tool-capable process
may have changed local state before failing; uncertain transport/post-tool
failures stop fail-closed and return control to scheduler recovery instead of
being replayed through another provider.

## Post-Hook Rules

Use shared primitives instead of inventing a parser or transport per task:

| Concern | Current boundary |
|---|---|
| Error/sentinel handling | `core.safety` plus the task's explicit ACK contract |
| JSON envelope parsing | `core.safety.parse_json_response` |
| Atomic files / JSONL | `core.safety.atomic_write`, `core.jsonl` |
| Time | `core.timeutil` with injected clocks in tests |
| Cards | `core.card`, `core.memorial_cards` |
| User-visible state and batching | `core.memorial` |
| Delivery/retry/dedup/dead-letter | `core.delivery` |
| Bot transport | `core.lark_bot_transport` |
| Model route and health | `core.model_control`, `core.provider_health` |
| Routine authority and run audit | `core.routines`, `core.routine_evidence` |

Hard rules:

- Never print raw provider JSON, stderr, credentials, or tool payloads to the
  user-facing stdout channel.
- Never use a model sentence as proof that a mutation, delivery, or schedule
  transition completed.
- Never maintain a producer-local retry/dedup truth beside `core.delivery`.
- Time-dependent tests inject an aware clock; tests never write production
  runtime paths.
- Derived/external text uses a no-tools boundary and does not receive private
  owner memory unless its task contract explicitly permits the purpose.

## Task Families

The exact roster is generated from `HEARTBEAT.md`; current families include:

- memory and cross-session continuity;
- daily rhythm, check-in, calendar, Intent closure, and active Routines;
- EigenFlux feed, profile, friendship, publish, preinstall, and stream support;
- perception and content curation;
- Delivery flush/dead-letter recovery and provider health;
- components, self-diagnostic, Guardian support, backup, log maintenance, and
  repository sync;
- Delegation reconciliation, Taskline bridging, and L3 observation.

Retired task names in historical PRDs are not a reason to recreate them. Check
`docs/capability_inventory.md` and the parsed live roster before changing a
family.

## Adding Or Changing A Task

Product expansion is frozen. A new product-facing task, proactive lane, or
authority requires an explicit owner thaw and updated Product/Domain/Design
contracts. Reliability and maintenance changes should:

1. reproduce the real incident from scheduler events and domain receipts;
2. add a regression for the observed failure class;
3. keep evidence gathering, model content, authority, and delivery separate;
4. test skip, malformed, infrastructure-failure, retry, duplicate, and
   terminal outcomes as applicable;
5. update `HEARTBEAT.md`, the capability inventory, and this document when the
   shared contract changes;
6. pass focused tests, the full protected CI suite, governed release, runtime
   revision checks, real delivery/provider smoke, and post-release observation.

## Why There Is No PostScript Framework

The small-hook form remains deliberate. Shared correctness belongs in narrow
tested helpers, while task-specific transformation remains explicit. Introduce
an abstraction only when it removes repeated policy or a demonstrated failure
class; do not force every working hook through a framework for visual
uniformity.
