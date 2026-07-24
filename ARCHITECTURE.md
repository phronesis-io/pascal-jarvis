# Jarvis Architecture

## Runtime Topology

```text
launchd
  -> taskline sidecar :8787 (optional engineering queue)
  -> daemon.py
       -> bot.sh
            -> Lark event listener
            -> core.heartbeat_loop
            -> core.ef_stream_loop
            -> admin.py :3456
  -> dashboard.main :3457
  -> dashboard.mobile_gateway :3458
```

`components.yaml` is the only manifest of what should be alive. The daemon,
doctor, restart/status tooling, and self-diagnostic consume it.

## Main Flows

### Conversation

```text
Lark event
  -> bot.sh parsing and trust boundary
  -> core.prompt current memory + Matter context
  -> selected model/provider
  -> deterministic action processors
  -> core.delivery reply envelope
  -> Lark receipt + Matter timeline
```

Group chat is fail-closed: curated group context, restricted tools, and no
owner-private writes.

### Proactive Work

```text
HEARTBEAT.md task
  -> pre-hook gathers typed evidence
  -> HeartbeatRunner batches model work
  -> post-hook parses a bounded contract
  -> Memorial / Intent / verified action
  -> core.delivery
```

The model authors content. Deterministic code owns scheduling, side effects,
state transitions, retries, and completion.

### Perception

```text
sources.yaml
  -> sources/<type>.py collect()
  -> core.perception Signal
  -> cross-source dedup + sensitivity
  -> memory/system inbox
  -> task-specific consolidation or delivery
```

Adding a source extends the adapter registry. It does not add a new scheduler
or delivery stack.

### Cross-Device Continuity

```text
Item / Matter
  -> Handoff lease
  -> exact stable route on phone or desktop
  -> action on the same underlying object
  -> all stale handoffs close
```

### Verified Delegation

```text
accepted outcome contract
  -> stable target + risk/authorization binding
  -> required-step DAG with claim/lease
  -> connector mutation with idempotency key
  -> authoritative read-back verifier
  -> evidence-derived Delegation state
  -> one-way Item/Matter/Intent/Handoff projections
```

The reconciler scans only bounded non-terminal work. Shadow capture is
observation-only and excluded from active Delegation lists and product metrics.

### Engineering Loops

```text
L3 signal -> deduplicated Proposal -> human accept
  -> L2 Taskline dependency queue + claim/lease/worktree
  -> L1 spec -> dev -> test -> review -> merged PR
  -> release gate -> runtime verify/smoke
  -> L3 outcome observation
```

Taskline is an optional external sidecar with a separate database. Its tasks
never become personal Intents. `core.taskline_bridge` links engineering
evidence into Delegation so an Agent can recover context without treating its
own prose as proof.

## Module Boundaries

- `core.delivery`: the only user-facing retry, dedup, quiet-hour, throttle,
  routing, and delivery-state machine. It uses short-lived connections,
  initializes schema once per database inode, and never holds a transaction
  across a network send.
- `core.memorial`: visible Item and decision ledger.
- `core.intentions` and `core.intent_*`: time, trigger, retry, and closure.
- `core.matters`: durable topic identity and executor context.
- `core.continuity`: device handoff leases and resume state. Each operation
  owns and closes its SQLite connection; active-handoff creation is atomic.
- `core.perception`: typed inbound signals and sensitivity.
- `core.eigenflux_messages`: deterministic friend identity, message
  idempotency, paginated discovery, send receipt, and authoritative read-back.
- `core.delegations`: accepted outcome contracts, step DAGs, claims, evidence,
  state transitions, links, metrics, and shadow labels.
- `core.delegation_verify`: read-only authoritative verifier registry.
- `core.delegation_projection`: one-way projections into existing user and
  execution objects.
- `core.iteration_loop`: L3 signals, proposals, human acceptance, and
  post-release outcome observations.
- `core.taskline_bridge`: L2 sidecar health, claims, leases, isolated
  worktrees, and Delegation links.
- `core.provider_health`: bounded provider canaries and sanitized model-chain
  observability.
- `core.release_gate`: fail-closed merged-PR, CI, branch-protection, and
  independent-review evidence before a production restart.
- `core.actions`: narrow dispatch for explicit system actions.
- `core.memory`: tiered context selection, not an authority for mutable
  external facts.
- `dashboard/`: human and operator projections over the same durable state.

## Durable State

`data/jarvis.db` uses SQLite WAL for cross-process state:

- delivery envelopes, attempts, events, and dead letters;
- Intent state and breaches;
- schedule events and runtime versions;
- Matters, Handoffs, and cross-device state;
- Delegations, steps, evidence, events, links, and shadow labels;
- L3 signals, proposals, and post-release observations;
- verified external-action receipts.

Append-only JSONL remains where event history itself is useful, notably
Memorial and compatibility ledgers. New policy must not depend on two writable
sources for the same state.

## Authority Matrix

| Claim | Authority |
|---|---|
| component is healthy | `core.components` live check |
| message was sent to an EigenFlux friend | server friend record + message history |
| Delegation is complete | all required steps have matching authoritative evidence |
| Lark output was delivered | transport `message_id` / delivery row |
| Item was decided | Memorial ledger projected into delivery state |
| Intent completed | Intent lifecycle and closure evidence |
| deployment is complete | git revision + runtime version + components + smoke |
| model fallback is usable | bounded provider canary plus live routing state |
| calendar is current | calendar API/sync artifact with freshness |

Model prose and memory summaries are never authorities for these claims.

## Dependency Direction

Adapters and task hooks may call domain services. Domain services may call
small infrastructure helpers. User interfaces read domain projections and
invoke domain commands. No producer may bypass `core.delivery` for ordinary
user-facing output, and no LLM prompt may directly own a mutation's terminal
state.
